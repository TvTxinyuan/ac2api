"""积分（AutoClaw credits）口径：单包归一化 / 账号级汇总 / 紧迫度排序。

为什么单独成模块
----------------
「一个积分包值多少、什么时候到期」如果散落在查询、展示、调度三处，就一定会
出现三套口径：页面上说 30 天内算快过期、调度按 7 天算、有的包到期时间取错
导致永远不会被优先消耗。本模块把这条口径收拢成唯一一份**纯函数**实现，
网关调度、/v1/balance、管理后台全部消费同一份结果。

到期时间的三条经验规则（照搬 wb2api，来自对上游真实返回的核对）
------------------------------------------------------------
1. 上游同时给 `DeductionEndTime`（毫秒）和 `CycleEndTime`（周期结束）。
   某些「版本基础用量」包的 `DeductionEndTime` 是一个 2034 年的**占位值**，
   真正的周期结束在 `CycleEndTime`。只读前者会把包判成永不过期，
   剩余额度永远进不了「快过期」统计，选号也就永远轮不到它 —— 到期整包作废。
   判据：前者比后者晚超过 ``EXPIRY_CYCLE_OVERRIDE_DAYS`` 天 → 改用后者。
2. 解析出的到期时间距 now 超过 ``FAR_FUTURE_EXPIRY_DAYS`` 天 → 视为长期有效（None），
   避免 2034/2049 这类占位值流到前端被当成真实到期日。
3. 剩余额度取**当前周期**剩余（`CycleCapacityRemain*`）而不是账号层累积剩余，
   否则体验版用完后仍显示满额，调度切过去才发现用不了。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

#: 「快过期」窗口（天）。官方客户端在套餐页把 7 天内到期的额度标成橙色，
#: 调度也按这个窗口抢先消耗 —— 两个地方必须是同一个数字。
EXPIRING_SOON_DAYS = 7

#: `DeductionEndTime` 比 `CycleEndTime` 晚超过该天数时，视前者为长期占位值。
EXPIRY_CYCLE_OVERRIDE_DAYS = 365

#: 解析出的到期时间超过该天数即视为「长期有效」。
FAR_FUTURE_EXPIRY_DAYS = 730

#: 上游计费时钟是北京时间；无时区的时间串按 +08:00 解释，
#: 否则部署在 UTC 的服务器上算出来的剩余天数会整体偏一天。
_CN_TZ = timezone(timedelta(hours=8))


def now_ms() -> int:
    """当前时间（毫秒）。集中一个入口，便于测试注入。"""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------
def parse_timestamp_ms(value: Any) -> int | None:
    """把上游各种形态的时间解析成毫秒时间戳；无法解析返回 None。

    上游同一个语义字段可能是：毫秒戳、秒戳、`"2026-09-30 23:59:59"`、
    `"%Y-%m-%d"` 纯日期，或未过期时的空串（**必须**判成 None 而不是 0）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return None
        # 10 位以内是秒，13 位是毫秒
        return int(round(n * 1000)) if abs(n) < 10_000_000_000 else int(round(n))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return parse_timestamp_ms(float(text))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if len(text) == 10:
            dt = dt.replace(hour=23, minute=59, second=59)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_CN_TZ)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.replace(tzinfo=_CN_TZ).timestamp() * 1000)
    return None


def _first(raw: dict, keys: Iterable[str]) -> Any:
    """按优先级取第一个「有值」的字段（空串视为没有值）。"""
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def resolve_expire_at(raw: dict, now: int | None = None) -> int | None:
    """解析一个资源包的到期时间（毫秒）；长期有效返回 None。

    兼容两套上游字段：

    * 老的智谱商业化网关：`DeductionEndTime` / `CycleEndTime`（两套时间要择一，
      见 `EXPIRY_CYCLE_OVERRIDE_DAYS`）
    * AutoClaw 真实钱包实例（`/agent-assetmgr/api/v1/wallet-instances`）：
      `expires_at` / `effective_at`（RFC3339 带时区，可直接解析）
    """
    now = now if now is not None else now_ms()
    deduction_end = parse_timestamp_ms(_first(raw, ("DeductionEndTime", "deductionEndTime", "ExpiredTime", "expiredTime")))
    cycle_end = parse_timestamp_ms(_first(raw, ("CycleEndTime", "cycleEndTime")))
    wallet_end = parse_timestamp_ms(_first(raw, ("expires_at", "expiresAt", "expire_at", "expireAt", "expired_at")))

    if deduction_end is not None and cycle_end is not None:
        override_ms = EXPIRY_CYCLE_OVERRIDE_DAYS * 86400 * 1000
        expire_at = cycle_end if deduction_end - cycle_end > override_ms else deduction_end
    else:
        expire_at = deduction_end if deduction_end is not None else cycle_end
    if expire_at is None:
        expire_at = wallet_end

    if expire_at is None:
        return None
    if expire_at - now > FAR_FUTURE_EXPIRY_DAYS * 86400 * 1000:
        return None
    return expire_at


