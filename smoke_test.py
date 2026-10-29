#!/usr/bin/env python3
"""ac2api 冒烟测试：打个假上游，把网关整条链路跑一遍。

和 selftest.py 的分工：selftest 只测纯逻辑（不联网、不起服务）；本脚本用
HTTP 层把「鉴权 → 选号 → 转发 → 记账 → 流式透传」串起来验证，**不需要**
AutoClaw 客户端、不需要网络。

跑法：``python smoke_test.py``（需要 fastapi / httpx / uvicorn）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import replace

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ac2api.credits import resource_summary, summarize_account
from ac2api.main import Gateway, create_app
from ac2api.pool import Account, AccountPool
from ac2api.store import KeyStore
from ac2api.upstream import Settings, UpstreamClient

FAILED: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 假上游：模拟 AutoClaw
# ---------------------------------------------------------------------------
def build_fake_upstream() -> FastAPI:
    fake = FastAPI()
    calls: list[dict] = []

    fake.state.calls = calls

    # 网关会在这条路径前面拼上受管网关前缀 /autoclaw-proxy/proxy/autoclaw，
    # 跟真实客户端的请求形状一致（见 ac2api/upstream.py 顶部说明）。
    @fake.post("/autoclaw-proxy/proxy/autoclaw/chat/completions")
    async def chat(request: Request):
        # 对话网关认 X-Authorization（放进 Authorization 会被回 401，
        # 见 ac2api/upstream.py 的 build_auth_headers 说明）。
        auth = request.headers.get("x-authorization", "")
        calls.append({"auth": auth, "sign": request.headers.get("x-auth-sign", ""),
                      "ts": request.headers.get("x-auth-timestamp", "")})
        # 签名头必须存在，否则就是网关漏算了
        if not request.headers.get("x-auth-sign"):
            return JSONResponse({"code": 1001, "msg": "missing sign"}, status_code=401)
        # bad-token 永远 401：保证网关一定会走到「换号重试」那条路径
        if auth.endswith("bad-token"):
            return JSONResponse({"code": 401, "msg": "token invalid"}, status_code=401)

        body = await request.json()
        if body.get("stream"):
            async def gen():
                for piece in ("你", "好"):
                    payload = {"id": "x", "object": "chat.completion.chunk", "created": 1,
                               "model": body.get("model"), "choices": [
                                   {"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                final = {"id": "x", "object": "chat.completion.chunk", "created": 1,
                         "model": body.get("model"), "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"total_tokens": 12, "total_credit": 0.7}}
                yield f"data: {json.dumps(final)}\n\n".encode()
                yield b"data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")

        return {"id": "chatcmpl-fake", "object": "chat.completion", "created": 1,
                "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10, "total_credit": 0.5}}

    # 真实钱包端点（GET，走身份域，用普通 Authorization）
    @fake.get("/agent-assetmgr/api/v2/wallets")
    async def wallets(request: Request):
        # 真实上游的合计口径在 total_balance；缺它网关会当成没积分。
        return {"code": 200, "data": {
            "total_balance": 5160.0,
            "wallets": [
                {"wallet_type": "subscription", "balance": 960.0},
                {"wallet_type": "reward", "balance": 4200.0},
            ],
        }}

    @fake.get("/agent-assetmgr/api/v1/wallet-instances")
    async def wallet_instances(request: Request):
        now_ms = int(time.time() * 1000)
        return {"code": 200, "data": {"wallet_instances": [
            {"wallet_type": "reward", "instance_id": "i1", "balance": 4200.0,
             "total": 5000.0, "expires_at": now_ms + 2 * 86400 * 1000,   # 2 天后到期
             "status": "active"},
            {"wallet_type": "subscription", "instance_id": "i2", "balance": 960.0,
             "total": 1000.0, "expires_at": now_ms + 200 * 86400 * 1000,
             "status": "active"},
        ]}}

    @fake.get("/agent-assetmgr/api/v1/ledgers_std")
    async def ledgers(request: Request):
        return {"code": 200, "data": {"ledgers": []}}

    # 模型目录（GET，走管理域，用 X-Authorization）。不实现它网关也能跑——
    # 会退到 Settings.models 的静态清单——但那要靠两次超时兜底（每次
    # credits_timeout），测试里会慢到触发 httpx 的 5s 超时，所以这里照真实
    # 上游的形状返回一份目录。
    @fake.get("/autoclaw-proxy/proxy/autoclaw-model-config")
    async def model_config(request: Request):
        return {"code": 200, "data": {"models": [
            {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash",
             "contextWindow": 200000, "maxTokens": 65536,
             "creditConsumptionLevel": 1},
            {"id": "glm-5.3", "name": "GLM-5.3",
             "contextWindow": 200000, "maxTokens": 65536,
             "creditConsumptionLevel": 2},
        ]}}

    return fake


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    # 首次启动要加载 FastAPI 路由表，慢机器上 5 秒不够，这里给到 30 秒。
    for _ in range(600):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def wait_http(url: str, tries: int = 600) -> None:
    for _ in range(tries):
        try:
            httpx.get(url, timeout=0.5, trust_env=False)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    raise RuntimeError(f"{url} not ready")


def main() -> int:
    # 测试绝不能碰用户真实的 key 文件（~/.ac2api/keys.json），否则每跑一次
    # 就多几行 smoke / from-admin 垃圾记录。整个测试过程都指向临时目录。
    real_keys_file = KeyStore().path
    tmp_root = tempfile.mkdtemp(prefix="ac2api-smoke-root-")
    os.environ["AC2API_DATA_DIR"] = tmp_root
    os.environ["AC2API_KEYS_FILE"] = os.path.join(tmp_root, "keys.json")
    real_keys_before = real_keys_file.read_bytes() if real_keys_file.exists() else None

    fake_port = free_port()
    gw_port = free_port()
    fake_app = build_fake_upstream()
    serve(fake_app, fake_port)
    fake_base = f"http://127.0.0.1:{fake_port}"
    wait_http(fake_base + "/openapi.json")

    settings = Settings(
        base_url=fake_base,
        account_base=fake_base,                      # 自建/测试上游：账号域与网关同源
        discover=False,                              # 测试里不扫本机 AutoClaw 配置
        token_url="",                                # 不启用外部 token 服务：走账号自带 token
        chat_path="/chat/completions",
        credits_path="/billing/meter/get-user-resource-summary",
        default_model="glm-5.3-flash",
        models=("glm-5.3-flash",),
        read_timeout=30,
        credits_timeout=5,
    )

    now = int(time.time() * 1000)
    # 两个号：一个 token 会先 401（验证换号），一个正常
    bad = Account(key="bad", token="bad-token", nickname="坏号",
                  summary=summarize_account([resource_summary(
                      {"CycleCapacityRemain": 5, "CycleCapacitySize": 5,
                       "CycleEndTime": now + 86400_000}, now)], now))
    good = Account(key="good", token="good-token", nickname="好号",
                   summary=summarize_account([resource_summary(
                       {"CycleCapacityRemain": 5000, "CycleCapacitySize": 5000,
                        "CycleEndTime": now + 500 * 86400_000}, now)], now))

    pool = AccountPool(accounts=[bad, good])
    gw = Gateway(settings=settings, pool=pool, upstream=UpstreamClient(settings))
    app = create_app(settings=settings, gateway=gw)
    key = gw.create_key("smoke", credit_limit=100, rate_limit=100)
    key_obj = lambda: gw.authenticate(key)  # noqa: E731 - 测试里取当前簿记对象
    serve(app, gw_port)
    base = f"http://127.0.0.1:{gw_port}"
    wait_http(base + "/health")
    headers = {"Authorization": f"Bearer {key}"}

    print("[smoke] 元信息")
    r = httpx.get(base + "/health", timeout=5, trust_env=False)
    check("/health 200", r.status_code == 200, r.text)
    check("/health 报告 2 个号", r.json()["accounts"] == 2, r.json())

    r = httpx.get(base + "/v1/models", timeout=5, trust_env=False)
    check("无 key 取模型被拒 401", r.status_code == 401, r.status_code)
    r = httpx.get(base + "/v1/models", headers=headers, timeout=5, trust_env=False)
    check("带 key 取模型 200", r.status_code == 200 and r.json()["data"][0]["id"], r.text)

    print("[smoke] 非流式对话 + 选号")
    # 网关启动时会巡检积分，把两个号都刷成假上游返回的同一份数据，那样就测不出
    # 「选号优先级」了。这里重新灌入两个不同的汇总：
    #   bad  快过期（层级 0）但一直 401  → 必须被先挑中，然后换号重试
    #   good 长期有效（层级 2）且额度充足
    now2 = int(time.time() * 1000)
    pool.get("bad").summary = summarize_account([resource_summary(
        {"CycleCapacityRemain": 5, "CycleCapacitySize": 5,
         "CycleEndTime": now2 + 86400_000}, now2)], now2)
    pool.get("good").summary = summarize_account([resource_summary(
        {"CycleCapacityRemain": 5000, "CycleCapacitySize": 5000,
         "CycleEndTime": now2 + 500 * 86400_000}, now2)], now2)
    for acc in pool.all():
        acc.last_error = ""
    time.sleep(0.2)   # 等过「防撞号窗口」，让断言只反映这次对话
    r = httpx.post(base + "/v1/chat/completions", headers=headers, timeout=10, trust_env=False,
                   json={"messages": [{"role": "user", "content": "hi"}], "conversation_id": "conv-1"})
    check("非流式 200", r.status_code == 200, r.text)
    check("返回内容透传", r.json()["choices"][0]["message"]["content"] == "你好", r.text)
    check("签名头已注入",
          bool(calls := fake_app.state.calls) and all(c["sign"] and c["ts"] for c in calls),
          fake_app.state.calls[:2])
    check("先打快过期号", calls and calls[0]["auth"].endswith("bad-token"), [c["auth"] for c in calls[:3]])
    check("失败后换号重试", any(c["auth"].endswith("good-token") for c in calls), [c["auth"] for c in calls])
    check("失败号被标记冷却",
          pool.get("bad").last_error != "" and pool.get("bad").status == "cooling",
          (pool.get("bad").last_error, pool.get("bad").status))
    check("key 已记账 0.5 积分", abs(key_obj().credit_used - 0.5) < 1e-6, key_obj().credit_used)

    print("[smoke] 流式对话")
    chunks: list[bytes] = []
    # 换会话标识：多轮工具调用要落在同一个号上，这里也顺便验证粘性不串号。
    time.sleep(0.2)
    with httpx.stream("POST", base + "/v1/chat/completions", headers=headers, timeout=10, trust_env=False,
                      json={"messages": [{"role": "user", "content": "hi"}], "stream": True,
                            "conversation_id": "conv-1"}) as resp:
        check("流式 200", resp.status_code == 200, resp.status_code)
        check("content-type 是 SSE", "text/event-stream" in resp.headers.get("content-type", ""))
        for chunk in resp.iter_bytes():
            chunks.append(chunk)
    raw = b"".join(chunks)
    check("流式包含 data: 事件", raw.count(b"data:") >= 3, raw[:200])
    check("流式以 [DONE] 结束", raw.rstrip().endswith(b"[DONE]"), raw[-40:])
    check("中文内容未被转义破坏", "你".encode() in raw, raw[:200])
    check("流式用量已入账", abs(key_obj().credit_used - 1.2) < 1e-6, key_obj().credit_used)

    print("[smoke] Responses 协议（Codex / ChatGPT 桌面端）")
    # codex 只认 /v1/responses：请求体用 input，响应体要有 output 数组。
    time.sleep(0.2)
    before = key_obj().credit_used
    r = httpx.post(base + "/v1/responses", headers=headers, timeout=10, trust_env=False,
                   json={"model": "glm-5.3-flash",
                         "input": [{"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "hi"}]}]})
    check("Responses 非流式 200", r.status_code == 200, r.text)
    payload = r.json()
    check("object 是 response", payload.get("object") == "response", payload.get("object"))
    check("output 含 assistant 文本",
          any(item.get("type") == "message"
              and any(part.get("text") == "你好" for part in item.get("content", []))
              for item in payload.get("output", [])),
          payload.get("output"))
    usage = payload.get("usage", {})
    check("usage 字段已改名",
          "input_tokens" in usage and "output_tokens" in usage and "prompt_tokens" not in usage,
          usage)
    check("usage 数值与上游一致",
          usage.get("total_tokens")
          == usage.get("input_tokens", 0) + usage.get("output_tokens", 0), usage)
    check("Responses 也记账", abs(key_obj().credit_used - before - 0.5) < 1e-6, key_obj().credit_used)

    # 配了 AC2API_DEFAULT_MODEL 时缺 model 应自动补齐（这里 settings 就配了），
    # 这是刻意的：codex 某些子命令不带 model，补默认值比直接 400 好用。
    r = httpx.post(base + "/v1/responses", headers=headers, timeout=10, trust_env=False,
                   json={"input": "hi"})
    check("缺 model 时补默认值", r.status_code == 200
          and r.json().get("model") == "glm-5.3-flash", r.text[:200])

    r = httpx.post(base + "/v1/responses", headers=headers, timeout=10, trust_env=False,
                   json={"model": "glm-5.3-flash", "input": []})
    check("空 input 返回 400", r.status_code == 400, r.text)

    # 流式：上游给 chat SSE，网关要翻成 Responses 事件序列
    events: list[bytes] = []
    with httpx.stream("POST", base + "/v1/responses", headers=headers, timeout=10, trust_env=False,
                      json={"model": "glm-5.3-flash", "stream": True,
                            "input": [{"type": "message", "role": "user",
                                       "content": [{"type": "input_text", "text": "hi"}]}]}) as resp:
        check("Responses 流式 200", resp.status_code == 200, resp.status_code)
        check("Responses 流式是 SSE", "text/event-stream" in resp.headers.get("content-type", ""))
        for chunk in resp.iter_bytes():
            events.append(chunk)
    raw_events = b"".join(events)
    check("有 response.created", b"event: response.created" in raw_events, raw_events[:200])
    check("有 output_text.delta", b"event: response.output_text.delta" in raw_events, raw_events[:400])
    check("有 response.completed", b"event: response.completed" in raw_events, raw_events[-300:])
    check("delta 里中文未损坏", "你".encode() in raw_events, raw_events[:400])
    check("事件序号从 0 递增", b'"sequence_number": 0' in raw_events, raw_events[:200])

    print("[smoke] balance / 配额")
    # Responses 那两轮（非流式 0.5 + 缺 model 自动补 0.5 + 流式 0.7）也要算进去，
    # 所以基准值不再写死 1.2，而是从簿记里取。
    spent = key_obj().credit_used
    r = httpx.get(base + "/v1/balance", headers=headers, timeout=5, trust_env=False)
    check("/v1/balance 200", r.status_code == 200, r.text)
    check("balance 反映本地用量", abs(r.json()["credit_used"] - spent) < 1e-6, r.json())
    check("/user/balance 兼容", httpx.get(base + "/user/balance", headers=headers, timeout=5, trust_env=False).status_code == 200)

    key_obj().credit_used = key_obj().credit_limit
    r = httpx.post(base + "/v1/chat/completions", headers=headers, timeout=10, trust_env=False,
                   json={"messages": [{"role": "user", "content": "hi"}]})
    check("配额用尽返回 402", r.status_code == 402, r.status_code)
    key_obj().credit_used = spent

    print("[smoke] 积分口径端到端")
    # 走网关自己的上游积分查询，确认占位到期值被正确覆盖
    # 真实链路里账号令牌是带 "Bearer " 前缀的整串，这里照样模拟，
    # 否则不能验证网关不会把它拼成 "Bearer Bearer ..." 导致上游 401。
    summary = asyncio.run(gw.upstream.credits("Bearer good-token"))
    check("积分端点可解析", summary["total_remaining"] > 0, summary)
    check("占位到期被覆盖为 2 天", summary["soonest_days_left"] is not None and summary["soonest_days_left"] < 3,
          summary["soonest_days_left"])
    check("7 天内到期额度被识别", summary["expiring_soon_remaining"] > 0, summary)

    print("[smoke] 管理接口")
    r = httpx.get(base + "/admin", timeout=5, trust_env=False)
    check("/admin 页面 200", r.status_code == 200 and "ac2api" in r.text, r.status_code)
    r = httpx.get(base + "/admin/api/overview", timeout=5, trust_env=False)
    check("overview 含账号与日志", r.status_code == 200 and len(r.json()["accounts"]) == 2, r.status_code)
    r = httpx.post(base + "/admin/api/keys", json={"name": "from-admin", "credit_limit": 5}, timeout=5, trust_env=False)
    check("通过管理台生成 key", r.status_code == 200 and r.json()["api_key"].startswith("ac2api-"), r.text)
    new_key = r.json()["api_key"]
    check("新 key 立即可用",
          httpx.get(base + "/v1/balance", headers={"Authorization": f"Bearer {new_key}"}, timeout=5, trust_env=False).status_code == 200)
    r = httpx.post(base + "/admin/api/refresh", timeout=10, trust_env=False)
    check("刷新积分成功", r.status_code == 200, r.text)

    print("[smoke] key 跨重启持久化")
    # 之前的坑：key 只在内存里，网关一重启，Codex / CC Switch 里填的 key 全变成
    # 401。这里用「同一份磁盘文件 + 全新的 Gateway」模拟重启。
    tmpdir = tempfile.mkdtemp(prefix="ac2api-smoke-")
    try:
        keys_path = os.path.join(tmpdir, "keys.json")
        settings2 = replace(settings, discover=False)
        # 第一套：独立临时目录，别污染用户真实的 ~/.ac2api/keys.json
        gw_a = Gateway(settings=settings2, pool=AccountPool(accounts=[good]),
                       upstream=UpstreamClient(settings2), store=KeyStore(keys_path))
        app_a = create_app(settings=settings2, gateway=gw_a, key_store=KeyStore(keys_path))
        port_a = free_port()
        serve(app_a, port_a)
        base_a = f"http://127.0.0.1:{port_a}"
        wait_http(base_a + "/health")
        r = httpx.post(base_a + "/admin/api/keys", json={"name": "persist", "credit_limit": 20},
                       timeout=5, trust_env=False)
        check("重启前建 key", r.status_code == 200, r.text)
        persisted = r.json()["api_key"]
        hdr = {"Authorization": f"Bearer {persisted}"}
        check("重启前 key 可用",
              httpx.get(base_a + "/v1/balance", headers=hdr, timeout=5, trust_env=False).status_code == 200)
        check("落盘文件存在", os.path.exists(keys_path), keys_path)
        with open(keys_path, encoding="utf-8") as fh:
            on_disk = fh.read()
        check("落盘不含明文 key", persisted not in on_disk)

        # 「重启」：同路径新建一套对象（模拟进程重启后重新加载）
        gw_b = Gateway(settings=settings2, pool=AccountPool(accounts=[good]),
                       upstream=UpstreamClient(settings2), store=KeyStore(keys_path))
        app_b = create_app(settings=settings2, gateway=gw_b, key_store=KeyStore(keys_path))
        port_b = free_port()
        serve(app_b, port_b)
        base_b = f"http://127.0.0.1:{port_b}"
        wait_http(base_b + "/health")
        r = httpx.get(base_b + "/v1/balance", headers=hdr, timeout=5, trust_env=False)
        check("重启后同一 key 仍可用", r.status_code == 200, r.text[:200])
        check("重启后配额仍在", r.status_code == 200 and r.json()["credit_limit"] == 20, r.text[:200])
        check("重启后旧 key 未串号",
              httpx.get(base_b + "/v1/balance", headers=headers, timeout=5,
                        trust_env=False).status_code == 401)

        # 回归：反复保存不该在磁盘上留下重复行
        gw_b.flush_usage()
        gw_b.flush_usage()
        with open(keys_path, encoding="utf-8") as fh:
            rows = json.load(fh)["keys"]
        hashes = [row["key_hash"] for row in rows]
        check("落盘无重复行", len(hashes) == len(set(hashes)), hashes)
        check("重启后 key 数不变", len(rows) == 1, rows)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("[smoke] 测试隔离")
    after = real_keys_file.read_bytes() if real_keys_file.exists() else None
    check("没有污染真实 key 文件", after == real_keys_before, real_keys_file)
    shutil.rmtree(tmp_root, ignore_errors=True)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print("冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
