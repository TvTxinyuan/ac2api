"""本地 API Key：签发、校验、额度与用量簿记。

为什么 key 明文不落库
--------------------
和上游 token 一样，泄露一份 key 等于把积分白送。这里只存
``sha256(pepper + key)``：校验时用哈希比对，数据库被拖走也换不回可用的 key。
key 明文只在创建那一次返回给用户（``ac2api-`` 前缀，便于在日志/代码里被
secret scanner 认出来）。

额度控制分两层，两层的失败语义不能混：
* **credit_limit**（本地配额）：该 key 一共能用多少「积分额度」。打满后返回
  ``402 quota_exceeded`` —— 这是**你自己的账**，换上游账号也不会变。
* **rate_limit**（速率）：每分钟多少请求。超了返回 429，等一会儿就好。
"""
from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
from dataclasses import dataclass, field

#: key 明文前缀。
KEY_PREFIX = "ac2api-"

#: 上游返回的 usage 里这些字段名都代表本次消耗的积分/额度。
CREDIT_USAGE_KEYS = (
    "total_tokens", "prompt_tokens", "completion_tokens",
    "total_credit", "totalCredit", "credit", "credits", "score", "consume",
)

#: 多少 token 折 1 积分。实测（4 次真实对话、423 token、扣 4 积分）约 100，
#: 且上游对每次调用至少扣 1 积分。改这个值只影响本地额度记账口径。
TOKENS_PER_CREDIT = 100.0


def generate_key() -> str:
    """生成一个新的 API Key 明文（仅此一次可见）。"""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str, pepper: str = "") -> str:
    return hashlib.sha256((pepper + key).encode("utf-8")).hexdigest()


def mask_key(key: str) -> str:
    """日志里只打前 12 位，避免泄漏。"""
    return key[:12] + "…" if len(key) > 12 else key


@dataclass
class ApiKey:
    """一个客户端 key 的运行时状态。

    ``credit_used`` 是本 key 已经消耗的积分额度；``credit_limit`` 为 None 表示
    不限量。两者都在**请求成功后**累加，失败不扣 —— 跟用户看到的账单一致。
    """

    name: str
    key_hash: str
    credit_limit: float | None = None
    rate_limit: int = 60                      # 每分钟请求数
    status: str = "active"                    # active / disabled
    credit_used: float = 0.0
    request_count: int = 0
    token_used: int = 0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    _window_start: float = field(default=0.0, repr=False)
    _window_count: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def credential(self) -> str:
        """`/v1/balance` 里展示的凭据脱敏串。"""
        return self.key_hash[:8] + "…"

    def check_rate(self, now: float | None = None) -> bool:
        """滑动 60 秒窗口内的速率检查；返回 False 表示应拒绝。"""
        now = now if now is not None else time.time()
        with self._lock:
            if now - self._window_start >= 60:
                self._window_start = now
                self._window_count = 0
            if self._window_count >= self.rate_limit:
                return False
            self._window_count += 1
            return True

    def has_quota(self) -> bool:
        if self.credit_limit is None:
            return True
        return self.credit_used < self.credit_limit

    def record_usage(self, *, credits: float = 0.0, tokens: int = 0) -> None:
        with self._lock:
            self.credit_used = round(self.credit_used + max(0.0, credits), 4)
            self.token_used += max(0, int(tokens))
            self.request_count += 1
            self.last_used_at = time.time()

    def to_record(self) -> dict:
        """落盘用。**只写哈希，不写明文 key** —— 明文在创建时返给用户一次就没了。"""
        return {
            "name": self.name,
            "key_hash": self.key_hash,
            "credit_limit": self.credit_limit,
            "rate_limit": self.rate_limit,
            "status": self.status,
            "credit_used": self.credit_used,
            "request_count": self.request_count,
            "token_used": self.token_used,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    def public(self) -> dict:
        return {
            "name": self.name,
            "credential": self.credential,
            "status": self.status,
            "credit_limit": self.credit_limit,
            "credit_used": self.credit_used,
            "credit_remaining": None if self.credit_limit is None else round(max(0.0, self.credit_limit - self.credit_used), 4),
            "rate_limit": self.rate_limit,
            "request_count": self.request_count,
            "token_used": self.token_used,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


def usage_credits(usage: dict | None, *, tokens_per_credit: float = TOKENS_PER_CREDIT) -> float:
    """从上游 usage 里取「本次消耗的积分」。

    AutoClaw 的 managed 网关**不返回积分字段**，只给 token 数（实测 usage 只有
    prompt/completion/total_tokens 三组，外加 details）。而它的计费口径也不是
    token 1:1，实测连续 4 次对话共 423 token 扣了 4 积分，且每次至少扣 1 积分
    （含 reasoning token 的那次也一样），也就是约 100 token 折 1 积分、向上取整。

    所以这里的口径是：
    1. 上游哪天真的给了积分字段 → 直接用（``total_credit`` 等）；
    2. 否则按 ``TOKENS_PER_CREDIT`` 折算并**向上取整**，且不低于 1 积分。

    口径写死在一处，免得本地配额和真实消耗对不上。
    """
    if not isinstance(usage, dict):
        return 0.0
    for key in ("total_credit", "totalCredit", "credit", "credits", "score", "consume"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    total = usage.get("total_tokens")
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        return 0.0
    rate = tokens_per_credit if tokens_per_credit and tokens_per_credit > 0 else TOKENS_PER_CREDIT
    return float(max(1, math.ceil(float(total) / rate)))


@dataclass
class UsageRecord:
    """一条请求日志（内存环形缓冲，重启即清）。"""

    ts: float
    key_name: str
    account_key: str
    model: str
    stream: bool
    status: str                  # ok / error
    latency_ms: int
    credits: float = 0.0
    tokens: int = 0
    error: str = ""
    client_ip: str = ""

    def public(self) -> dict:
        return {
            "ts": self.ts, "key_name": self.key_name, "account_key": self.account_key,
            "model": self.model, "stream": self.stream, "status": self.status,
            "latency_ms": self.latency_ms, "credits": self.credits, "tokens": self.tokens,
            "error": self.error, "client_ip": self.client_ip,
        }