def _number(value: Any) -> float | None:
    """宽松取数：上游把容量写成字符串 `"459.49000428"` 很常见。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _first_number(raw: dict, keys: Iterable[str]) -> float | None:
    for key in keys:
        n = _number(raw.get(key))
        if n is not None:
            return n
    return None


#: 槽位优先级：精确值优先于整数近似值。上游对同一语义给了两套字段，
#: 用整数版会出现「已用 + 剩余 ≠ 总量」的缺口，展示和核对都会被误导。
_SIZE_KEYS = ("CycleCapacitySizePrecise", "CycleCapacitySize", "CycleTotalCapacity",
              "CapacitySizePrecise", "CapacitySize")
_REMAIN_KEYS = ("CycleCapacityRemainPrecise", "CycleCapacityRemain", "CycleRemainCapacity",
                "CapacityRemainPrecise", "CapacityRemain")
_USED_KEYS = ("CycleCapacityUsedPrecise", "CycleCapacityUsed", "CycleUsedCapacity",
              "CapacityUsedPrecise", "CapacityUsed")


# ---------------------------------------------------------------------------
# 单包归一化
# ---------------------------------------------------------------------------
def resource_summary(raw: dict, now: int | None = None) -> dict:
    """把一个上游资源包归一化成统一结构（展示 / 统计 / 调度共用）。"""
    now = now if now is not None else now_ms()

    slices = raw.get("SlicePeriodUsageDetails") or raw.get("slicePeriodUsageDetails") or []
    if isinstance(slices, list) and slices and isinstance(slices[0], dict):
        merged = {k: v for k, v in raw.items() if v not in (None, "")}
        merged.update(slices[0])
        raw = merged

    raw_total = _first_number(raw, _SIZE_KEYS)
    raw_remaining = _first_number(raw, _REMAIN_KEYS)
    raw_used = _first_number(raw, _USED_KEYS)

    # 三个数缺一个就用另外两个补，保证 total = remaining + used 自洽。
    if raw_total is None:
        if raw_remaining is not None and raw_used is not None:
            raw_total = raw_remaining + raw_used
        else:
            raw_total = raw_remaining if raw_remaining is not None else raw_used
    total = max(0.0, raw_total or 0.0)
    remaining = max(0.0, raw_remaining) if raw_remaining is not None else max(0.0, total - (raw_used or 0.0))
    used = max(0.0, raw_used) if raw_used is not None else max(0.0, total - remaining)

    expire_at = resolve_expire_at(raw, now)
    expired = expire_at is not None and expire_at <= now
    expiring_soon = expire_at is not None and now < expire_at <= now + EXPIRING_SOON_DAYS * 86400 * 1000
    days_left = max(0.0, (expire_at - now) / 86400000.0) if expire_at is not None else None

    package_code = str(_first(raw, ("PackageCode", "packageCode", "ProductId", "product_id")) or "")
    package_name = str(_first(raw, ("PackageName", "packageName", "Name", "name")) or "")

    return {
        "package_code": package_code,
        "package_name": package_name,
        "display_name": package_name or package_code or "积分包",
        "total": round(total, 4),
        "remaining": round(remaining, 4),
        "used": round(used, 4),
        "status": _first(raw, ("Status", "status")),
        "expire_at": expire_at,
        "expired": expired,
        "expiring_soon": expiring_soon,
        "days_left": round(days_left, 2) if days_left is not None else None,
        "days_left_label": days_left_label(days_left),
        "expire_at_label": format_expire_at(expire_at),
    }


# ---------------------------------------------------------------------------
# 账号级汇总
# ---------------------------------------------------------------------------
def summarize_account(resources: list[dict], now: int | None = None) -> dict:
    """把逐包明细汇总成账号级到期视图。

    * `soonest_expire_at` 只在**还有剩余额度**的包里取最早 —— 已经用完的包即使
      明天到期也没有可抢救的额度，把它算进来会让调度白挑一个号。
    * `expiring_soon_remaining` 是 7 天内到期的包剩余额度合计，也就是
      「再不抢用就会作废」的那部分积分 —— 这是调度的核心依据。
    * `expired_remaining` 是账面已过期但仍有剩余的额度，单独列出便于对账，
      不计入可抢救额度。
    """
    now = now if now is not None else now_ms()
    total_capacity = total_remaining = expiring_soon_remaining = expired_remaining = 0.0
    soonest_expire_at: int | None = None
    expiring_soon = expired = False

    for item in resources or []:
        remaining = float(item.get("remaining") or 0.0)
        total_capacity += float(item.get("total") or 0.0)
        total_remaining += remaining
        if item.get("expiring_soon"):
            expiring_soon_remaining += remaining
            if remaining > 0:
                expiring_soon = True
        if item.get("expired"):
            expired_remaining += remaining
            if remaining > 0:
                expired = True
        if remaining > 0 and item.get("expire_at"):
            ts = int(item["expire_at"])
            if soonest_expire_at is None or ts < soonest_expire_at:
                soonest_expire_at = ts

    days_left = max(0.0, (soonest_expire_at - now) / 86400000.0) if soonest_expire_at else None

    # 按到期时间升序（无到期的排最后）：既是「最近快到期」的展示顺序，
    # 也是调度读取优先级的顺序 —— 两处必须是同一份排序。
    ordered = sorted(
        (item for item in (resources or []) if float(item.get("remaining") or 0) > 0),
        key=lambda item: (item.get("expire_at") is None, item.get("expire_at") or 0),
    )

    return {
        "total_capacity": round(total_capacity, 4),
        "total_remaining": round(total_remaining, 4),
        "expiring_soon_remaining": round(expiring_soon_remaining, 4),
        "expired_remaining": round(expired_remaining, 4),
        "soonest_expire_at": soonest_expire_at,
        "soonest_days_left": round(days_left, 2) if days_left is not None else None,
        "expiring_soon": expiring_soon,
        "expired": expired,
        "package_count": len(resources or []),
        "active_package_count": len(ordered),
        "resources": ordered,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# AutoClaw 真实钱包（/agent-assetmgr/*）
# ---------------------------------------------------------------------------
#: 钱包实例里 `public_wallet_type` -> 展示名。上游的 `display_name` 已经是中文，
#: 这里只在它缺失时兜底。
_WALLET_TYPE_LABEL = {
    "reward": "奖励积分",
    "daily": "每日额度",
    "subscription": "订阅额度",
    "booster": "加油包",
    "other": "其他",
}


def wallet_instance_summary(raw: dict, now: int | None = None) -> dict:
    """把一个钱包实例（`wallet_instances[]` 的一项）归一化成资源包结构。

    真实字段：`wallet_id` / `wallet_scope` / `public_wallet_type` / `balance` /
    `effective_at` / `expires_at` / `status` / `cycle_key`。钱包没有「总量」概念，
    只有当前余额，所以 total == remaining、used == 0 —— 这不影响到期感知调度，
    因为调度只用到期时间和剩余量。
    """
    now = now if now is not None else now_ms()
    balance = _number(_first(raw, ("balance", "Balance", "remaining", "remaining_points"))) or 0.0
    wallet_type = str(_first(raw, ("public_wallet_type", "wallet_type", "type")) or "")
    display = str(_first(raw, ("display_name", "displayName", "name")) or "") or _WALLET_TYPE_LABEL.get(wallet_type, wallet_type or "积分")

    expire_at = resolve_expire_at(raw, now)
    expired = expire_at is not None and expire_at <= now
    expiring_soon = expire_at is not None and now < expire_at <= now + EXPIRING_SOON_DAYS * 86400 * 1000
    days_left = max(0.0, (expire_at - now) / 86400000.0) if expire_at is not None else None
    status = str(_first(raw, ("status", "Status")) or "")

    return {
        "package_code": str(_first(raw, ("wallet_id", "walletId", "cycle_key")) or ""),
        "package_name": display,
        "display_name": display,
        "wallet_type": wallet_type,
        "wallet_scope": str(_first(raw, ("wallet_scope", "walletScope")) or ""),
        "total": round(balance, 4),
        "remaining": round(balance, 4),
        "used": 0.0,
        "status": status,
        "expire_at": expire_at,
        "expired": expired,
        "expiring_soon": expiring_soon,
        "days_left": round(days_left, 2) if days_left is not None else None,
        "days_left_label": days_left_label(days_left),
        "expire_at_label": format_expire_at(expire_at),
    }


def summarize_wallet(wallets: Any, instances: Any, now: int | None = None) -> dict:
    """把 `/wallets` + `/wallet-instances` 两个响应合并成账号级积分视图。

    * 有实例明细时以实例为准（**只有它带 `expires_at`**），顺带补上各钱包类型
      的总额（`wallets[]` 里 balance 为 0 的类型不列进 resources，避免调度以为
      有余额）。
    * 没有实例明细时退回 `/wallets` 的总额视图：只有一个「无到期时间」的包。
    """
    now = now if now is not None else now_ms()
    resources: list[dict] = []

    inst_list = _wallet_list(instances)
    for item in inst_list:
        summary = wallet_instance_summary(item, now)
        if summary["remaining"] > 0 or summary["expire_at"] is not None:
            resources.append(summary)

    total_balance = _wallet_total(wallets)
    if not resources and total_balance:
        resources.append({
            "package_code": "wallet-total",
            "package_name": "积分总余额",
            "display_name": "积分总余额",
            "wallet_type": "all",
            "wallet_scope": "all",
            "total": round(total_balance, 4),
            "remaining": round(total_balance, 4),
            "used": 0.0,
            "status": "active",
            "expire_at": None,
            "expired": False,
            "expiring_soon": False,
            "days_left": None,
            "days_left_label": days_left_label(None),
            "expire_at_label": format_expire_at(None),
        })

    summary = summarize_account(resources, now)
    # /wallets 的 total_balance 是上游权威口径，直接用它覆盖合计更可信。
    if total_balance is not None:
        summary["total_remaining"] = round(total_balance, 4)
        summary["total_capacity"] = round(max(total_balance, summary["total_capacity"]), 4)
    summary["wallets"] = _wallet_breakdown(wallets)
    return summary


def _wallet_list(payload: Any) -> list[dict]:
    """从 `/wallet-instances` 的响应里取出实例数组（容忍多种包装）。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("wallet_instances", "walletInstances", "instances", "list", "items"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _wallet_total(payload: Any) -> float | None:
    """从 `/wallets` 的响应里取 total_balance。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    return _number(_first(data, ("total_balance", "totalBalance", "balance")))


def _wallet_breakdown(payload: Any) -> list[dict]:
    """`/wallets` 里各钱包类型的余额，给后台展示用。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = data.get("wallets") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "type": str(_first(row, ("public_wallet_type", "wallet_type", "type")) or ""),
            "name": str(_first(row, ("display_name", "displayName", "name")) or ""),
            "balance": _number(_first(row, ("balance", "Balance"))) or 0.0,
        })
    return out


