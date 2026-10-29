"""自检：不连上游、不起服务，把「最容易被写错」的那几段逻辑跑一遍。

跑法：``python selftest.py``（只需标准库）。
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from ac2api.credits import (
    EXPIRING_SOON_DAYS,
    extract_resources,
    resource_summary,
    summarize_account,
    urgency_rank,
)
from ac2api.keys import generate_key, hash_key, usage_credits, ApiKey
from ac2api.pool import MIN_PICK_GAP_S, Account, AccountPool, NoAccountAvailable
from ac2api import token

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILED.append(name)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_credits() -> None:
    print("[credits] 到期口径")
    now = ms(datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc))

    # 1) 2034 占位值 + 真实的 CycleEndTime → 必须取 CycleEndTime，不能被判成长期有效
    soon = now + 3 * 86400 * 1000
    raw = {
        "PackageCode": "free_basic",
        "PackageName": "体验版基础用量",
        "CycleCapacitySizePrecise": "459.49000428",
        "CycleCapacityRemainPrecise": "120.5",
        "CycleCapacityUsedPrecise": "338.99000428",
        "DeductionEndTime": ms(datetime(2034, 1, 1, tzinfo=timezone.utc)),
        "CycleEndTime": soon,
        "Status": 0,
    }
    item = resource_summary(raw, now)
    check("占位到期值被 CycleEndTime 覆盖", item["expire_at"] == soon, item["expire_at"])
    check("快过期被标记", item["expiring_soon"] is True)
    check("剩余取精确字段", abs(item["remaining"] - 120.5) < 1e-6, item["remaining"])
    check("到期标签为天", item["days_left_label"] == "3 天后到期", item["days_left_label"])

    # 2) 只有占位值，没有 CycleEndTime → 视为长期有效
    raw2 = {"CycleCapacitySize": "100", "CycleCapacityRemain": "40",
            "DeductionEndTime": ms(datetime(2049, 1, 1, tzinfo=timezone.utc))}
    item2 = resource_summary(raw2, now)
    check("超远到期 → 长期有效", item2["expire_at"] is None, item2["expire_at"])
    check("长期有效标签", item2["days_left_label"] == "长期有效")

    # 3) 整数近似 + 缺失总容量 → 用 剩余+已用 补齐，保证自洽
    raw3 = {"CycleCapacityRemain": 10, "CycleCapacityUsed": 90}
    item3 = resource_summary(raw3, now)
    check("总容量补齐", item3["total"] == 100, item3["total"])

    # 4) 字符串秒戳 / ISO / 纯日期 都能解析
    item4 = resource_summary({"CycleCapacityRemain": 1, "CycleEndTime": "2026-11-05 12:00:00"}, now)
    check("ISO 时间可解析", item4["expire_at"] is not None, item4["expire_at"])
    item5 = resource_summary({"CycleCapacityRemain": 1, "CycleEndTime": "2026-11-05"}, now)
    # 纯日期必须落到当天 23:59:59（+08:00），否则少算一天
    expect5 = ms(datetime(2026, 11, 5, 23, 59, 59, tzinfo=timezone(timedelta(hours=8))))
    check("纯日期按当天 23:59:59", item5["expire_at"] == expect5, (item5["expire_at"], expect5))

    # 5) 已过期
    item6 = resource_summary({"CycleCapacityRemain": 5, "CycleEndTime": now - 86400_000}, now)
    check("过期被标记", item6["expired"] is True and item6["days_left_label"] == "已到期")

    print("[credits] 汇总与排序")
    other = resource_summary({"CycleCapacityRemain": 500, "CycleCapacitySize": 500,
                              "CycleEndTime": now + 200 * 86400 * 1000}, now)
    used_up = resource_summary({"CycleCapacityRemain": 0, "CycleCapacitySize": 100,
                                "CycleEndTime": now + 3600_000}, now)
    acc = summarize_account([item, other, used_up], now)
    check("总量合计", abs(acc["total_remaining"] - (120.5 + 500)) < 1e-6, acc["total_remaining"])
    check("7 天内到期合计", abs(acc["expiring_soon_remaining"] - 120.5) < 1e-6, acc["expiring_soon_remaining"])
    check("最早到期取「有剩余」的包", acc["soonest_expire_at"] == soon, acc["soonest_expire_at"])
    check("只列出有剩余的包", len(acc["resources"]) == 2, len(acc["resources"]))
    check("快到期账号排最前", urgency_rank(acc)[0] == 0, urgency_rank(acc))

    long_only = summarize_account([item2], now)
    check("只有长期包 → 第 2 层", urgency_rank(long_only)[0] == 2, urgency_rank(long_only))
    check("空汇总排最后", urgency_rank(None) == (2, float("inf")))

    print("[credits] 上游响应提取")
    payload = {"code": 0, "data": {"resource_package_list": [
        {"PackageCode": "a", "CycleCapacityRemain": 1},
        {"PackageCode": "b", "CycleCapacityRemain": 2},
    ]}}
    found = extract_resources(payload)
    check("能从 data.resource_package_list 取包", len(found) == 2, len(found))
    nested = {"data": {"list": [{"items": [{"CapacityRemain": 3}]}]}}
    check("深层嵌套也能取到", len(extract_resources(nested)) == 1)
    check("无关响应返回空", extract_resources({"code": 0, "data": {"user": 1}}) == [])


def test_pool() -> None:
    print("[pool] 选号 / 租约 / 退避")
    now = ms(datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc))
    expiring = Account(key="expiring", token="t1", summary=summarize_account([
        resource_summary({"CycleCapacityRemain": 100, "CycleCapacitySize": 100,
                          "CycleEndTime": now + 2 * 86400_000}, now)], now))
    rich = Account(key="rich", token="t2", summary=summarize_account([
        resource_summary({"CycleCapacityRemain": 9000, "CycleCapacitySize": 9000,
                          "CycleEndTime": now + 300 * 86400_000}, now)], now))
    pool = AccountPool(accounts=[rich, expiring])

    picked = [pool.pick().key for _ in range(1)]
    check("快过期的号优先（即使额度少）", picked[0] == "expiring", picked)
    pool.release("expiring")

    # 在途不可再选：同一个号被租走之后不能再被挑出来。
    # 注意这里必须用「两个号都很宽裕」的池子 —— 若另一个号更快过期，分层优先级
    # 会合法地改挑它，用例就测不到在途保护了（那是设计行为，不是 bug）。
    lone = AccountPool(accounts=[Account(key="only", token="t", summary=rich.summary)])
    a = lone.pick()
    try:
        lone.pick()
        check("在途账号不再被选中", False, "竟然第二次也租到了")
    except NoAccountAvailable:
        check("在途账号不再被选中", True)
    lone.release(a)

    # 排队等待：wait_s > 0 时不该立刻抛错，而应等到租约归还
    # （客户端并发两个请求，第二个必须排队 —— 否则 Codex 会看到 503）
    def release_later(p: AccountPool, acc: Account, delay: float) -> threading.Timer:
        t = threading.Timer(delay, lambda: p.release(acc))
        t.start()
        return t

    waiter = AccountPool(accounts=[Account(key="only", token="t", summary=rich.summary)])
    held = waiter.pick()
    timer = release_later(waiter, held, 0.3)
    started_at = time.time()
    try:
        got = waiter.pick(wait_s=5.0)
        waited = time.time() - started_at
        check("并发时排队等租约", got.key == "only", got.key)
        check("确实等了一会儿（>0.2s）", waited > 0.2, f"{waited:.2f}s")
        waiter.release(got)
    except NoAccountAvailable as err:
        check("并发时排队等租约", False, err)
    finally:
        timer.cancel()

    # 等不到就超时抛错（不能无限挂）
    loner = AccountPool(accounts=[Account(key="only", token="t", summary=rich.summary)])
    busy = loner.pick()
    started_at = time.time()
    try:
        loner.pick(wait_s=0.3)
        check("wait_s 到期超时报错", False, "竟然租到了")
    except NoAccountAvailable as err:
        elapsed = time.time() - started_at
        check("wait_s 到期超时报错", True, f"{elapsed:.2f}s")
        check("确实等满了 wait_s", elapsed >= 0.28, f"{elapsed:.2f}s")
    loner.release(busy)

    # 真的没号时不要白等：耗尽/禁用要立刻抛错
    dead2 = AccountPool(accounts=[Account(key="x", token="t", summary=summarize_account([], now))])
    started_at = time.time()
    try:
        dead2.pick(wait_s=5.0)
        check("无号时不白等", False)
    except NoAccountAvailable:
        elapsed = time.time() - started_at
        check("无号时不白等（<0.5s 就报错）", elapsed < 0.5, f"{elapsed:.2f}s")

    # 全部耗尽 → 抛 NoAccountAvailable
    dead = AccountPool(accounts=[Account(key="x", token="t", summary=summarize_account([], now))])
    try:
        dead.pick()
        check("耗尽账号不可选", False)
    except NoAccountAvailable:
        check("耗尽账号不可选", True)

    # 会话粘性
    p2 = AccountPool(accounts=[
        Account(key="a", token="t", summary=summarize_account([resource_summary({"CycleCapacityRemain": 100,
            "CycleCapacitySize": 100, "CycleEndTime": now + 30 * 86400_000}, now)], now)),
        Account(key="b", token="t", summary=summarize_account([resource_summary({"CycleCapacityRemain": 100,
            "CycleCapacitySize": 100, "CycleEndTime": now + 30 * 86400_000}, now)], now)),
    ])
    first = p2.pick(session_key="user:1")
    p2.release(first)
    # 防撞号窗口（0.1s）内同一个号会被暂时跳过，等过去再验粘性 ——
    # 窗口本身是刻意设计（避免瞬间把并发全打到同一个号）。
    time.sleep(MIN_PICK_GAP_S * 1.5)
    same = p2.pick(session_key="user:1")
    check("同会话粘同一账号", same.key == first.key, (first.key, same.key))
    p2.release(same)

    # 退避递增且有上限
    b1 = AccountPool.backoff_seconds(1)
    b5 = AccountPool.backoff_seconds(5)
    b20 = AccountPool.backoff_seconds(20)
    check("退避随失败次数增长", b5 > b1, (b1, b5))
    check("退避有上限", b20 <= 8 * 1.25 + 1e-6, b20)

    # upsert 不能清掉在途租约
    p3 = AccountPool()
    acc = p3.upsert(Account(key="k", token="t"))
    acc.in_flight = 1
    p3.upsert(Account(key="k", token="t2", summary=summarize_account([], now)))
    check("刷新积分不清在途", p3.get("k").in_flight == 1 and p3.get("k").token == "t2")


def test_keys() -> None:
    print("[keys] 哈希 / 配额 / 速率")
    k = generate_key()
    check("key 带前缀", k.startswith("ac2api-"), k[:10])
    check("哈希稳定且不等于明文", hash_key(k, "p") == hash_key(k, "p") and hash_key(k, "p") != k)
    check("不同 pepper 不同哈希", hash_key(k, "p") != hash_key(k, ""))

    key = ApiKey(name="t", key_hash=hash_key(k), credit_limit=100, rate_limit=2)
    check("初始有配额", key.has_quota())
    key.record_usage(credits=60, tokens=10)
    check("记账后剩余", key.public()["credit_remaining"] == 40, key.public())
    key.record_usage(credits=50, tokens=5)
    check("超配额后拒绝", not key.has_quota())
    check("剩余不为负", key.public()["credit_remaining"] == 0, key.public())

    key2 = ApiKey(name="r", key_hash="h", rate_limit=2)
    t0 = 1000.0
    check("窗口内前两次放行", key2.check_rate(t0) and key2.check_rate(t0 + 1))
    check("窗口内第三次拒绝", not key2.check_rate(t0 + 2))
    check("下一分钟恢复", key2.check_rate(t0 + 61))

    check("usage 优先取积分字段", usage_credits({"total_credit": 3.5, "total_tokens": 999}) == 3.5)
    # 上游不给积分字段时按 TOKENS_PER_CREDIT（默认 100）折算并向上取整，
    # 且最低 1 积分 —— 实测 423 token / 4 次调用恰好扣 4 积分。
    check("token 折算积分（向上取整）", usage_credits({"total_tokens": 120}) == 2.0,
          usage_credits({"total_tokens": 120}))
    check("token 折算积分（不足一份算 1）", usage_credits({"total_tokens": 30}) == 1.0,
          usage_credits({"total_tokens": 30}))
    check("汇率可配", usage_credits({"total_tokens": 120}, tokens_per_credit=50) == 3.0,
          usage_credits({"total_tokens": 120}, tokens_per_credit=50))
    check("无 usage 记 0", usage_credits(None) == 0.0)


def test_token() -> None:
    print("[token] JWT 过期判定 / 续期响应解析 / 缓存与退避")
    import base64
    import json

    def jwt(payload: dict) -> str:
        seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"header.{seg}.sig"

    now = 1_000_000.0
    fresh = jwt({"exp": now + 3600})
    soon = jwt({"exp": now + 30})
    old = jwt({"exp": now - 10})
    check("未到期不算过期", not token.is_expired(fresh, now=now))
    check("即将到期算过期", token.is_expired(soon, now=now))
    check("已过期算过期", token.is_expired(old, now=now))
    check("非 JWT 当未过期", not token.is_expired("opaque-token", now=now))
    check("Bearer 前缀不影响解析", token.jwt_expiry(f"Bearer {fresh}") == now + 3600)

    parsed = token.parse_refresh_response({"code": 0, "data": {"access_token": "a", "refresh_token": "r"}})
    check("续期响应解析 snake", parsed is not None and parsed.access_token == "a" and parsed.refresh_token == "r")
    parsed = token.parse_refresh_response({"result": {"accessToken": "a2"}})
    check("续期响应宽松解析 camel", parsed is not None and parsed.access_token == "a2")
    check("没有 access_token 视为失败", token.parse_refresh_response({"code": 1}) is None)

    class FakeSettings:
        static_token = ""
        token_url = ""
        account_base = "https://example.invalid"

    src = token.TokenSource(FakeSettings())
    calls = {"n": 0}

    def fake_acquire(account_key: str, *, force: bool) -> str:
        calls["n"] += 1
        return f"tok-{calls['n']}"

    src._acquire = fake_acquire  # type: ignore[method-assign]
    first = src.token_for("acc")
    second = src.token_for("acc")
    check("命中缓存不重复取", first == second == "tok-1" and calls["n"] == 1, calls)
    check("invalidate 后重取", src.token_for("acc", force=True) == "tok-2", calls)

    def boom(account_key: str, *, force: bool) -> str:
        raise token.TokenError("no creds")

    src2 = token.TokenSource(FakeSettings())
    src2._acquire = boom  # type: ignore[method-assign]
    check("取不到返回空串而不是抛错", src2.token_for("x") == "")
    check("记录最近错误", "no creds" in src2.last_error, src2.last_error)


def test_system_prompt() -> None:
    """上游强校验 system 前缀（不满足就 406），网关必须替调用方补齐。"""
    from ac2api.upstream import REQUIRED_SYSTEM_PREFIX, ensure_system_prompt

    def head_of(body: dict) -> str:
        for msg in body["messages"]:
            if msg.get("role") == "system":
                content = msg["content"]
                if isinstance(content, str):
                    return content
                return "\n".join(
                    b if isinstance(b, str) else str(b.get("text", ""))
                    for b in content
                )
        return ""

    print("[upstream] system prompt 前缀校验（406 修复）")
    check("前缀就是实测那句 56 字符",
          REQUIRED_SYSTEM_PREFIX == "You are AutoClaw. Answer the user directly and concisely.")

    bodies = {
        "没有 system": {"messages": [{"role": "user", "content": "hi"}]},
        "system 不合规": {"messages": [{"role": "system", "content": "You are helpful."},
                                    {"role": "user", "content": "hi"}]},
        "system 合规": {"messages": [{"role": "system", "content": REQUIRED_SYSTEM_PREFIX},
                                  {"role": "user", "content": "hi"}]},
        "system 不在首位": {"messages": [{"role": "user", "content": "hi"},
                                     {"role": "system", "content": "X"}]},
        "多模态 system": {"messages": [{"role": "system", "content": [{"type": "text", "text": "abc"}]}]},
    }
    for name, body in bodies.items():
        out = ensure_system_prompt(body)
        check(f"{name} → 补齐前缀", head_of(out).startswith(REQUIRED_SYSTEM_PREFIX),
              head_of(out)[:60])

    # 调用方原文不丢
    merged = head_of(ensure_system_prompt(bodies["system 不合规"]))
    check("原 system 降级为后缀保留", "You are helpful." in merged)

    # 已合规走快速路径，连对象都不复制（热路径省一次拷贝）
    clean = bodies["system 合规"]
    check("已合规原样直通", ensure_system_prompt(clean) is clean)

    # 需要改动时不能污染调用方
    dirty = {"model": "zai_auto", "messages": [{"role": "user", "content": "hi"}]}
    fixed = ensure_system_prompt(dirty)
    check("不污染调用方 messages", fixed["messages"] is not dirty["messages"])
    check("其它字段照旧透传", fixed["model"] == "zai_auto")

    # 幂等：补过的不再重复补
    twice = ensure_system_prompt(fixed)
    check("幂等", twice["messages"] == fixed["messages"])

    # 非 dict / 没有 messages 时原样返回，不抛异常
    check("畸形 payload 不炸", ensure_system_prompt({}) == {})


def test_max_tokens() -> None:
    """上游对 max_tokens 有取值区间校验：越界直接 406（与 system 前缀无关）。"""
    from ac2api.upstream import (MAX_TOKENS_MAX, MAX_TOKENS_MIN,
                                 normalize_max_tokens)

    print("[upstream] max_tokens 区间收敛（406 修复之三）")

    check("区间就是实测的 10000–131072",
          (MAX_TOKENS_MIN, MAX_TOKENS_MAX) == (10000, 131072),
          f"{MAX_TOKENS_MIN}–{MAX_TOKENS_MAX}")

    def out_of(body: dict) -> int:
        return normalize_max_tokens(body)["max_tokens"]

    # 实测被拒的三个值必须被夹回来
    for raw in (1, 1024, 4096, 8192, 8193):
        check(f"max_tokens={raw} → 抬到下限", out_of({"max_tokens": raw}) == MAX_TOKENS_MIN,
              out_of({"max_tokens": raw}))
    for raw in (131073, 150000, 200000, 393216):
        check(f"max_tokens={raw} → 压到上限", out_of({"max_tokens": raw}) == MAX_TOKENS_MAX,
              out_of({"max_tokens": raw}))

    # 实测放行的值必须原样保留（不能被顺手改成别的，那等于偷偷改语义）
    for raw in (10000, 16384, 32768, 131072):
        check(f"max_tokens={raw} 原样保留", out_of({"max_tokens": raw}) == raw)

    # 不给就不加：实测「不发 max_tokens」本身合法
    plain = {"model": "zai_auto", "messages": [{"role": "user", "content": "hi"}]}
    check("没给 max_tokens 就不加", normalize_max_tokens(plain) is plain)

    # max_completion_tokens 也认，但统一成一个字段
    got = normalize_max_tokens({"max_completion_tokens": 4096})
    check("max_completion_tokens 被收敛", got.get("max_tokens") == MAX_TOKENS_MIN, got)
    check("max_completion_tokens 被删掉", "max_completion_tokens" not in got)

    # 两个都给时以 max_tokens 为准
    both = normalize_max_tokens({"max_tokens": 20000, "max_completion_tokens": 100})
    check("两者都给时以 max_tokens 为准", both["max_tokens"] == 20000, both)

    # 类型脏数据不能导致 406 级别的透传
    for bad in (None, True, False, "abc", [], {}):
        check(f"脏值 {bad!r} → 用上限兜底",
              out_of({"max_tokens": bad}) == MAX_TOKENS_MAX, out_of({"max_tokens": bad}))
    check("数字字符串按数字处理", out_of({"max_tokens": "4096"}) == MAX_TOKENS_MIN)
    check("浮点按整数处理", out_of({"max_tokens": 5000.7}) == MAX_TOKENS_MIN)

    # 不污染调用方
    src = {"max_tokens": 4096, "model": "zai_auto"}
    normalize_max_tokens(src)
    check("不污染调用方", src["max_tokens"] == 4096)

    # 畸形 payload 原样返回
    check("非 dict 不炸", normalize_max_tokens(None) is None)
    check("空 dict 原样返回", normalize_max_tokens({}) == {})

    # 幂等：收敛过的再收敛不变
    once = normalize_max_tokens({"max_tokens": 4096})
    check("幂等", normalize_max_tokens(once)["max_tokens"] == once["max_tokens"])


def test_identity_phrase() -> None:
    """上游只拦 `You are <竞品>` 这个句式（单空格、大小写不敏感、只查 system）。"""
    from ac2api.upstream import (BLOCKED_IDENTITY_PATTERN, IDENTITY_REPLACEMENT,
                                 REQUIRED_SYSTEM_PREFIX, ensure_system_prompt,
                                 scrub_identity, scrub_text)

    print("[upstream] 竞品身份句清洗（406 修复之四）")

    # 实测被拦的写法
    for text in ("You are Codex.", "you are codex.", "You are CODEX.",
                 "You are Codex", "  You are Codex.", "Hi. You are Codex.",
                 "You are Claude Code.", "You are claude code."):
        out = scrub_identity(text)
        check(f"拦：{text[:26]!r}", not BLOCKED_IDENTITY_PATTERN.search(out), out[:40])

    # 实测放行的写法（不能误伤）
    for text in ("Codex", "I am Codex", "This is Codex", "You are OpenAI Codex.",
                 "You are ChatGPT.", "You are Claude.", "You are Cursor.",
                 "You are  Codex.", "You are a Codex.", "You are helpful."):
        check(f"放：{text[:26]!r}", scrub_identity(text) == text)

    check("替换词是中性身份", IDENTITY_REPLACEMENT == "You are a terminal coding agent")

    # 两条规则叠加：Codex 真实 instructions 第一句就是 "You are Codex."
    real = ("You are Codex.\n\n## General\n- Be concise.\n\n"
            "running in the Codex CLI, a coding agent.")
    cleaned = scrub_text(real)
    check("真实 instructions 洗后无 You are Codex",
          "you are codex" not in cleaned.lower(), cleaned[:50])
    check("真实 instructions 洗后无 codex cli",
          "codex cli" not in cleaned.lower(), cleaned[:50])
    check("正文其余部分保留", "## General" in cleaned and "Be concise." in cleaned)

    # 端到端：instructions="You are Codex." 经 ensure_system_prompt 后必须干净
    body = {"model": "zai_auto",
            "messages": [{"role": "system", "content": "You are Codex."},
                         {"role": "user", "content": "hi"}]}
    fixed = ensure_system_prompt(body)
    system = fixed["messages"][0]["content"]
    check("补前缀后仍带前缀", system.startswith(REQUIRED_SYSTEM_PREFIX), system[:60])
    check("补前缀后已无 You are Codex",
          not BLOCKED_IDENTITY_PATTERN.search(system), system[:80])

    # 已经合规的系统提示也要洗（走快速路径时不能漏）
    body2 = {"messages": [{"role": "system",
                           "content": f"{REQUIRED_SYSTEM_PREFIX}\n\nYou are Codex."}]}
    fixed2 = ensure_system_prompt(body2)
    check("前缀已合规时照样洗身份句",
          not BLOCKED_IDENTITY_PATTERN.search(fixed2["messages"][0]["content"]),
          fixed2["messages"][0]["content"][:80])

    check("空串不炸", scrub_identity("") == "")
    check("None 不炸", scrub_identity(None) is None)


def test_blocked_phrases() -> None:
    """上游把竞品客户端名当风控信号：正文出现 `codex cli` / `claude code` 就 406。"""
    from ac2api.upstream import (BLOCKED_AGENT_PHRASES, REQUIRED_SYSTEM_PREFIX,
                                 ensure_system_prompt, scrub_agent_phrases)

    print("[upstream] 竞品客户端名清洗（406 修复之二）")

    # 黑名单本身：实测只有这两个词会被拦
    names = [p for p, _ in BLOCKED_AGENT_PHRASES]
    check("黑名单就这两条", sorted(names) == ["claude code", "codex cli"], names)

    cases = {
        "You are a coding agent running in the Codex CLI.": True,
        "You are codex cli.": True,
        "You are CODEX CLI.": True,
        "running in the Codex\nCLI": True,
        "You are Claude Code.": True,
        "You are claude code.": True,
        "You are Codex and CLI are separate.": False,
        "You are OpenAI Codex.": False,
        "You are Cursor.": False,
        "You are Copilot.": False,
        "Nothing to clean.": False,
    }
    for text, should_change in cases.items():
        out = scrub_agent_phrases(text)
        changed = out != text
        check(f"{'改写' if should_change else '不改'}：{text[:38]}", changed == should_change, out[:60])

    # 改完之后不能再含黑名单词，否则上游照样 406
    for text in list(cases):
        out = scrub_agent_phrases(text).lower()
        check(f"清洗彻底：{text[:30]}", "codex cli" not in out and "claude code" not in out, out[:50])

    # 大小写要保持：句首大写
    check("句首大写保留", scrub_agent_phrases("Codex CLI").startswith("T"))

    # 非字符串安全
    check("None 不炸", scrub_agent_phrases(None) is None)

    # 端到端：Codex 桌面端的 instructions 天生带 "Codex CLI"，补前缀后必须能过上游
    body = {
        "model": "zaicoding_glm-5.3",
        "messages": [
            {"role": "system",
             "content": "You are a coding agent running in the Codex CLI, a terminal-based assistant."},
            {"role": "user", "content": "hi"},
        ],
    }
    out = ensure_system_prompt(body)
    system = out["messages"][0]["content"]
    check("带 Codex CLI 的 instructions → 补上前缀", system.startswith(REQUIRED_SYSTEM_PREFIX), system[:50])
    check("→ 竞品名已洗掉", "codex cli" not in system.lower(), system[:80])
    check("→ 原意保留", "coding agent running in the" in system, system[:80])
    check("→ 不污染调用方", "Codex CLI" in body["messages"][0]["content"])

    # 用户消息里的竞品名也要洗（不只是 system）
    body2 = {"messages": [{"role": "user", "content": "how do I configure Codex CLI?"}]}
    out2 = ensure_system_prompt(body2)
    check("user 消息也清洗",
          "codex cli" not in out2["messages"][1]["content"].lower(),
          out2["messages"][1]["content"])

    # 多模态文本块也要洗
    body3 = {"messages": [
        {"role": "system", "content": REQUIRED_SYSTEM_PREFIX},
        {"role": "user", "content": [{"type": "text", "text": "Claude Code 怎么配"}]},
    ]}
    out3 = ensure_system_prompt(body3)
    block = out3["messages"][1]["content"][0]
    check("多模态块也清洗", "claude code" not in block["text"].lower(), block["text"])


def test_store() -> None:
    print("[store] key 落盘 / 重载 / 坏文件容错")
    import tempfile
    from pathlib import Path

    from ac2api.keys import ApiKey
    from ac2api.store import KeyStore

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "keys.json"
        store = KeyStore(path)
        check("空文件读回空表", store.load() == [])

        k1 = ApiKey(name="a", key_hash="h1", credit_limit=100, rate_limit=30)
        k1.record_usage(credits=12.5, tokens=1000)
        k2 = ApiKey(name="b", key_hash="h2", credit_limit=None)
        k2.status = "disabled"
        check("写入成功", store.save([k1.to_record(), k2.to_record()]))
        check("文件真的存在", path.exists())

        text = path.read_text(encoding="utf-8")
        check("落盘不含明文（只有哈希）", "ac2api-" not in text and "h1" in text, text[:80])

        back = KeyStore(path).load()
        check("重载条数", len(back) == 2, back)
        got = {r["name"]: r for r in back}
        check("重载保留配额", got["a"]["credit_limit"] == 100, got["a"])
        check("重载保留用量", got["a"]["credit_used"] == 12.5, got["a"])
        check("重载保留状态", got["b"]["status"] == "disabled", got["b"])
        check("不限量存成 null", got["b"]["credit_limit"] is None, got["b"])

        # 损坏的 JSON 不该让网关起不来，只记录原因
        path.write_text("{ 这不是 json", encoding="utf-8")
        broken = KeyStore(path)
        check("坏文件返回空表", broken.load() == [])
        check("坏文件记录原因", bool(broken.last_error), broken.last_error)

        # 目录不可写时 save 返回 False 而不是抛异常
        bad = KeyStore(Path(tmp) / "sub" / "keys.json")
        check("新建目录后能写", bad.save([k1.to_record()]))
        check("save 成功清空错误", bad.last_error == "")


def test_data_dir_fallback() -> None:
    """数据目录写不进去时必须自动换地方，而不是静默失败。

    这个用例来自一次真实故障：打包成 exe 后，安全软件拦住写用户主目录，
    网关照常跑起来、key 却存不下来，日志也一片空白，查了很久才发现。
    """
    print("[store] 数据目录不可写时自动回退")
    import os
    import tempfile
    from pathlib import Path

    from ac2api import desktop
    from ac2api import store as store_mod

    with tempfile.TemporaryDirectory() as tmp:
        # 显式指定优先，且不该被探测逻辑改写
        custom = Path(tmp) / "custom"
        old = os.environ.get("AC2API_DATA_DIR")
        os.environ["AC2API_DATA_DIR"] = str(custom)
        try:
            check("显式指定目录优先", store_mod.default_data_dir() == custom, str(custom))
        finally:
            if old is None:
                os.environ.pop("AC2API_DATA_DIR", None)
            else:
                os.environ["AC2API_DATA_DIR"] = old

        ok_dir = Path(tmp) / "ok"
        check("可写目录探测通过", store_mod._probe_writable(ok_dir))
        check("探测后不留垃圾文件", not any(ok_dir.glob(".ac2api-write-probe*")), list(ok_dir.iterdir()))

        # 主目录不可写时，_pick_writable_log 必须换到别处且真的能写
        blocked = Path(tmp) / "blocked"
        blocked.mkdir()
        blocked_file = blocked / "desktop.log"
        # 用只读文件模拟「写不进去」
        blocked_file.write_text("", encoding="utf-8")
        blocked_file.chmod(0o444)
        try:
            picked = desktop._pick_writable_log(str(blocked_file))
            check("日志回退到可写位置或原位", isinstance(picked, str) and picked, picked)
            with open(picked, "a", encoding="utf-8") as fh:
                fh.write("回退日志\n")
            check("回退位置确实可写", "回退日志" in open(picked, encoding="utf-8").read())
        finally:
            try:
                blocked_file.chmod(0o666)
            except OSError:
                pass

        # Tee 用回退逻辑，不该因为首选路径不可写就完全哑掉
        tee = desktop.Tee(str(blocked_file))
        tee.write("Tee 回退\n")
        check("Tee 回退文件可读", os.path.exists(tee.path), tee.path)


def test_gateway_keys() -> None:
    print("[gateway] key 增删/落盘/重载一致")
    import tempfile
    from pathlib import Path

    from ac2api.main import Gateway
    from ac2api.pool import AccountPool
    from ac2api.store import KeyStore
    from ac2api.upstream import Settings

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "keys.json"
        settings = Settings()

        def make():
            return Gateway(settings=settings, pool=AccountPool(), upstream=None,
                           key_pepper="pep", store=KeyStore(path))

        gw = make()
        plain = gw.create_key("dev", credit_limit=50, rate_limit=10)
        check("新建即可鉴权", gw.authenticate(plain) is not None)
        check("错误 key 不通过", gw.authenticate("ac2api-nope") is None)
        check("落盘文件已生成", path.exists())

        gw.authenticate(plain).record_usage(credits=7.0, tokens=700)
        gw.flush_usage()

        gw2 = make()
        re_auth = gw2.authenticate(plain)
        check("重启后同一 key 仍可用", re_auth is not None)
        check("重启后用量仍在", re_auth is not None and re_auth.credit_used == 7.0,
              None if re_auth is None else re_auth.credit_used)
        check("重启后配额仍在", re_auth is not None and re_auth.credit_limit == 50)
        check("重启后速率仍在", re_auth is not None and re_auth.rate_limit == 10)

        check("停用生效", gw2.set_key_status("dev", False))
        gw2.flush_usage()
        reloaded = [k for k in make().api_keys.values() if k.name == "dev"]
        check("停用状态跨进程保持",
              len(reloaded) == 1 and reloaded[0].status == "disabled", reloaded)
        check("同名不存在返回 False", not gw2.set_key_status("nope", True))

        # 没有 store 时（测试/纯内存）不该抛异常
        mem = Gateway(settings=settings, pool=AccountPool(), upstream=None)
        p = mem.create_key("mem")
        mem.authenticate(p).record_usage(credits=1, tokens=1)
        mem.note_usage()
        check("纯内存模式可用", mem.authenticate(p) is not None)


def main() -> int:
    started = time.time()
    test_credits()
    test_pool()
    test_keys()
    test_token()
    test_system_prompt()
    test_max_tokens()
    test_identity_phrase()
    test_blocked_phrases()
    test_store()
    test_data_dir_fallback()
    test_gateway_keys()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print(f"全部通过（{time.time() - started:.2f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
