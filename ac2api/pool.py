"""账号池：在途租约 / 会话粘性 / 三因子加权选号 / 失败退避。

设计要点（为什么不是一个 `random.choice`）
------------------------------------------
积分是有**保质期**的，所以「平均分摊」是错的调度目标 —— 那会让每个号都
在临近到期时剩一堆额度，然后一起作废。正确的目标是：**先把最快要作废的
积分烧掉**，长期有效的额度留到最后兜底。

选号按三个因子加权打分，取前 N 个里随机一个（TOP_N + 随机，既保证优先级
又不至于把所有并发都钉在同一个号上）：

    score = CREDITS_WEIGHT * 剩余额度(万)
          + EXPIRING_WEIGHT * (1 - min(1, 剩余天数/7))   # 越快到期越高分
          + min(IDLE_WEIGHT_CAP, IDLE_WEIGHT_PER_HOUR * 空闲小时数)

另外三件事必须在这里做，放在调用方就会各写一遍、各写错一遍：

1. **在途租约**：一个号被并发请求占用时不能再被选中，否则会话粘性失效，
   上游可能按「同一会话并发」判风控。
2. **会话粘性**：同一个 session 在 TTL 内尽量落同一个号，多轮工具调用才
   不会上下文错位。
3. **防撞号窗口**：极短时间（默认 100ms）内不重复选同一个号，避免上游
   把「同一账号高频并发」当成脚本；被连续判定风控的 IP 直接 fail-fast。
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

from .credits import urgency_rank

#: 剩余额度权重（单位：万积分）。乘以 1e4 前的系数，调大更偏「额度多的号」。
CREDITS_WEIGHT = 10.0
#: 快到期权重。
EXPIRING_WEIGHT = 8.0
#: 每小时空闲奖励（上限 IDLE_WEIGHT_CAP），用于把请求摊到冷号上。
IDLE_WEIGHT_PER_HOUR = 0.5
IDLE_WEIGHT_CAP = 5.0
#: 参与随机化的候选数：排序后只在这 N 个里挑。
TOP_N = 5
#: 同一账号的最小选取间隔（秒），防撞号。
MIN_PICK_GAP_S = 0.1
#: 会话粘性 TTL（秒）。
SESSION_TTL_S = 30 * 60
#: 风控 IP fail-fast 窗口与阈值。
WAF_IP_WINDOW_S = 60
WAF_IP_THRESHOLD = 2
#: 轮转退避：500ms * 2^n，封顶 8s，±25% 抖动。
ROTATE_BACKOFF_BASE_S = 0.5
ROTATE_BACKOFF_CAP_S = 8.0
ROTATE_BACKOFF_JITTER = 0.25


class NoAccountAvailable(Exception):
    """没有可用账号（全部在途 / 冷却 / 无剩余积分）。"""


class _AllBusy(Exception):
    """内部用：账号都在途（但本身是健康的），值得排队等一会儿 —— 不对外暴露。"""


@dataclass
class Account:
    """一个 AutoClaw 账号在池中的运行时状态。

    `key` 为账号稳定标识（本机账号目录名 / 登录 uid），用于日志与粘性键。
    """

    key: str
    token: str = ""
    nickname: str = ""
    email: str = ""
    enabled: bool = True
    summary: dict | None = None          # credits.summarize_account() 的结果
    last_used: float = 0.0               # 上次被选中的时间戳（秒），用于空闲奖励
    in_flight: int = 0                   # 在途请求数（租约）
    cooling_until: float = 0.0           # 巡检退避到该时刻（秒）
    last_error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def remaining(self) -> float:
        return float((self.summary or {}).get("total_remaining") or 0.0)

    @property
    def status(self) -> str:
        """面向后台与客户端的状态标签 —— 只此一处判定，别处不要重算。"""
        if not self.enabled:
            return "disabled"
        if self.cooling_until > time.time():
            return "cooling"
        if self.summary is not None and self.remaining <= 0:
            return "exhausted"
        if not self.token:
            return "no_token"
        return "active"

    def public(self) -> dict:
        s = self.summary or {}
        return {
            "key": self.key,
            "nickname": self.nickname,
            "email": self.email,
            "status": self.status,
            "in_flight": self.in_flight,
            "remaining": round(self.remaining, 2),
            "total_capacity": s.get("total_capacity"),
            "expiring_soon_remaining": s.get("expiring_soon_remaining"),
            "soonest_expire_at": s.get("soonest_expire_at"),
            "soonest_days_left": s.get("soonest_days_left"),
            "active_package_count": s.get("active_package_count"),
            "last_error": self.last_error,
            "last_used": self.last_used,
            "token_prefix": (self.token[:8] + "…") if self.token else "",
        }


class AccountPool:
    """线程安全的账号池。纯内存状态，进程重启后由巡检重新灌入积分。"""

    def __init__(self, *, accounts: Iterable[Account] = (), rng: random.Random | None = None):
        self._accounts: dict[str, Account] = {a.key: a for a in accounts}
        self._mutex = threading.RLock()
        self._sessions: dict[str, tuple[str, float]] = {}   # session_key -> (account_key, expire_at)
        self._last_pick: dict[str, float] = {}              # account_key -> 上次选中时刻
        self._waf_hits: dict[str, list[float]] = {}         # ip -> 命中时刻列表
        self._fail_streak: dict[str, int] = {}              # account_key -> 连续失败次数
        self._rng = rng or random.Random()

    # -- 账号维护 ---------------------------------------------------------
    def upsert(self, account: Account) -> Account:
        with self._mutex:
            existing = self._accounts.get(account.key)
            if existing is not None:
                # 保留运行时状态（在途、冷却、上次使用），只更新静态/积分类字段，
                # 否则每次巡检刷新都会把在途租约清零，造成同一账号被并发选中。
                existing.token = account.token or existing.token
                existing.nickname = account.nickname or existing.nickname
                existing.email = account.email or existing.email
                existing.enabled = account.enabled
                if account.summary is not None:
                    existing.summary = account.summary
                return existing
            self._accounts[account.key] = account
            return account

    def get(self, key: str) -> Account | None:
        with self._mutex:
            return self._accounts.get(key)

    def remove(self, key: str) -> bool:
        with self._mutex:
            return self._accounts.pop(key, None) is not None

    def all(self) -> list[Account]:
        with self._mutex:
            return list(self._accounts.values())

    def snapshot(self) -> list[dict]:
        return [a.public() for a in sorted(self.all(), key=lambda a: a.key)]

    # -- 选号 -------------------------------------------------------------
    def pick(self, *, session_key: str | None = None, exclude: Iterable[str] = (),
             wait_s: float = 0.0) -> Account:
        """按紧迫度 + 加权打分选一个账号并加租约。无可用账号抛 NoAccountAvailable。

        `wait_s > 0` 时，如果暂时没有空闲账号（都在途），会**排队等**直到有账号
        归还租约，最多等 `wait_s` 秒。只有一个账号时这是必须的：客户端并发发两个
        请求，第二个不能直接 503，否则 Codex 会看到「没有可用账号」。
        """
        deadline = time.time() + max(0.0, wait_s)
        while True:
            try:
                return self._pick_once(session_key=session_key, exclude=exclude)
            except _AllBusy:
                if time.time() >= deadline:
                    raise NoAccountAvailable(
                        "all accounts are busy (in-flight)")
            except NoAccountAvailable:
                # 真的没有可用号（禁用/冷却/积分耗尽），等待也没用
                raise
            time.sleep(0.05)

    def _pick_once(self, *, session_key: str | None = None,
                   exclude: Iterable[str] = ()) -> Account:
        """单次选号。只因为「在途」选不出来时抛 :class:`_AllBusy`，供外面排队。"""
        now = time.time()
        excluded = set(exclude)

        with self._mutex:
            candidates: list[tuple[float, float, Account]] = []
            busy = False
            sticky_key, sticky_exp = self._sessions.get(session_key, ("", 0.0)) if session_key else ("", 0.0)

            for acc in self._accounts.values():
                if acc.key in excluded or acc.status != "active":
                    continue
                if acc.in_flight > 0:
                    busy = True
                    continue
                if now - self._last_pick.get(acc.key, 0.0) < MIN_PICK_GAP_S:
                    busy = True
                    continue
                rank, soonest = urgency_rank(acc.summary)
                score = self._score(acc, now)
                candidates.append((rank, score, acc))

            if not candidates:
                if busy:
                    raise _AllBusy()
                raise NoAccountAvailable("no idle account with remaining credits")

            # 会话粘性优先：同 session 的旧账号若仍可用，且不在「明显更差」的
            # 紧迫度层级上，就继续用它（多轮工具调用不能换号）。
            if sticky_key and sticky_exp > now:
                for rank, score, acc in candidates:
                    if acc.key == sticky_key and rank <= 1:
                        return self._lease(acc, session_key, now)

            # 先按紧迫度分层（0 快过期 > 1 有到期 > 2 长期），层内按分数降序，
            # 再在前 TOP_N 里随机 —— 保证优先级稳定，又避免并发全压一个号。
            candidates.sort(key=lambda item: (item[0], -item[1]))
            best_rank = candidates[0][0]
            top = [item for item in candidates if item[0] == best_rank][:TOP_N]
            chosen = self._rng.choice(top)[2]
            return self._lease(chosen, session_key, now)

    def _score(self, acc: Account, now: float) -> float:
        remaining_wan = acc.remaining / 10000.0
        days = (acc.summary or {}).get("soonest_days_left")
        if days is None:
            urgency = 0.0
        else:
            urgency = 1.0 - min(1.0, max(0.0, float(days)) / 7.0)
        idle_h = (now - acc.last_used) / 3600.0 if acc.last_used else 0.0
        idle_bonus = min(IDLE_WEIGHT_CAP, IDLE_WEIGHT_PER_HOUR * idle_h)
        return CREDITS_WEIGHT * remaining_wan + EXPIRING_WEIGHT * urgency + idle_bonus

    def _lease(self, acc: Account, session_key: str | None, now: float) -> Account:
        acc.in_flight += 1
        acc.last_used = now
        self._last_pick[acc.key] = now
        if session_key:
            self._sessions[session_key] = (acc.key, now + SESSION_TTL_S)
        return acc

    def release(self, account: Account | str, *, success: bool = True, error: str = "",
                blame_account: bool = True) -> None:
        """归还租约；失败时记录连续失败并安排退避，成功则清零。

        `blame_account=False` 用于「账号本身没毛病、是请求被上游判不合规」的失败
        （典型是 406）。这类错误换号也没用，而且**冷却账号是有害的**：单账号池下
        冷却期内所有请求都会撞上 `NoAccountAvailable` 变成 503，把一个「请求格式
        问题」放大成「服务不可用」。所以只记 last_error，不动冷却与失败计数。

        租约永远会被归还（`in_flight` 必减），只有冷却与否受 `blame_account` 影响。
        """
        key = account if isinstance(account, str) else account.key
        with self._mutex:
            acc = self._accounts.get(key)
            if acc is None:
                return
            acc.in_flight = max(0, acc.in_flight - 1)
            if success:
                self._fail_streak[key] = 0
                acc.last_error = ""
                return
            acc.last_error = error[:300]
            if not blame_account:
                # 请求侧的问题：账号照常可用，不计入连续失败、不打冷却。
                return
            streak = self._fail_streak.get(key, 0) + 1
            self._fail_streak[key] = streak
            acc.cooling_until = time.time() + self.backoff_seconds(streak)

    @staticmethod
    def backoff_seconds(streak: int) -> float:
        """指数退避 + ±25% 抖动：退避带抖动才不会让一批号在同一秒集体复活。"""
        base = min(ROTATE_BACKOFF_CAP_S, ROTATE_BACKOFF_BASE_S * (2 ** max(0, streak - 1)))
        return base * (1.0 + random.uniform(-ROTATE_BACKOFF_JITTER, ROTATE_BACKOFF_JITTER))

    # -- 风控 fail-fast ---------------------------------------------------
    def note_waf(self, ip: str) -> bool:
        """记录一次风控命中；返回 True 表示该 IP 短期内应直接拒绝。"""
        now = time.time()
        with self._mutex:
            hits = [t for t in self._waf_hits.get(ip, []) if now - t < WAF_IP_WINDOW_S]
            hits.append(now)
            self._waf_hits[ip] = hits
            return len(hits) >= WAF_IP_THRESHOLD

    def waf_blocked(self, ip: str) -> bool:
        """只读判定，不改计数（给请求前置检查用）。"""
        now = time.time()
        with self._mutex:
            hits = [t for t in self._waf_hits.get(ip, []) if now - t < WAF_IP_WINDOW_S]
            return len(hits) >= WAF_IP_THRESHOLD

    # -- 统计 -------------------------------------------------------------
    def stats(self) -> dict:
        with self._mutex:
            accs = list(self._accounts.values())
            active = [a for a in accs if a.status == "active"]
            expiring = sorted(
                (a for a in active if (a.summary or {}).get("expiring_soon")),
                key=lambda a: a.summary.get("soonest_expire_at") or 0,
            )
            return {
                "accounts": len(accs),
                "active": len(active),
                "in_flight": sum(a.in_flight for a in accs),
                "total_remaining": round(sum(a.remaining for a in active), 2),
                "expiring_soon_remaining": round(
                    sum(float((a.summary or {}).get("expiring_soon_remaining") or 0) for a in active), 2
                ),
                "expiring_accounts": [a.key for a in expiring],
                "soonest": expiring[0].summary.get("soonest_expire_at") if expiring else None,
            }