def urgency_rank(summary: dict | None) -> tuple:
    """调度的紧迫度排序键：**越小越先被选**。

      0 —— 有 7 天内到期的剩余额度（最紧急，必须优先烧掉，否则作废）
      1 —— 有过期时间但还早（先消耗离到期近的，把长期包留到最后）
      2 —— 全部长期有效（没有作废风险，只用它兜底）

    同级内按 `soonest_expire_at` 升序；无到期时间的排最后。这样即使所有号
    都很宽裕，也会自然形成「离到期近的先走」的稳定偏好。
    """
    if not summary:
        return (2, float("inf"))
    if summary.get("expiring_soon") and float(summary.get("expiring_soon_remaining") or 0) > 0:
        return (0, summary.get("soonest_expire_at") or float("inf"))
    soonest = summary.get("soonest_expire_at")
    if soonest:
        return (1, soonest)
    return (2, float("inf"))


def extract_resources(payload: Any) -> list[dict]:
    """从上游各种形状的响应里捞出「资源包数组」。

    上游把包列表藏在 `data.resource_package_list` / `data.list` /
    `data.packages` / `data.data` 等位置，且不同版本字段名不一致。
    这里做一次**深度优先**的宽口径提取：找到第一个「dict 数组，且其中元素
    含容量类字段」就返回，找不到返回空数组。
    """
    capacity_keys = set(_SIZE_KEYS) | set(_REMAIN_KEYS) | set(_USED_KEYS) | {
        "CapacityUnit", "capacity_unit", "CapacityRemain", "capacity_remain",
    }

    def looks_like_packages(value: Any) -> bool:
        if not isinstance(value, list) or not value:
            return False
        if not all(isinstance(item, dict) for item in value):
            return False
        return any(capacity_keys & set(item.keys()) for item in value[:5])

    if looks_like_packages(payload):
        return list(payload)
    if isinstance(payload, dict):
        for key in ("resource_package_list", "ResourcePackageList", "package_list",
                    "packages", "list", "resources", "credit_list", "score_list", "items"):
            if looks_like_packages(payload.get(key)):
                return list(payload[key])
        for value in payload.values():
            found = extract_resources(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = extract_resources(item)
            if found:
                return found
    return []


def days_left_label(days_left: float | None) -> str:
    """到期天数的中文短标签（展示层共用，避免各处自己拼）。"""
    if days_left is None:
        return "长期有效"
    if days_left <= 0:
        return "已到期"
    if days_left < 1:
        return f"{max(1, int(round(days_left * 24)))} 小时后到期"
    return f"{int(days_left)} 天后到期"


def format_expire_at(expire_at: int | None) -> str:
    """到期时间戳 → 本地可读字符串（无到期返回「长期有效」）。"""
    if not expire_at:
        return "长期有效"
    try:
        return datetime.fromtimestamp(expire_at / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "长期有效"
