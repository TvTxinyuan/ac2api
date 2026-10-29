"""OpenAI 兼容网关：把 AutoClaw 的积分额度包装成标准 API。

对外暴露（与 wb2api 的能力边界对齐，先做 OpenAI 这一族）：
    GET  /health                 存活 + 池概况
    GET  /v1/models              模型列表
    GET  /v1/balance             本 key 的剩余额度
    POST /v1/chat/completions    ★ 主入口（Chat Completions，流式 / 非流式）
    POST /v1/responses           ★ Responses 协议入口（Codex CLI / ChatGPT 桌面端），
                                 内部翻译成 chat/completions 再走同一条链路

一次请求的完整生命周期（也是本文件的骨架）：
    key 校验 → 限速 → 本地配额 → 选号(租约) → 注入上游 → 转发
       → 成功: 记账 + 心跳刷新到期
       → 可重试失败: 归还并标记 → 换号重试（最多 N 次）
       → 不可重试失败: 原样回给调用方

几个不能省的细节：
* **SSE 要绝对透传**。上游给的字节流已经是 OpenAI 格式，我们只做「拼接 +
  记账」，绝不重新序列化 —— 重排 JSON 会改变字段顺序，某些客户端（以及
  一些严格的 SDK 校验）会因此失败；重新组装也可能把 `data: ` 的换行弄错，
  前端表现为「流式卡住不吐字」。
* **流式请求的配额也要扣**。流结束后从 `usage` 事件里取用量；上游没给
  usage 时就按字符数粗略估算，宁可少扣也别漏扣成免费。
* **长连接不设总超时**。只在 upstream 层设读空闲超时，见 upstream.Settings。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import keys as keysmod
from . import responses as respmod
from .keys import ApiKey, UsageRecord, hash_key, usage_credits
from .pool import Account, AccountPool, NoAccountAvailable
from .store import KeyStore
from .token import TokenSource, is_expired
from .upstream import (
    UpstreamAuthError,
    UpstreamClient,
    UpstreamError,
    UpstreamQuotaError,
    normalize_max_tokens,
    resolve_model_base_url,
)

#: 单个请求最多尝试几个账号（含首次）。
MAX_ACCOUNT_ATTEMPTS = 3
#: 没有空闲账号时最多排队等多久（秒）。只有一个账号时，客户端并发两个请求
#: 必然撞车，第二个要排队等前一个还回租约，而不是直接 503。
POOL_WAIT_S = 120.0
#: 请求日志保留条数。
LOG_CAPACITY = 500
#: 用量落盘的节流间隔（秒）。每次请求都 fsync 太浪费，攒一会儿再写。
USAGE_FLUSH_INTERVAL_S = 15.0


def _opt_float(value) -> float | None:
    """把可选数字字段转成 float；None / 空串 / 非法值都算"不限量"。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_error(status: int, message: str, *, code: str = "", type_: str = "invalid_request_error") -> JSONResponse:
    """OpenAI 风格的错误体。客户端 SDK 都按这个结构取 message。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "code": code or None}},
    )


def sid_from_request(body: dict, request: Request) -> str | None:
    """会话粘性键：优先用调用方给的稳定标识，退化为 IP。

    多轮工具调用必须落在同一个上游账号上，否则上下文会错位，
    某些客户端表现为「第二轮开始答非所问」。
    """
    for key in ("user", "session_id", "conversation_id", "prompt_cache_key",
                "previous_response_id", "safety_identifier"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}:{value.strip()}"
    client = request.client.host if request.client else ""
    return f"ip:{client}" if client else None


def sid_from_chat(messages: list) -> str | None:
    """从 chat 的 messages 里找会话粘性线索。

    Responses 协议本身不带窄会话 id（codex 每轮重发完整 input），所以我们用
    「首条消息的指纹」当粘性键：同一个对话的首条 prompt 不变，工具调用就会
    稳定落在一个上游账号上，多轮上下文不会错位。
    """
    import hashlib

    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") in ("system", "user"):
            content = msg.get("content")
            if isinstance(content, list):
                content = respmod.text_of(content)
            if isinstance(content, str) and content.strip():
                digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:16]
                return f"chat:{digest}"
    return None


class Gateway:
    """把配置、池、key 簿记、日志串起来的运行时对象。"""

    def __init__(self, *, settings, pool: AccountPool, upstream: UpstreamClient,
                 api_keys: dict[str, ApiKey] | None = None, key_pepper: str = "",
                 store=None):
        self.settings = settings
        self.pool = pool
        self.upstream = upstream
        self.api_keys: dict[str, ApiKey] = dict(api_keys or {})
        self.key_pepper = key_pepper
        self.logs: deque[UsageRecord] = deque(maxlen=LOG_CAPACITY)
        #: accessToken 来源（本机凭据 / refreshToken 续期 / 外部服务 / 手工填）。
        self.token_source = TokenSource(settings)
        # key 落盘：不给 store 就是纯内存（测试用），给了就重启不丢。
        self.store = store
        #: 上次把用量写盘的时间（秒）；用量节流见 ``note_usage``。
        self._last_flush = 0.0
        if store is not None:
            self._load_keys()

    # -- key 持久化 -------------------------------------------------------
    def _load_keys(self) -> None:
        """把磁盘上的 key 读回内存。同一哈希只留一条（文件被写重复时以最后一条为准）。"""
        for rec in self.store.load():
            try:
                key_hash = str(rec["key_hash"])
                item = ApiKey(
                    name=str(rec.get("name") or key_hash[:8]),
                    key_hash=key_hash,
                    credit_limit=_opt_float(rec.get("credit_limit")),
                    rate_limit=int(rec.get("rate_limit") or 60),
                    status=str(rec.get("status") or "active"),
                    credit_used=float(rec.get("credit_used") or 0.0),
                    request_count=int(rec.get("request_count") or 0),
                    token_used=int(rec.get("token_used") or 0),
                    created_at=float(rec.get("created_at") or time.time()),
                    last_used_at=float(rec.get("last_used_at") or 0.0),
                )
            except (TypeError, ValueError):
                # 单条坏记录不该拖垮整个加载，跳过即可。
                continue
            self.api_keys[key_hash] = item

    def save_keys(self) -> bool:
        if self.store is None:
            return False
        # 按 key_hash 去重：同一个哈希只留一条（内存里本不该有重复，重复只会
        # 出现在「磁盘上被写坏过、又被重新加载」的历史数据里）。
        seen: dict[str, dict] = {}
        for k in self.api_keys.values():
            seen[k.key_hash] = k.to_record()
        return self.store.save(list(seen.values()))

    def _touch_keys(self) -> None:
        """变更后落盘。用量这种高频字段不每次都写，见 ``flush_usage``。"""
        self.save_keys()

    # -- key --------------------------------------------------------------
    def create_key(self, name: str, *, credit_limit: float | None = None, rate_limit: int = 60) -> str:
        plaintext = keysmod.generate_key()
        digest = hash_key(plaintext, self.key_pepper)
        self.api_keys[digest] = ApiKey(
            name=name, key_hash=digest,
            credit_limit=credit_limit, rate_limit=rate_limit,
        )
        self._touch_keys()
        return plaintext

    def set_key_status(self, name: str, enabled: bool) -> bool:
        """按名字启停 key（管理台用）。找到并改了返回 True。"""
        changed = False
        for item in self.api_keys.values():
            if item.name == name:
                item.status = "active" if enabled else "disabled"
                changed = True
        if changed:
            self._touch_keys()
        return changed

    def flush_usage(self) -> None:
        """把用量计数落盘。

        每次请求都 fsync 太浪费，所以用量走「节流写」：``record_usage`` 之后
        由 ``note_usage`` 决定是否真的写盘；进程退出时 ``__main__`` 兜一次。
        """
        self._last_flush = time.time()
        self.save_keys()

    def note_usage(self) -> None:
        """记录完用量后调用：距上次写盘超过节流间隔才真正落盘。

        目的是「重启不丢额度」和「不把磁盘写爆」之间的折中：最坏情况下崩溃会
        丢最近 ``USAGE_FLUSH_INTERVAL_S`` 秒的用量计数（不是积分本身，积分在
        上游已扣）。
        """
        if self.store is None:
            return
        if time.time() - self._last_flush >= USAGE_FLUSH_INTERVAL_S:
            self.flush_usage()

    def authenticate(self, key: str | None) -> ApiKey | None:
        if not key:
            return None
        return self.api_keys.get(hash_key(key, self.key_pepper))

    def log(self, record: UsageRecord) -> None:
        self.logs.appendleft(record)

    # -- 上游账号 ---------------------------------------------------------
    def refresh_account_token(self, account: Account) -> str:
        """取一个可用 token：手填 / 外部服务 → 本机凭据 → refreshToken 续期 → 池里现存的。

        本机装了 AutoClaw 的话，`token.TokenSource` 会直接用客户端自己的
        refreshToken 去 `/userapi/v1/refresh` 换新 token（并回写客户端凭据），
        所以这里通常什么都不用配。管理台手工填的 token 始终优先。
        """
        if account.token and not self._token_stale(account.token):
            return account.token
        try:
            token = self.token_source.token_for(account.key or account.name)
        except Exception as err:  # noqa: BLE001 - 取 token 失败要能被上层换号
            token = ""
            if not account.token:
                raise UpstreamAuthError(
                    f"取不到 accessToken：{err}. 可安装 AutoClaw 客户端（网关会自动续期），"
                    "或在管理台填入 accessToken（亦可用 AC2API_TOKEN_URL / AC2API_STATIC_TOKEN）。"
                ) from err
        if token:
            account.token = token
            return token
        if not account.token:
            raise UpstreamAuthError(
                "该账号没有 accessToken：本机没找到 AutoClaw 凭据，也没有 refreshToken。"
                "请在管理台填入 accessToken，或配置 AC2API_TOKEN_URL / AC2API_STATIC_TOKEN。"
            )
        return account.token

    @staticmethod
    def _token_stale(token: str) -> bool:
        """JWT 能解出 exp 就按 exp 判过期；解不出（非 JWT）就当作没过期。"""
        try:
            return is_expired(token)
        except Exception:  # noqa: BLE001
            return False

    async def refresh_account_credits(self, account: Account) -> None:
        token = account.token or self.refresh_account_token(account)
        account.summary = await self.upstream.credits(token)

    async def refresh_all(self) -> None:
        """刷新所有启用账号的 token 与积分。"""

        async def one(acc: Account) -> None:
            try:
                await self.refresh_account_credits(acc)
                acc.last_error = ""
            except UpstreamAuthError:
                # token 可能只是本机刚轮换，重取一次再判死。
                try:
                    acc.token = ""
                    await self.refresh_account_credits(acc)
                    acc.last_error = ""
                except Exception as err:  # noqa: BLE001
                    acc.last_error = f"{type(err).__name__}: {err}"[:300]
            except Exception as err:  # noqa: BLE001
                acc.last_error = f"{type(err).__name__}: {err}"[:300]

        await asyncio.gather(*(one(acc) for acc in self.pool.all() if acc.enabled))

    # -- 转发 -------------------------------------------------------------
    def prepare_payload(self, body: dict) -> dict:
        """补齐上游需要的字段，去掉上游用不到、留着还会 400 的字段。

        上游就是 OpenAI 协议，绝大多数字段原样透传；只剔除明确属于「宿主侧
        能力」的东西（工具编排、缓存、元数据），它们在 AutoClaw 客户端里是
        由客户端自己处理的。

        另外把 `max_tokens` 夹进上游允许的区间：上游对越界的 max_tokens 直接
        回 406 空 body（实测只放行 10000–131072），而 Codex 默认发 4096。
        """
        payload = dict(body)
        for key in ("metadata", "store", "stream_options", "safety_settings"):
            payload.pop(key, None)
        payload = normalize_max_tokens(payload)
        return payload

    async def forward(self, body: dict, account: Account, *, session_id: str = "") -> Any:
        """对一个已选定的账号发起上游请求。"""
        token = account.token or self.refresh_account_token(account)
        payload = self.prepare_payload(body)
        if payload.get("stream"):
            return self.upstream.chat_stream(token, payload, session_id=session_id)
        return await self.upstream.chat(token, payload)

    # -- 统计 -------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "keys": len(self.api_keys),
            "pool": self.pool.stats(),
            "logs": len(self.logs),
        }


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
def create_app(*, settings=None, gateway: Gateway | None = None,
               key_store: KeyStore | None = None) -> FastAPI:
    """构建 FastAPI 应用。settings 缺省从环境变量读。

    ``key_store`` 缺省是 ``~/.ac2api/keys.json``：key 会落盘，重启后仍然有效。
    传 ``KeyStore`` 实例可以改路径；传 ``False`` 可以退回纯内存（selftest 用）。
    """
    from .upstream import Settings  # 局部导入：便于测试替换

    settings = settings or Settings.from_env()
    upstream_client = gateway.upstream if gateway is not None else UpstreamClient(settings)
    if key_store is False:
        store = None
    else:
        store = key_store if key_store is not None else KeyStore()
    gw = gateway or Gateway(settings=settings, pool=AccountPool(),
                            upstream=upstream_client, store=store)
    if gateway is not None and store is not None and gw.store is None:
        # 外部注入的 gateway 没带 store 时补上，行为与默认一致。
        gw.store = store
        gw._load_keys()
    elif gateway is not None and store is not None and gw.store is not store:
        # 已经有 store（自己加载过了）：别再加载一遍，否则同一份磁盘记录会被
        # 二次并入内存，落盘时就变成重复行。只把文件路径对齐即可。
        gw.store = store
    # 没有配置任何账号时，用一个「本机客户端」虚拟账号兜底 —— 单机自用场景
    # 下这就是绝大多数人的用法：只开一个客户端，不需要多账号池。
    if not gw.pool.all():
        token = settings.static_token
        gw.pool.upsert(Account(key="local", token=token, nickname="本机 AutoClaw", enabled=True))

    app = FastAPI(title="ac2api", version="1.0.0",
                  description="把 AutoClaw 的积分额度包装成 OpenAI 兼容 API")
    app.state.gateway = gw

    # 客户端大多是浏览器/桌面端跨域调用，全放开；鉴权靠 key，不靠来路。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def key_from_request(request: Request) -> str | None:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        # 兼容 Anthropic 风格的 x-api-key，省得用户换客户端又改一次。
        return request.headers.get("x-api-key")

    def require_key(request: Request) -> ApiKey | JSONResponse:
        key = key_from_request(request)
        api_key = gw.authenticate(key)
        if api_key is None:
            return _json_error(401, "无效的 API Key。请在 Authorization: Bearer <key> 里传入。",
                               code="invalid_api_key", type_="authentication_error")
        if api_key.status != "active":
            return _json_error(403, "该 API Key 已被禁用。", code="key_disabled", type_="permission_error")
        # 注意：这里 **不做** 限速判定。require_key 也被 /v1/models、/v1/balance 用来
        # 做鉴权 + 探测，如果连元信息端点都计次，客户端一次探活 + 重试 3 次，
        # 60/分的配额瞬间被打掉一半。只在真正花钱的对话端点调用 check_rate()。
        if not api_key.has_quota():
            return _json_error(402, "该 API Key 的本地积分配额已用尽。", code="quota_exceeded")
        return api_key

    def health_snapshot() -> dict:
        pool = gw.pool.stats()
        return {
            "status": "ok" if pool["active"] > 0 else "degraded",
            "accounts": pool["accounts"],
            "active_accounts": pool["active"],
            "total_remaining": pool["total_remaining"],
            "expiring_soon_remaining": pool["expiring_soon_remaining"],
            # 非流式：转发上游 JSON，记账后原样回给调用方。
            # 下面这些都是「探测结果」（resolve_model_base_url 会读客户端下发的
            # 受管配置，不是写死的常量）。
            "upstream": resolve_model_base_url(settings),
            "chat_path": settings.chat_path,
            "default_model": settings.default_model,
            # client=借本机 AutoClaw 客户端代发（兜底），
            # direct=网关直连上游（默认，对话/积分/目录都可用，不依赖客户端）。
            "chat_mode": gw.upstream.chat_mode(),
            "client_exe": getattr(gw.upstream._claw, "exe", "") if gw.upstream._claw else "",
        }

    # -- 元信息 -----------------------------------------------------------
    @app.get("/health")
    async def health():
        return health_snapshot()

    @app.get("/v1/models")
    async def list_models(request: Request):
        auth = require_key(request)
        if isinstance(auth, JSONResponse):
            return auth
        token = ""
        for acc in gw.pool.all():
            if acc.enabled and acc.token:
                token = acc.token
                break
        models = await gw.upstream.models(token)
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": int(time.time()),
                 "owned_by": "autoclaw", "permission": []}
                for name in models
            ],
        }

    @app.get("/v1/balance")
    @app.get("/user/balance")
    async def balance(request: Request):
        auth = require_key(request)
        if isinstance(auth, JSONResponse):
            return auth
        pool = gw.pool.stats()
        # 同时给 OpenAI 风格和 DeepSeek 风格两套字段：不同客户端各取所需。
        return {
            "object": "balance",
            "credential": auth.credential,
            "is_available": pool["active"] > 0 and auth.has_quota(),
            "credit_limit": auth.credit_limit,
            "credit_used": auth.credit_used,
            "credit_remaining": None if auth.credit_limit is None else max(0.0, auth.credit_limit - auth.credit_used),
            "pool_remaining": pool["total_remaining"],
            "expiring_soon_remaining": pool["expiring_soon_remaining"],
            "balance_infos": [{
                "currency": "CREDIT",
                "total_balance": str(pool["total_remaining"]),
                "granted_balance": str(pool["expiring_soon_remaining"]),
                "topped_up_balance": "0",
            }],
        }

    # -- 主入口 -----------------------------------------------------------
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        auth = require_key(request)
        if isinstance(auth, JSONResponse):
            return auth
        if not auth.check_rate():
            return _json_error(429, f"超出速率限制（{auth.rate_limit} 次/分钟）。", code="rate_limited")

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(400, "请求体不是合法 JSON。")

        if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
            return _json_error(400, "`messages` 必须是非空数组。")

        # 模型名上游是严格校验的（不对就 404/403），所以宁可直接报错，
        # 也不要去猜一个默认值 —— 猜错了调用方只会看到莫名其妙的 502。
        if not str(body.get("model") or "").strip():
            if settings.default_model:
                body["model"] = settings.default_model
            else:
                return _json_error(
                    400,
                    "缺少 `model`。请填 /v1/models 里列出的名字，或设置 AC2API_DEFAULT_MODEL。",
                    code="model_required",
                )

        client_ip = request.client.host if request.client else ""
        if client_ip and gw.pool.waf_blocked(client_ip):
            return _json_error(429, "该来源 IP 触发了上游风控，请稍后再试。", code="upstream_waf")

        sid = sid_from_request(body, request)
        is_stream = bool(body.get("stream"))
        started = time.time()
        tried: list[str] = []
        last_error: Exception | None = None

        for _ in range(MAX_ACCOUNT_ATTEMPTS):
            try:
                account = await asyncio.to_thread(
                    gw.pool.pick, session_key=sid, exclude=tried, wait_s=POOL_WAIT_S)
            except NoAccountAvailable as err:
                last_error = err
                break
            tried.append(account.key)
            try:
                result = await gw.forward(body, account, session_id=sid or "")
            except (UpstreamAuthError, UpstreamQuotaError, UpstreamError) as err:
                last_error = err
                # 只有「换号有意义」的错误才继续重试；参数错误直接回给调用方。
                gw.pool.release(account, success=False, error=f"{type(err).__name__}: {err}")
                if isinstance(err, UpstreamError) and not err.retryable:
                    break
                continue
            except Exception as err:  # noqa: BLE001
                last_error = err
                gw.pool.release(account, success=False, error=f"{type(err).__name__}: {err}")
                continue

            if is_stream:
                return await _stream_response(gw, auth, account, result, body, started, client_ip)
            return _nonstream_response(gw, auth, account, result, body, started, client_ip)

        # 全试过了还是失败 / 池子空了
        message = str(last_error) if last_error else "没有可用的 AutoClaw 账号。"
        status = 503
        if isinstance(last_error, UpstreamError):
            status = last_error.status_code if 400 <= last_error.status_code < 600 else 502
        if isinstance(last_error, NoAccountAvailable):
            status, message = 503, "没有可用的 AutoClaw 账号（全部在途、冷却或积分耗尽）。"
        gw.log(UsageRecord(ts=time.time(), key_name=auth.name, account_key=",".join(tried) or "-",
                           model=str(body.get("model") or ""), stream=is_stream, status="error",
                           latency_ms=int((time.time() - started) * 1000), error=message[:300],
                           client_ip=client_ip))
        return _json_error(status, message, code="upstream_unavailable", type_="api_error")

    # -- Responses 协议入口（Codex CLI / ChatGPT 桌面端）-------------------
    @app.post("/v1/responses")
    async def responses(request: Request):
        """Responses 协议 → chat/completions → 上游。

        鉴权/限速/重试/计费/日志全部复用 chat 那条链路的状态机，
        所以两个入口在「换号重试」「积分扣减」「请求日志」上的行为完全一致。
        """
        auth = require_key(request)
        if isinstance(auth, JSONResponse):
            return auth
        if not auth.check_rate():
            return _json_error(429, f"超出速率限制（{auth.rate_limit} 次/分钟）。", code="rate_limited")

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(400, "请求体不是合法 JSON。")
        if not isinstance(body, dict):
            return _json_error(400, "请求体必须是 JSON 对象。")

        try:
            chat_body = respmod.to_chat_request(body)
        except Exception as err:  # noqa: BLE001 - 翻译层不该 500
            return _json_error(400, f"无法解析 Responses 请求：{err}")
        if not chat_body["messages"]:
            return _json_error(400, "`input` 转换后为空，请检查消息格式。")

        model = str(chat_body.get("model") or "").strip()
        if not model:
            if settings.default_model:
                model = chat_body["model"] = settings.default_model
            else:
                return _json_error(
                    400,
                    "缺少 `model`。请填 /v1/models 里列出的名字，或设置 AC2API_DEFAULT_MODEL。",
                    code="model_required",
                )

        client_ip = request.client.host if request.client else ""
        if client_ip and gw.pool.waf_blocked(client_ip):
            return _json_error(429, "该来源 IP 触发了上游风控，请稍后再试。", code="upstream_waf")

        sid = sid_from_request(body, request) or sid_from_chat(chat_body["messages"])
        is_stream = bool(chat_body.get("stream"))
        started = time.time()
        response_id = respmod.new_id("resp")
        created_at = int(started)
        tried: list[str] = []
        last_error: Exception | None = None

        for _ in range(MAX_ACCOUNT_ATTEMPTS):
            try:
                account = await asyncio.to_thread(
                    gw.pool.pick, session_key=sid, exclude=tried, wait_s=POOL_WAIT_S)
            except NoAccountAvailable as err:
                last_error = err
                break
            tried.append(account.key)
            try:
                result = await gw.forward(chat_body, account, session_id=sid or "")
            except (UpstreamAuthError, UpstreamQuotaError, UpstreamError) as err:
                last_error = err
                gw.pool.release(account, success=False, error=f"{type(err).__name__}: {err}")
                if isinstance(err, UpstreamError) and not err.retryable:
                    break
                continue
            except Exception as err:  # noqa: BLE001
                last_error = err
                gw.pool.release(account, success=False, error=f"{type(err).__name__}: {err}")
                continue

            if is_stream:
                return await _responses_stream(gw, auth, account, result, chat_body, model,
                                               response_id, created_at, started, client_ip)
            return _responses_object(gw, auth, account, result, chat_body, model,
                                     response_id, created_at, started, client_ip)

        message = str(last_error) if last_error else "没有可用的 AutoClaw 账号。"
        status = 503
        if isinstance(last_error, UpstreamError):
            status = last_error.status_code if 400 <= last_error.status_code < 600 else 502
        if isinstance(last_error, NoAccountAvailable):
            status, message = 503, "没有可用的 AutoClaw 账号（全部在途、冷却或积分耗尽）。"
        gw.log(UsageRecord(ts=time.time(), key_name=auth.name, account_key=",".join(tried) or "-",
                           model=model, stream=is_stream, status="error",
                           latency_ms=int((time.time() - started) * 1000), error=message[:300],
                           client_ip=client_ip))
        return _responses_error(status, message)

    # -- 管理接口（改 key / 看日志；生产请放在内网或用 ADMIN_TOKEN 保护）--
    def admin_guard(request: Request) -> JSONResponse | None:
        token = settings.admin_token or ""
        if not token:
            return None
        if request.headers.get("x-admin-token") == token:
            return None
        return _json_error(401, "缺少或错误的 x-admin-token。", code="admin_required", type_="authentication_error")

    @app.get("/admin/api/overview")
    async def admin_overview(request: Request):
        denied = admin_guard(request)
        if denied:
            return denied
        # 管理台要把「可用模型」展示出来，用户才知道 body.model 该填什么。
        token = ""
        for acc in gw.pool.all():
            if acc.enabled and acc.token:
                token = acc.token
                break
        try:
            model_names = await gw.upstream.models(token)
        except Exception:  # noqa: BLE001 - 目录拉不到不该让整个管理台挂掉
            model_names = list(settings.models) or ([settings.default_model] if settings.default_model else [])
        return {
            "health": health_snapshot(),
            "stats": gw.stats(),
            "accounts": gw.pool.snapshot(),
            "keys": [k.public() for k in gw.api_keys.values()],
            "logs": [r.public() for r in list(gw.logs)[:100]],
            "models": [{"id": m, "object": "model"} for m in model_names],
        }

    @app.post("/admin/api/keys")
    async def admin_create_key(request: Request):
        denied = admin_guard(request)
        if denied:
            return denied
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 允许空 body
            body = {}
        if not isinstance(body, dict):
            return _json_error(400, "请求体必须是 JSON 对象。")
        name = str(body.get("name") or f"key-{uuid.uuid4().hex[:6]}")
        limit = body.get("credit_limit")
        try:
            credit_limit = float(limit) if limit not in (None, "") else None
            rate_limit = int(body.get("rate_limit") or 60)
        except (TypeError, ValueError):
            return _json_error(400, "`credit_limit` / `rate_limit` 必须是数字。")
        plaintext = gw.create_key(name, credit_limit=credit_limit, rate_limit=rate_limit)
        # 明文只在这里出现一次。
        return {"name": name, "api_key": plaintext, "note": "请立刻复制保存，之后无法再次查看。"}

    @app.post("/admin/api/keys/{name}/toggle")
    async def admin_toggle_key(name: str, request: Request):
        denied = admin_guard(request)
        if denied:
            return denied
        for key in gw.api_keys.values():
            if key.name == name:
                key.status = "disabled" if key.status == "active" else "active"
                gw.save_keys()
                return key.public()
        return _json_error(404, "没有这个 key。", code="not_found")

    @app.post("/admin/api/accounts")
    async def admin_add_account(request: Request):
        denied = admin_guard(request)
        if denied:
            return denied
        body = await request.json()
        key = str(body.get("key") or "").strip()
        if not key:
            return _json_error(400, "`key` 必填（账号标识）。")
        account = gw.pool.upsert(Account(
            key=key,
            token=str(body.get("token") or ""),
            nickname=str(body.get("nickname") or key),
            enabled=bool(body.get("enabled", True)),
        ))
        try:
            await gw.refresh_account_credits(account)
        except Exception as err:  # noqa: BLE001 - 加号失败也要能看到号，只是标记错误
            account.last_error = f"{type(err).__name__}: {err}"[:300]
        return account.public()

    @app.post("/admin/api/refresh")
    async def admin_refresh(request: Request):
        denied = admin_guard(request)
        if denied:
            return denied
        await gw.refresh_all()
        return {"accounts": gw.pool.snapshot()}

    @app.get("/admin")
    async def admin_page():
        from fastapi.responses import HTMLResponse
        from .admin_page import ADMIN_HTML
        return HTMLResponse(ADMIN_HTML)

    @app.on_event("shutdown")
    async def _shutdown():
        await upstream_client.aclose()

    @app.on_event("startup")
    async def _startup():
        """启动时先查一次积分（拿到剩余额度与到期日），之后按间隔巡检。

        巡检必须常驻且吞掉所有异常：积分端点不可用只该让调度退化，
        不能让网关挂掉或刷屏。
        """
        async def first_pass() -> None:
            try:
                await gw.refresh_all()
            except Exception as err:  # noqa: BLE001
                print(f"[ac2api] 首次刷新积分失败：{type(err).__name__}: {err}")

        async def loop(interval: float) -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await gw.refresh_all()
                except Exception as err:  # noqa: BLE001
                    print(f"[ac2api] 巡检刷新失败：{type(err).__name__}: {err}")

        await first_pass()
        pool = gw.pool.stats()
        print(f"[ac2api] 账号 {pool['accounts']} 个，可用 {pool['active']} 个，"
              f"剩余积分 {pool['total_remaining']}，7 天内到期 {pool['expiring_soon_remaining']}")
        interval = float(os.environ.get("AC2API_REFRESH_INTERVAL", "600") or 0)
        if interval > 0:
            app.state.refresh_task = asyncio.create_task(loop(interval))

    return app


# ---------------------------------------------------------------------------
# 响应构造
# ---------------------------------------------------------------------------
def _nonstream_response(gw: Gateway, auth: ApiKey, account: Account, result: Any,
                        body: dict, started: float, client_ip: str) -> JSONResponse:
    """非流式：转发上游 JSON，记账后原样回给调用方。"""
    usage = result.get("usage") if isinstance(result, dict) else None
    credits = usage_credits(usage, tokens_per_credit=gw.settings.tokens_per_credit)
    tokens = int((usage or {}).get("total_tokens") or 0) if isinstance(usage, dict) else 0
    auth.record_usage(credits=credits, tokens=tokens)
    gw.note_usage()
    gw.pool.release(account, success=True)
    gw.log(UsageRecord(
        ts=time.time(), key_name=auth.name, account_key=account.key,
        model=str(body.get("model") or ""), stream=False, status="ok",
        latency_ms=int((time.time() - started) * 1000), credits=credits, tokens=tokens,
        client_ip=client_ip,
    ))
    return JSONResponse(content=result)


async def _stream_response(gw: Gateway, auth: ApiKey, account: Account, stream: Any,
                           body: dict, started: float, client_ip: str) -> StreamingResponse:
    """流式：逐字节透传上游 SSE 字节，结束时补齐账目。

    这里刻意**不改动上游数据**：
    * OpenAI 的 SSE 是 `data: {...}\\n\\n` 边界；
    * 重新 JSON 序列化会改变字段顺序、丢失未知字段；
    * 任何一处解析失败都不该让用户的流断掉。

    透传的同时旁路解析出 usage（不改动转发内容），用于计费。
    """

    async def generator():
        buffer = b""
        usage: dict = {}
        credits = 0.0
        tokens = 0
        status = "ok"
        error = ""
        try:
            async for chunk in stream:
                yield chunk
                # 只旁路嗅探 usage，不碰转发出去的字节；解析不了 JSON 直接跳过。
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == b"[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        if isinstance(event.get("usage"), dict):
                            usage = event["usage"]
                        if event.get("error"):
                            error = str(event["error"])[:300]
                            status = "error"
        except UpstreamError as err:
            status, error = "error", f"{type(err).__name__}: {err}"
            # 已经在流里了，没法再改 HTTP 码：按 SSE 惯例补一个 error 事件。
            yield f"data: {json.dumps({'error': {'message': err.message, 'type': 'api_error'}}, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            # 客户端主动断开：不算网关错误，但要把租约还回去。
            status = "cancelled"
            raise
        finally:
            credits = usage_credits(usage, tokens_per_credit=gw.settings.tokens_per_credit)
            tokens = int(usage.get("total_tokens") or 0) if usage else 0
            if status == "ok" and credits:
                auth.record_usage(credits=credits, tokens=tokens)
            elif status == "ok":
                auth.record_usage(credits=0.0, tokens=tokens)
            if status == "ok":
                gw.note_usage()
            gw.pool.release(account, success=status in ("ok", "cancelled"),
                            error=error if status == "error" else "")
            gw.log(UsageRecord(
                ts=time.time(), key_name=auth.name, account_key=account.key,
                model=str(body.get("model") or ""), stream=True, status=status,
                latency_ms=int((time.time() - started) * 1000), credits=credits,
                tokens=tokens, error=error, client_ip=client_ip,
            ))

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 缓冲，否则流式会被攒成一坨一次性吐出。
            "X-Accel-Buffering": "no",
        },
    )


def _responses_error(status: int, message: str) -> JSONResponse:
    """Responses 的错误体。

    形状是 `{"error": {...}, "response": {...}}`：codex 先看 `error`，
    再读 `response` 里的状态；只给一处有时会显示成「空错误」。
    """
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": "upstream_error", "message": message, "type": "api_error"},
            "response": None,
        },
    )


def _responses_object(gw: Gateway, auth: ApiKey, account: Account, result: Any,
                      chat_body: dict, model: str, response_id: str, created_at: int,
                      started: float, client_ip: str) -> JSONResponse:
    """非流式 Responses：chat 响应体翻译成 Responses 响应体后记账返回。"""
    usage = result.get("usage") if isinstance(result, dict) else None
    credits = usage_credits(usage, tokens_per_credit=gw.settings.tokens_per_credit)
    tokens = int((usage or {}).get("total_tokens") or 0) if isinstance(usage, dict) else 0
    auth.record_usage(credits=credits, tokens=tokens)
    gw.note_usage()
    gw.pool.release(account, success=True)
    gw.log(UsageRecord(
        ts=time.time(), key_name=auth.name, account_key=account.key,
        model=model, stream=False, status="ok",
        latency_ms=int((time.time() - started) * 1000), credits=credits, tokens=tokens,
        client_ip=client_ip,
    ))
    payload = respmod.to_response_object(result, model=model, response_id=response_id,
                                         created_at=created_at)
    return JSONResponse(content=payload)


async def _responses_stream(gw: Gateway, auth: ApiKey, account: Account, stream: Any,
                            chat_body: dict, model: str, response_id: str, created_at: int,
                            started: float, client_ip: str) -> StreamingResponse:
    """流式 Responses：把上游 chat SSE 逐事件翻译成 Responses 事件流。

    与 chat 端点的「字节透传」不同，这里必须重组：两边的 SSE 事件名和负载
    完全不同（`chat.completion.chunk` vs `response.output_text.delta`）。
    """

    async def generator():
        buffer = b""
        usage: dict = {}
        credits = 0.0
        tokens = 0
        status = "ok"
        error = ""
        translator = respmod.StreamTranslator(model, response_id=response_id,
                                              created_at=created_at)
        try:
            for event in translator.start():
                yield event
            async for chunk in stream:
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    event = respmod.parse_sse_line(line)
                    if event is None:
                        continue
                    for out in translator.feed(event):
                        yield out
            for out in translator.finish():
                yield out
        except UpstreamError as err:
            status, error = "error", f"{type(err).__name__}: {err}"
            for out in translator.fail(err.message):
                yield out
        except (asyncio.CancelledError, GeneratorExit):
            status = "cancelled"
            raise
        finally:
            usage = translator.usage or usage  # noqa: SLF001 - 同一模块内取上游用量
            credits = usage_credits(usage, tokens_per_credit=gw.settings.tokens_per_credit)
            tokens = int(usage.get("total_tokens") or 0) if usage else 0
            auth.record_usage(credits=credits if status == "ok" else 0.0, tokens=tokens)
            if status == "ok":
                gw.note_usage()
            gw.pool.release(account, success=status in ("ok", "cancelled"),
                            error=error if status == "error" else "")
            gw.log(UsageRecord(
                ts=time.time(), key_name=auth.name, account_key=account.key,
                model=model, stream=True, status=status,
                latency_ms=int((time.time() - started) * 1000), credits=credits,
                tokens=tokens, error=error, client_ip=client_ip,
            ))

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


#: 模块级 ASGI 入口（`uvicorn ac2api.main:app`）。
app = create_app()
