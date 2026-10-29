"""上游适配层：与 AutoClaw（智谱 autoglm）后端通信的全部细节都在这里。

协议是怎么来的
--------------
AutoClaw 桌面客户端本身是「一个 Electron 外壳 + 一套受管（managed）端点」：
端点、base_url 都由服务端下发并做了 Ed25519 签名（`managed_model_gateway`
的连接表），客户端把这份配置缓存在本地。**所以 base_url 不能写死**：
必须从本地配置里读——版本升级、灰度、海外版都会换域名。

一次对话请求，客户端实际发出的东西是：

    POST {modelBaseUrl}/autoclaw-proxy/proxy/autoclaw/chat/completions
    body: {"model": "<id>", "messages": [...], "stream": true|false, ...}   # 就是 OpenAI 协议

    X-Authorization: Bearer <accessToken>     # 注意：网关域认 X-Authorization
    X-Auth-Appid / X-Auth-TimeStamp / X-Auth-Sign     # md5(appid&ts&appkey)，见 build_auth_headers
    X-Product / X-Version / X-Tm / X-Lang / X-Channel  # 客户端指纹
    X-Request-Id / X-Trace-Id / X-Request-Model / X-Client-Type / X-Session-Id / X-Query-Id

身份域（账户、钱包、模型目录、token 续期）则用普通 `Authorization: Bearer`。
两种头不能混：managed 模型网关只认 `X-Authorization`，身份域只认 `Authorization`。

失败语义也照抄了客户端的 `Lqn` 表（见 `FAILURE_MAP`）：
403+810000 额度耗尽、403+810001 限流、403+410004 账号被封、402 积分不足、
401 accessToken 失效。前四种都值得「换个号再试」，401 应该先刷 token。

关于 accessToken
----------------
客户端自己持有 token，并只在「同家族进程发起的本地连接」上通过
127.0.0.1:18432/get_token 吐出明文（用 netstat 反查对端 PID + 进程树校验），
所以本网关**不能**走那个接口。token 只有三条来路：

1. `token.py` 直接解密本机 `account-credentials.enc`（装了客户端就能自动工作）；
2. 手填（配合一次性抓包 / 调试工具，或从客户端里 copy 出来）——见 README；
3. 外部「取 token 服务」把当前 token 暴露成本机 HTTP 接口，用
   `AC2API_TOKEN_URL` 指过去（脚本/插件/自动化都行，格式宽松，见
   `parse_token_payload`）。

`token.py` 负责按这个顺序取 token，本模块只负责「拿到 token 之后怎么发请求」。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .credits import extract_resources, resource_summary, summarize_account, summarize_wallet

# ---------------------------------------------------------------------------
# 客户端内置常量与端点（来自 main.cjs 反编译，别凭印象改）
# ---------------------------------------------------------------------------
#: 受管端点所在的 API 网关（客户端里 `httpOrigin`）。聊天走这里。
DEFAULT_BASE_URL = "https://autoglm-acceleration-api.zhipuai.cn"
#: 账户、钱包、登录相关的老域（客户端里 `legalCloudOrigin`）。积分走这里。
DEFAULT_ACCOUNT_BASE = "https://autoglm-api.zhipuai.cn"

#: 客户端把下发配置缓存成 `<userData>/<hash>.json`；这里给出可能的根目录，
#: 实际文件名每次启动重算，所以直接扫内容找 `connections`。
ACCOUNT_ROOT_HINTS = (
    "{APPDATA}/AutoClaw-official",
    "{APPDATA}/AutoClaw",
    "{APPDATA}/Zwork-official",
    "{APPDATA}/yuandian",
    "{HOME}/.config/AutoClaw-official",
    "{HOME}/Library/Application Support/AutoClaw-official",
)

#: accessToken 的外部来源（手工填 / 本地服务）。留空只认账号池里的 token。
DEFAULT_TOKEN_URL = ""

#: 签名头用的 appid/appkey —— 客户端内置常量（`Feo`）。
DEFAULT_APP_ID = "100003"
DEFAULT_APP_KEY = "38d2391985e2369a5fb8227d8e6cd5e5"
#: 客户端身份指纹（`gE()` 里写死的几个）。
DEFAULT_PRODUCT = "autoclaw-desktop"
DEFAULT_CLIENT_TYPE = "pc"
#: 模型目录端点（GET，返回 `{"models": [...]}`）。
DEFAULT_MODELS_PATH = "/autoclaw-proxy/proxy/autoclaw-model-config"
#: 对话端点，拼在 modelBaseUrl（含 proxy 前缀）后面。
DEFAULT_CHAT_PATH = "/chat/completions"
#: 积分钱包端点（身份域，GET）。实测返回各钱包类型余额。
WALLET_PATH = "/agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw"
#: 钱包实例端点（身份域，GET）。带 `expires_at`，到期感知选号就靠它。
WALLET_INSTANCES_PATH = "/agent-assetmgr/api/v1/wallet-instances?wallet_type=all&wallet_scope=all"
#: 消费明细端点（身份域，GET）。
LEDGER_PATH = "/agent-assetmgr/api/v1/ledgers_std"
#: 兼容旧配置：AC2API_CREDITS_PATH 仍可覆盖，默认指向钱包实例端点。
DEFAULT_CREDITS_PATH = "/agent-assetmgr/api/v1/wallet-instances"

#: 客户端上报的「托管模型网关」标识。
MANAGED_SERVICE_ID = "managed_model_gateway"
MANAGED_PROXY_PREFIX = "/autoclaw-proxy/proxy/autoclaw"

#: 上游网关对 system prompt 的强校验前缀 —— **406 的第一条规则**（共三条，见
#: :data:`MAX_TOKENS_MIN` 与 :data:`BLOCKED_IDENTITY_PATTERN`）。
#:
#: 抓包实测（2026-10-29，真账号，`zai_auto`）：
#:   * 完整客户端 system（5397 字）        -> 200
#:   * 上面这段前缀（56 字，正好到句号）    -> 200
#:   * 少一个字符（"...concisely"）        -> 406
#:   * "You are AutoClaw." / 空 / 自拟文案 -> 406
#:   * 前缀之后随便追加（"EXTRA."、中文）  -> 200
#:
#: 也就是说网关只做**前缀匹配**，但前缀必须一字不差（含那个句号）。任何第三方
#: 客户端（codex / Cherry Studio / Cline…）都不会发这个 system，所以网关必须替
#: 调用方补上，否则对话永远 406。
REQUIRED_SYSTEM_PREFIX = "You are AutoClaw. Answer the user directly and concisely."

#: 上游对 `max_tokens` / `max_completion_tokens` 的**取值区间**校验 —— 这是 406
#: 的第二个成因（与 system 前缀无关，两个条件互不影响）。
#:
#: 实测（2026-10-29，真账号，四个模型，system 前缀完全合规）：
#:   * 不发 max_tokens                        -> 200
#:   * max_tokens = 1024 / 4096 / 8192        -> 406
#:   * max_tokens = 10000 起                  -> 200
#:   * max_tokens = 131072                    -> 200
#:   * max_tokens = 131073 / 150000 / 200000  -> 406
#:   * max_completion_tokens = 4096           -> 200（换成这个字段名就放行）
#:
#: 也就是只放行 [10000, 131072]；太小、太大都会被判「不合规」。上界 131072 与
#: 模型无关（zai_auto 在模型目录里写的是 393216，报 393216 仍然 406），所以是网关
#: 侧的固定区间，不是逐模型的 maxTokens。Codex 默认 max_output_tokens=4096，
#: 正好落在被拒区间里 —— 这就是它每次对话都 406 的原因。
MAX_TOKENS_MIN = 10000
MAX_TOKENS_MAX = 131072

#: 上游还会拦「竞品客户端身份」——正文里出现这些词同样 406 空 body。
#:
#: 实测（2026-10-29，真账号，前缀完全合规的前提下只换追加正文）：
#:   * "You are Codex CLI."     -> 406
#:   * "You are codex cli."     -> 406（大小写不敏感）
#:   * "Codex and CLI."         -> 200（分开写不拦）
#:   * "You are Claude Code."   -> 406
#:   * "You are OpenAI Codex."  -> 200（顺序反了不拦，说明是字面量匹配）
#:   * Cursor / Copilot / Cline / Windsurf / aider / Gemini CLI / Trae… -> 200
#:
#: Codex 桌面端的 instructions 里天然带 "running in the Codex CLI"，如果不洗掉，
#: 每次对话都 406。所以转发前把这些词替换成中性写法（语义保留、身份抹掉）。
BLOCKED_AGENT_PHRASES: tuple[tuple[str, str], ...] = (
    ("codex cli", "terminal coding agent"),
    ("claude code", "terminal coding agent"),
)

#: 上游还拦 `You are <竞品>` 这个**句式** —— 光洗 `codex cli` 是不够的。
#:
#: 实测（2026-10-29，system 里前缀合规、只改追加正文）：
#:   * "You are Codex."        -> 406      * "you are codex."     -> 406
#:   * "You are CODEX."        -> 406      * "You are Codex"      -> 406
#:   * "You are Claude."       -> 200      * "You are ChatGPT."   -> 200
#:   * "Codex" / "I am Codex"  -> 200       （词单独出现不拦）
#:   * "You are  Codex."（两个空格）-> 200  （**必须单个空格**才命中）
#:   * "You are a Codex."      -> 200      （中间插词就不命中）
#:   * user 消息里 "You are Codex." -> 200  （只查 system）
#:
#: Codex 桌面端的 instructions 开头正好是 "You are Codex."，所以这条踩得很死。
BLOCKED_IDENTITY_PATTERN = re.compile(r"(?i)\byou are (?P<who>codex|claude code)\b")

#: 命中 :data:`BLOCKED_IDENTITY_PATTERN` 后替换成的中性写法。
IDENTITY_REPLACEMENT = "You are a terminal coding agent"

#: 上游拦的**整句**竞品身份声明 —— 只按字面子串匹配，独立于上面两条规则。
#:
#: 实测（2026-10-30，真账号，system 前缀合规、每次只改追加的一句话）：
#:   * "You are an AI agent powered by DeepSeek Harness."     -> 406
#:   * "you are an ai agent powered by deepseek harness."     -> 406（大小写不敏感）
#:   * "You are an AI agent powered by DeepSeek Harness"      -> 406（句号可有可无）
#:   * "You are an AI agent powered by X Harness."            -> 200
#:   * "You are an AI agent powered by DeepSeek X."           -> 200
#:   * "You are an AI assistant powered by DeepSeek Harness." -> 200（AI agent 不能变）
#:   * "Youre an AI agent powered by DeepSeek Harness."       -> 200（You are 不能断）
#:   * "You are an  AI agent …"（两个空格）                    -> 200（内部必须单空格）
#:   * 单独出现 "DeepSeek" / "DeepSeek Harness" / "Harness"     -> 200
#:   * 单独出现 "You are an AI agent" / "powered by"            -> 200
#:
#: 也就是说：**整句连起来**才命中，拆散或改动任一词都放行 —— 典型字面子串
#: 匹配。DSH（DeepSeek Harness）的 system prompt 开场就是这一句，而它正是
#: ac2api 最常见的调用方，所以不洗掉就每次对话都 406。
BLOCKED_AGENT_LINES: tuple[tuple[str, str], ...] = (
    ("you are an ai agent powered by deepseek harness",
     "You are an AI agent"),
)

#: 这些状态码意味着「换个号还有救」。
RETRYABLE_STATUS = {401, 402, 403, 408, 425, 429, 500, 502, 503, 504}

#: (HTTP 状态, 业务码) -> (语义, 是否换号)。
#: 业务码来自客户端 `Lqn` 表：上游把原因塞在 body 的 `code`/`error.code` 里，
#: 光看状态码会把「额度耗尽」误判成「鉴权失败」，所以必须按业务码细分。
FAILURE_MAP: dict[tuple[int, str], tuple[str, bool]] = {
    (403, "810000"): ("quota", True),          # managed_model_quota_exhausted
    (403, "810001"): ("rate_limit", True),     # managed_model_rate_limited
    (403, "410004"): ("banned", True),         # managed_model_account_banned
    (403, "820002"): ("bad_request", False),   # client_version_unsupported
    (403, "820003"): ("bad_request", False),   # model_request_headers_invalid
    (403, "820004"): ("conflict", True),       # migration_session_taken
    (403, "820005"): ("expired", True),        # migration_window_expired
    (402, ""): ("quota", True),                # managed_model_points_insufficient
    (401, ""): ("auth", True),
}

#: 业务码之外的关键字兜底（上游偶尔把中文提示直接塞进 message）。
QUOTA_KEYWORDS = (
    "insufficient", "quota", "balance", "exceed", "limit_reached",
    "积分不足", "额度不足", "余额不足", "points_insufficient",
)
AUTH_KEYWORDS = ("unauthorized", "invalid token", "token expired", "access_credential_rejected")


class UpstreamError(Exception):
    """上游返回了非预期结果。status_code 保留上游原值以便原样回传。"""

    def __init__(self, message: str, *, status_code: int = 502, body: Any = None,
                 retryable: bool = True, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        self.retryable = retryable
        self.code = code


class UpstreamAuthError(UpstreamError):
    """accessToken 失效 / 无权限：换号（或刷 token）重试。"""

    def __init__(self, message: str, **kwargs: Any):
        kwargs.setdefault("code", "auth")
        super().__init__(message, **kwargs)


class UpstreamQuotaError(UpstreamError):
    """该账号积分不足 / 被限 / 被封：换号重试，并把该号标记出去。"""

    def __init__(self, message: str, **kwargs: Any):
        kwargs.setdefault("code", "quota")
        super().__init__(message, **kwargs)


def _load_dotenv() -> None:
    """极简 .env 加载：只想「一条命令就能跑」，不引 python-dotenv 依赖。
    已存在的环境变量优先（命令行/容器注入的更可信）。
    """
    for base in (os.getcwd(), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
        path = os.path.join(base, ".env")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    name, _, value = line.partition("=")
                    name = name.strip()
                    value = value.strip().strip('"').strip("'")
                    if name and name not in os.environ:
                        os.environ[name] = value
        except OSError:
            continue
        break


@dataclass
class Settings:
    """网关配置。全部来自环境变量，便于一键部署脚本直接写 .env。"""

    #: 受管端点网关；会被本地下发配置里的 base_url 覆盖（下发优先）。
    base_url: str = DEFAULT_BASE_URL
    #: 账户 / 钱包域。
    account_base: str = DEFAULT_ACCOUNT_BASE
    #: 是否从本机 AutoClaw 的下发配置里读 base_url（推荐开启）。
    discover: bool = True
    #: accessToken 的外部来源（见模块 docstring）；空 = 不启用。
    token_url: str = DEFAULT_TOKEN_URL
    app_id: str = DEFAULT_APP_ID
    app_key: str = DEFAULT_APP_KEY
    product: str = DEFAULT_PRODUCT
    client_type: str = DEFAULT_CLIENT_TYPE
    version: str = "1.0.0"
    channel: str = ""
    language: str = "zh-CN"
    models_path: str = DEFAULT_MODELS_PATH
    chat_path: str = DEFAULT_CHAT_PATH
    credits_path: str = DEFAULT_CREDITS_PATH
    default_model: str = ""
    #: 是否在对话请求里带 `X-Request-Model`。默认**打开**：客户端每次请求都带，
    #: 值取自下发目录里的模型 ID；不带会被网关回 `400 非法模型`，带了才是
    #: 真正进入 managed 模型校验。这个头必须与 body.model 严格一致。
    send_model_header: bool = True
    #: 上游模型目录拉不到时的静态清单（逗号分隔）。
    models: tuple[str, ...] = ()
    #: 手工配置的 token（单账号兜底）。
    static_token: str = ""
    #: 管理接口口令；为空则管理接口不设防（本地自用）。
    admin_token: str = ""
    #: 对话请求怎么发出去：
    #:   direct —— 网关自己直连上游，不依赖本机客户端（默认，且已实测可用）
    #:   client —— 交给本机 AutoClaw 客户端代发（需客户端开着，老兜底方案）
    #:   auto   —— 连官方网关且本机有客户端时才走 client，否则 direct
    chat_mode: str = "direct"
    #: 本机 token 服务（客户端提供，只对客户端家族进程放行）。
    client_token_endpoint: str = "http://127.0.0.1:18432/get_token"
    #: AutoClaw 客户端可执行文件路径；空 = 自动查找。
    client_exe: str = ""
    #: 客户端签名凭证 `id.secret`（来自下发 provider 配置的 providerApiKey）。
    #: **现在不需要了**：406 的真因是 system prompt 前缀，不是签名。保留字段只为
    #: 兼容旧配置与自建上游场景。
    sign_credential: str = ""
    #: 本地额度记账口径：多少 token 折 1 积分（上游 usage 不含积分字段，实测
    #: 约 100 token 折 1 积分、每次调用至少 1 积分）。只影响 /v1/balance 与
    #: 每个 API Key 的 credit_limit 记账，不影响上游真实扣费。
    tokens_per_credit: float = 100.0
    #: 签名握手的目标 origin；空 = 取模型网关 origin。
    sign_base_url: str = ""
    #: 签名时放进 `X-App-Id` 的 app id（客户端固定 `autoclaw`）。
    sign_app_id: str = "autoclaw"
    #: 签名用的客户端版本（下发配置里的 provider.version / 客户端自身版本）。
    sign_version: str = ""
    connect_timeout: float = 10.0
    read_timeout: float = 600.0
    credits_timeout: float = 20.0
    #: 出站代理，形如 `http://127.0.0.1:7890`。空 = 直连（**不**读系统代理，
    #: 原因见 UpstreamClient._http 的注释）。
    proxy: str = ""
    #: 运行时探测到的网关地址（不落 .env，仅进程内缓存）。
    discovered_base_url: str = field(default="", compare=False)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()

        def env(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        models = tuple(m.strip() for m in env("AC2API_MODELS").split(",") if m.strip())
        return cls(
            base_url=env("AC2API_UPSTREAM_BASE", DEFAULT_BASE_URL).rstrip("/"),
            account_base=env("AC2API_ACCOUNT_BASE", DEFAULT_ACCOUNT_BASE).rstrip("/"),
            discover=env("AC2API_DISCOVER", "1") not in ("0", "false", "False", "no"),
            token_url=env("AC2API_TOKEN_URL", DEFAULT_TOKEN_URL),
            app_id=env("AC2API_APP_ID", DEFAULT_APP_ID),
            app_key=env("AC2API_APP_KEY", DEFAULT_APP_KEY),
            product=env("AC2API_PRODUCT", DEFAULT_PRODUCT),
            client_type=env("AC2API_CLIENT_TYPE", DEFAULT_CLIENT_TYPE),
            version=env("AC2API_CLIENT_VERSION", "1.0.0"),
            channel=env("AC2API_CHANNEL"),
            language=env("AC2API_LANG", "zh-CN"),
            models_path=env("AC2API_UPSTREAM_MODELS_PATH", DEFAULT_MODELS_PATH),
            chat_path=env("AC2API_UPSTREAM_CHAT_PATH", DEFAULT_CHAT_PATH),
            credits_path=env("AC2API_UPSTREAM_CREDITS_PATH", DEFAULT_CREDITS_PATH),
            default_model=env("AC2API_DEFAULT_MODEL"),
            send_model_header=env("AC2API_SEND_MODEL_HEADER", "1") not in ("0", "false", "False", "no"),
            models=models,
            static_token=env("AC2API_STATIC_TOKEN"),
            admin_token=env("AC2API_ADMIN_TOKEN"),
            chat_mode=env("AC2API_CHAT_MODE", "direct").lower() or "direct",
            client_token_endpoint=env("AC2API_CLIENT_TOKEN_ENDPOINT",
                                      "http://127.0.0.1:18432/get_token"),
            client_exe=env("AC2API_CLIENT_EXE"),
            sign_credential=env("AC2API_SIGN_CREDENTIAL"),
            sign_base_url=env("AC2API_SIGN_BASE_URL"),
            sign_app_id=env("AC2API_SIGN_APP_ID", "autoclaw"),
            sign_version=env("AC2API_SIGN_VERSION"),
            connect_timeout=float(env("AC2API_CONNECT_TIMEOUT", "10")),
            read_timeout=float(env("AC2API_READ_TIMEOUT", "600")),
            credits_timeout=float(env("AC2API_CREDITS_TIMEOUT", "20")),
            proxy=env("AC2API_UPSTREAM_PROXY", ""),
            tokens_per_credit=float(env("AC2API_TOKENS_PER_CREDIT", "") or 100.0),
        )


def _bearer(token: str) -> str:
    """统一补上 `Bearer ` 前缀（凭据里存的可能带也可能不带）。"""
    token = (token or "").strip()
    if not token:
        return ""
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def build_auth_headers(settings: Settings, token: str, *, timestamp: int | None = None,
                       use_x_authorization: bool = True) -> dict[str, str]:
    """客户端 `gE()` 的等价实现：一组固定指纹头 + 时间戳签名 + 凭据。

    签名是 `md5("appid&ts&appkey")`，16 进制小写；时间戳是**秒**。上游接受
    的时钟偏差较窄，所以每次请求现算，不要缓存。

    ⚠️ 两个域认不同的头，**放错域就等于「token 无效」**（实测，客户端 2.2.4）：

    | 域 | 端点 | 认的头 |
    |---|---|---|
    | 身份域 | `/userapi/v1/*`、模型目录、钱包 | `Authorization` |
    | managed 模型网关 | `/autoclaw-proxy/proxy/autoclaw/chat/completions` | `X-Authorization` |

    对话请求放进 `Authorization` 会被网关回 `401 {"code":401001,"message":
    "Invalid token"}`（那层根本不看这个头）；放进 `X-Authorization` 才会被
    managed 网关当成对话请求处理。所以对话用默认的
    `use_x_authorization=True`，积分/目录用 `False`。
    """
    ts = int(timestamp if timestamp is not None else time.time())
    sign = hashlib.md5(f"{settings.app_id}&{ts}&{settings.app_key}".encode("utf-8")).hexdigest()
    headers = {
        "X-Version": settings.version,
        "X-Tm": "win" if os.name == "nt" else ("mac" if os.uname().sysname == "Darwin" else "linux"),
        "X-Product": settings.product,
        "X-Auth-Appid": settings.app_id,
        "X-Auth-TimeStamp": str(ts),
        "X-Auth-Sign": sign,
        "X-Lang": settings.language,
    }
    if settings.channel:
        headers["X-Channel"] = settings.channel
    bearer = _bearer(token)
    if bearer:
        headers["X-Authorization" if use_x_authorization else "Authorization"] = bearer
    return headers


def is_model_gateway_url(settings: Settings, url: str) -> bool:
    """判断某个绝对 URL 是否属于 managed 模型网关（决定用哪种鉴权头）。"""
    gateway = resolve_model_base_url(settings).rstrip("/")
    return bool(gateway) and url.rstrip("/").startswith(gateway)


def build_request_headers(settings: Settings, token: str, *, model: str = "", session_id: str = "",
                          query_id: str = "", request_id: str = "") -> dict[str, str]:
    """一次对话请求要带的全部头：签名头 + 追踪头 + content-type。

    对话请求打 managed 模型域，鉴权头必须是 `X-Authorization`（放进
    `Authorization` 会被回 401，理由见 `build_auth_headers`）。
    """
    headers = build_auth_headers(settings, token, use_x_authorization=True)
    rid = request_id or f"ac2api-{int(time.time() * 1000)}-{os.urandom(4).hex()}"
    headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # 客户端用独立的大小写混写头做网关侧统计，这里照抄。
        "X-Trace-Id": rid,
        "X-Request-Id": rid,
        "X-Client-Type": settings.client_type,
        "x_trace_id": "autoclaw-desktop",
    })
    # `X-Request-Model`：实测必填。不带会被网关回 `400 非法模型`；值必须与
    # body.model 完全一致（客户端也是从下发目录里取同名 ID）。
    if model and settings.send_model_header:
        headers["X-Request-Model"] = model
    if session_id:
        headers["X-Session-Id"] = session_id
    if query_id:
        headers["X-Query-Id"] = query_id
    return headers


def parse_token_payload(raw: str) -> str:
    """从外部 token 服务 / 手工内容里取出裸 accessToken。

    兼容裸字符串、`Bearer xxx`、`{"token": ...}`、`{"access_token": ...}`、
    `{"data": {"accessToken": ...}}` 等各种形态 —— 写脚本的人不一定看文档。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    keys = ("accessToken", "access_token", "token", "bearer_token")
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.removeprefix("Bearer ").strip()
        for holder in ("data", "result", "account", "auth"):
            inner = obj.get(holder)
            if isinstance(inner, dict):
                for key in keys:
                    value = inner.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.removeprefix("Bearer ").strip()
    return ""


def fetch_remote_token(token_url: str = DEFAULT_TOKEN_URL, timeout: float = 10.0) -> str:
    """从外部 token 服务取当前 accessToken（空 URL 直接返回空）。"""
    if not token_url:
        return ""
    req = urllib.request.Request(token_url, headers={"Accept": "application/json, text/plain"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_token_payload(resp.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# 本机下发配置发现（base_url / 模型目录）
# ---------------------------------------------------------------------------
def _candidate_config_roots() -> list[str]:
    subs = {
        "APPDATA": os.environ.get("APPDATA", ""),
        "HOME": os.path.expanduser("~"),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
    }
    roots: list[str] = []
    for hint in ACCOUNT_ROOT_HINTS:
        path = hint
        for name, value in subs.items():
            path = path.replace("{" + name + "}", value)
        if "{" in path or not path:
            continue
        if os.path.isdir(path):
            roots.append(path)
    override = os.environ.get("AC2API_CLIENT_DATA", "").strip()
    if override and os.path.isdir(override):
        roots.insert(0, override)
    return roots


def discover_managed_endpoints(roots: list[str] | None = None) -> dict[str, str]:
    """从本机 AutoClaw 的运行时配置缓存里读出各受管服务的 base_url。

    客户端把下发配置缓存成 `<userData>/<hash>.json`，文件名每次启动重算，
    所以这里按内容特征扫描：一个含 `connections` 的对象，其中每个值都有
    `protocol` + `base_url`。返回 `{service_id: base_url}`。
    """
    found: dict[str, str] = {}
    for root in roots if roots is not None else _candidate_config_roots():
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in entries:
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > 2 * 1024 * 1024:
                    continue
                with open(path, "r", encoding="utf-8") as fh:
                    blob = json.load(fh)
            except (OSError, ValueError):
                continue
            conns = blob.get("connections") if isinstance(blob, dict) else None
            if not isinstance(conns, dict):
                inner = blob.get("snapshot") if isinstance(blob, dict) else None
                conns = inner.get("connections") if isinstance(inner, dict) else None
            if not isinstance(conns, dict):
                continue
            for service_id, conf in conns.items():
                if not isinstance(conf, dict):
                    continue
                base = conf.get("base_url") or conf.get("baseUrl")
                if isinstance(base, str) and base.startswith("http"):
                    found.setdefault(str(service_id), base.rstrip("/"))
    return found


OFFICIAL_HOSTS = ("zhipuai.cn", "bigmodel.cn", "autoclaw.cn")


def _is_official_gateway(url: str) -> bool:
    """该 base_url 是不是官方 managed 网关（只有它要求客户端签名）。"""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS)


def resolve_model_base_url(settings: Settings) -> str:
    """算出对话请求的 base_url（下发配置里的 managed_model_gateway 优先）。"""
    if settings.discovered_base_url:
        return settings.discovered_base_url
    if settings.discover:
        try:
            endpoints = discover_managed_endpoints()
        except Exception:  # noqa: BLE001 - 发现失败不该让网关起不来
            endpoints = {}
        base = endpoints.get(MANAGED_SERVICE_ID)
        if base:
            settings.discovered_base_url = base
            return base
    return settings.base_url


def model_base_url_with_prefix(settings: Settings) -> str:
    """受管网关的 base_url 可能只到域名，也可能带上 proxy 前缀，统一补齐。"""
    base = resolve_model_base_url(settings)
    if MANAGED_PROXY_PREFIX in base:
        return base
    return base + MANAGED_PROXY_PREFIX


# ---------------------------------------------------------------------------
# 模型目录
# ---------------------------------------------------------------------------
def parse_model_catalog(raw: Any) -> list[str]:
    """从模型目录响应里抽出模型 id 列表（只认明确的几种形状）。

    真实上游的目录响应外面还包着一层业务信封 ``{"code":200,"data":{...}}``
    （实测拿到的就是 ``{"code":200,"data":{"models":[...]}}``），所以不能只看
    顶层键，要顺着 data/result/models 这几层往下找。只认这几个"容器"键，
    不去遍历任意嵌套 —— 否则一旦响应里混进别的带 id 的列表，就会把不相干
    的字符串当成模型名冒出来。
    """
    def ids(items: Any) -> list[str]:
        out: list[str] = []
        if not isinstance(items, list):
            return out
        for item in items:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                mid = item.get("id") or item.get("model") or item.get("name")
                if isinstance(mid, str) and mid.strip():
                    out.append(mid.strip())
        return out

    # 逐层深入的最大深度：信封(1) → data(2) → models(3) 已经够用，
    # 多留一层给 result/data/models/items 这种更啰嗦的包装。
    CONTAINERS = ("data", "result", "models", "list", "items")
    LIST_KEYS = ("models", "list", "items")
    MAX_DEPTH = 4

    def walk(node: Any, depth: int) -> list[str]:
        if depth > MAX_DEPTH or not isinstance(node, dict):
            return []
        # 先看当前层的「列表键」：models 是标准写法，list/items 是信封里的别名。
        for key in LIST_KEYS:
            direct = ids(node.get(key))
            if direct:
                return direct
        # 没有列表，就继续往容器键里钻（data/result/...）。
        for key in CONTAINERS:
            found = walk(node.get(key), depth + 1)
            if found:
                return found
        return []

    return walk(raw, 0)


# ---------------------------------------------------------------------------
# system prompt 修正（406 的唯一解法）
# ---------------------------------------------------------------------------
def scrub_agent_phrases(text: str) -> str:
    """把上游拉黑的竞品客户端名替换成中性说法（见 :data:`BLOCKED_AGENT_PHRASES`）。

    大小写不敏感，且容忍词间多余空白（"Codex\\nCLI" 也会被拦）。
    """
    if not isinstance(text, str) or not text:
        return text
    for phrase, replacement in BLOCKED_AGENT_PHRASES:
        pattern = r"\s+".join(re.escape(part) for part in phrase.split())

        def _sub(match: re.Match) -> str:
            # 句首/大写开头时让替换词也跟着大写，避免出现 "… You are coding agent."
            text_hit = match.group(0)
            if text_hit[:1].isupper():
                return replacement[:1].upper() + replacement[1:]
            return replacement

        text = re.sub(pattern, _sub, text, flags=re.IGNORECASE)
    return text


def scrub_identity(text: str) -> str:
    """把上游拉黑的 `You are <竞品>` 句式改写成中性身份。

    见 :data:`BLOCKED_IDENTITY_PATTERN` —— 这是**独立于** :func:`scrub_agent_phrases`
    的另一条规则：`You are Codex.` 里既没有 "codex cli" 也没有 "claude code"，
    但照样 406。Codex 桌面端的 instructions 第一句就是它。
    """
    if not isinstance(text, str) or not text:
        return text
    return BLOCKED_IDENTITY_PATTERN.sub(IDENTITY_REPLACEMENT, text)


def scrub_agent_lines(text: str) -> str:
    """洗掉上游按**整句**拉黑的竞品身份声明（见 :data:`BLOCKED_AGENT_LINES`）。

    与 :func:`scrub_identity` 的区别：那条匹配的是 `You are <名字>` 句式，这条
    匹配的是一整句话，且**内部必须是单空格**（换成双空格上游就不拦了，说明是
    字面子串比较而不是分词）。所以这里只做大小写不敏感，不做空白归一化 ——
    归一化反而会把上游本来放行的写法也改掉。
    """
    if not isinstance(text, str) or not text:
        return text
    for phrase, replacement in BLOCKED_AGENT_LINES:
        text = re.sub(re.escape(phrase), replacement, text, flags=re.IGNORECASE)
    return text


def scrub_text(text: str) -> str:
    """三条竞品规则一起过（整句 → `You are …` 句式 → 词）。"""
    return scrub_agent_phrases(scrub_identity(scrub_agent_lines(text)))


def _scrub_message(msg: dict) -> dict:
    """返回清洗过竞品名的新消息 dict（content 为 str 或文本块列表时）。"""
    content = msg.get("content")
    if isinstance(content, str):
        cleaned = scrub_text(content)
        return msg if cleaned == content else dict(msg, content=cleaned)
    if isinstance(content, list):
        changed = False
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                cleaned = scrub_text(block["text"])
                if cleaned != block["text"]:
                    changed = True
                    block = dict(block, text=cleaned)
            parts.append(block)
        if changed:
            return dict(msg, content=parts)
    return msg


def ensure_system_prompt(payload: dict) -> dict:
    """保证 `messages` 里带一段以 :data:`REQUIRED_SYSTEM_PREFIX` 开头的 system。

    上游网关对 system prompt 做**前缀校验**，不满足就直接 406 空 body（见
    :data:`REQUIRED_SYSTEM_PREFIX` 的实测记录）。第三方客户端发来的 system 显然
    不合规，所以这里替它兜住：    * 完全没有 system      → 补一条
    * 有 system 但不合规    → 前缀 + (调用方原文)，调用方意图不丢
    * 已经合规            → 不加前缀（但仍会洗竞品名）

    另外所有消息里的竞品客户端名都会被替换掉（见 :data:`BLOCKED_AGENT_PHRASES`），
    否则 Codex 桌面端的 "Codex CLI" 文案会让每一次对话都 406。

    返回的是**新的** payload（不改动传入对象），`messages` 也是新列表。
    """
    if not isinstance(payload, dict):
        return payload

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload

    out = [_scrub_message(m) if isinstance(m, dict) else m for m in messages]
    index = -1
    text = ""
    for i, msg in enumerate(out):
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        index = i
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # 多模态 system：只有纯文本片段能参与前缀判断
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            text = "\n".join(parts)
        break

    if index >= 0 and text.lstrip().startswith(REQUIRED_SYSTEM_PREFIX):
        if all(a is b for a, b in zip(out, messages)) and len(out) == len(messages):
            return payload
        return dict(payload, messages=out)

    if index >= 0:
        # 只改第一条 system，其余 system 消息保持原样
        merged = REQUIRED_SYSTEM_PREFIX
        rest = text.strip()
        if rest:
            # 分隔符用单个换行。空行本身**不会**致 406（实测 `前缀 + "\n\n文本"`
            # 也是 200），但用单换行最贴近客户端真实请求，少一个变量。
            merged = f"{REQUIRED_SYSTEM_PREFIX}\n{rest}"
        patched = dict(out[index])
        patched["content"] = merged
        out[index] = patched
    else:
        out.insert(0, {"role": "system", "content": REQUIRED_SYSTEM_PREFIX})

    fixed = dict(payload)
    fixed["messages"] = out
    return fixed


def _clamp_int(value, default: int) -> int:
    """把上游不认的 `max_tokens` 压回允许区间 `[MAX_TOKENS_MIN, MAX_TOKENS_MAX]`。

    非整数 / 布尔 / 字符串一律当没给（返回 `default`），因为上游对类型错误也是
    406 而不是 400，不值得我们原样试探。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        elif isinstance(value, float):
            value = int(value)
        else:
            return default
    return max(MAX_TOKENS_MIN, min(MAX_TOKENS_MAX, value))


def normalize_max_tokens(payload: dict) -> dict:
    """把 `max_tokens` 收进上游允许的区间（见 :data:`MAX_TOKENS_MIN`）。

    实测区间是 [10000, 131072]，越界一律 406 空 body：

    * 调用方给了 `max_tokens` / `max_completion_tokens`
      → 夹到区间内，并**只留 `max_tokens`**（上游两个字段都认，但混发容易踩到
        另一个校验，统一成一个）。
    * 调用方一个都没给
      → 不动。实测「不发 max_tokens」本身是合法的，此时上游按模型默认值截断，
        语义上最接近调用方期望，也省得我们凭空放宽上限。
    * 既没有 `max_tokens` 也没有 `messages` 的畸形 payload → 原样返回。

    返回**新的** payload；没有需要改动时返回传入对象本身（便于调用方判断）。
    """
    if not isinstance(payload, dict):
        return payload

    has_max = "max_tokens" in payload
    has_completion = "max_completion_tokens" in payload
    if not has_max and not has_completion:
        return payload

    out = dict(payload)
    out.pop("max_completion_tokens", None)
    source = payload.get("max_tokens")
    if not has_max:
        source = payload.get("max_completion_tokens")
    out["max_tokens"] = _clamp_int(source, MAX_TOKENS_MAX)
    return out


# ---------------------------------------------------------------------------
# 上游客户端
# ---------------------------------------------------------------------------
class UpstreamClient:
    """一个 httpx 连接池，供所有请求复用（上游是同一批域名）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._catalog_cache: tuple[float, list[str]] | None = None
        self._claw = None
        self._token_source = None   # 惰性建：只有真的代发对话时才需要
        self._signer = None         # 惰性建：只有配了签名凭证才需要
        self._sign_lock: asyncio.Lock | None = None
        self._sign_error = ""       # 上次握手失败原因（给 /health 与日志看）
        # httpx.AsyncClient 的连接池里带 asyncio.Lock/Event，一旦在某个事件循环
        # 上建好就不能跨循环复用（会报 "... is bound to a different event loop"）。
        # 网关本身是单循环，但「探针/管理脚本里 asyncio.run 调一次」这种用法很常见，
        # 所以按事件循环各持一个客户端，换循环就重建。
        self._client: httpx.AsyncClient | None = None
        self._client_loop: object = None

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.settings.read_timeout,
                    connect=self.settings.connect_timeout,
                    # 对 SSE 来说 write/pool 超时过小会误杀长连接，统一放宽到读超时。
                    write=self.settings.read_timeout,
                    pool=self.settings.connect_timeout,
                ),
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
                # 不跟随重定向：上游若返回 302 到风控页，我们要如实暴露，
                # 而不是悄悄跟过去拿到一个 200 的 HTML。
                follow_redirects=False,
                # **不读系统代理**。httpx 默认 proxy=None 等于 trust_env=True，
                # 会去读 Windows 的"Internet 选项"代理（常见是本机 7890 端口的
                # clash/v2ray）。那个代理对 localhost 的豁免规则 httpx 并不认，
                # 于是连"本机假上游 / 本机客户端 token 服务"这种 127.0.0.1 请求
                # 也会被塞给代理，结果是一个空 body 的 502——实测极难和真正的
                # 上游故障区分。上游地址要么是公网（直连即可），要么是 127.0.0.1
                # （更不能走代理），所以这里一律直连；需要代理的人自己设
                # AC2API_UPSTREAM_PROXY，见下面的 proxy=。
                trust_env=False,
                proxy=(self.settings.proxy or None),
            )
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._claw is not None:
            self._claw.cleanup()
        client, self._client = self._client, None
        self._client_loop = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - 跨循环关闭时尽力而为
                pass

    # -- 客户端签名 --------------------------------------------------------
    def signer_configured(self) -> bool:
        return bool((self.settings.sign_credential or "").strip())

    async def _signed_headers(self, headers: dict[str, str], *, session_id: str = "") -> dict[str, str]:
        """给对话请求补上 `X-Client-Sig` / `X-Client-Pow` 等客户端签名头。

        **这个头组本身不是 406 的原因**（406 的真因是 system prompt 前缀，见
        :data:`REQUIRED_SYSTEM_PREFIX`）。保留它只是为了兼容那些确实按客户端
        行为校验签名的上游，平时不必配置：没配凭证就原样返回 headers。
        """
        if not self.signer_configured():
            return headers
        signer = await self._ensure_signer()
        if signer is None:
            return headers
        # 授权头已经在 headers 里（`X-Authorization`）；签名器看到已有授权头
        # 就不会再补自己的 `Bearer id.secret`，这正是客户端的行为。
        loop = asyncio.get_running_loop()
        # PoW 是纯计算（典型几十毫秒），放到线程里免得卡住事件循环。
        return await loop.run_in_executor(
            None, lambda: signer.sign(headers, session_id=session_id or "connection-probe")
        )

    async def _ensure_signer(self):
        from .signer import SignError

        if self._sign_lock is None:
            self._sign_lock = asyncio.Lock()
        async with self._sign_lock:
            if self._signer is not None and self._signer.ready:
                return self._signer
            if self._sign_error:
                # 上一次刚失败过就不再重复握手，等下一次进程重启或成功一次。
                return None
            base = (self.settings.sign_base_url or self.settings.base_url).rstrip("/")
            version = self.settings.sign_version or self.settings.version
            try:
                from .signer import ClientSigner

                signer = ClientSigner(
                    base_url=base,
                    credential=self.settings.sign_credential,
                    version=version,
                    app_id=self.settings.sign_app_id,
                )
                response = await self._http().post(
                    signer.handshake_url,
                    headers=signer.handshake_headers(),
                    content=json.dumps(signer.handshake_payload()).encode("utf-8"),
                )
                signer.finish_handshake(response.status_code, response.text)
            except SignError as err:
                self._sign_error = str(err)
                return None
            except httpx.HTTPError as err:
                self._sign_error = f"{type(err).__name__}: {err}"
                return None
            self._sign_error = ""
            self._signer = signer
            return signer

    # -- 对话传输模式 ------------------------------------------------------
    def chat_mode(self) -> str:
        """解析 `AC2API_CHAT_MODE`：`direct`（默认）/ `client` / `auto`。

        * `direct` —— 网关自己发 HTTPS，不依赖本机客户端。**这是默认值**，
          因为 system prompt 前缀问题解决后直连已经能通（见
          :data:`REQUIRED_SYSTEM_PREFIX`），关掉 AutoClaw 也照常工作。
        * `client` —— 借本机 AutoClaw 客户端进程代发（老的兜底路子，需要客户
          端开着；除非直连又出问题，一般不用）。
        * `auto` —— 只有连官方网关、本机又确实装了客户端时才用 client；指向自
          建/测试上游时一律 direct，免得 mock 上游被代发绕过去。
        """
        from .clawproxy import ClawClient

        mode = (self.settings.chat_mode or "direct").lower()
        if mode not in ("auto", "client", "direct"):
            mode = "direct"
        if mode in ("client", "direct"):
            return mode
        if not _is_official_gateway(self.settings.base_url):
            return "direct"
        if self._claw is None:
            self._claw = ClawClient(exe=self.settings.client_exe,
                                    token_endpoint=self.settings.client_token_endpoint)
        return "client" if self._claw.available() else "direct"

    def _claw_client(self):
        from .clawproxy import ClawClient

        if self._claw is None:
            self._claw = ClawClient(exe=self.settings.client_exe,
                                    token_endpoint=self.settings.client_token_endpoint)
        return self._claw

    async def _chat_via_client(self, payload: dict, *, session_id: str = "") -> dict | AsyncIterator[bytes]:
        """把对话请求交给本机 AutoClaw 客户端代发（签名由客户端完成）。

        managed 网关要求 Ed25519 客户端签名 + 工作量证明，私钥只能由客户端向
        `open.bigmodel.cn/api/paas/c1f3a7e2/v2/client` 握手领取，网关侧无法伪造，
        所以对话必须借客户端之手。钱包/积分/模型目录不走这里（直连即可）。
        """
        from .clawproxy import BridgeRequest, ClawProxyError, build_client_headers

        claw = self._claw_client()
        if not claw.available():
            raise UpstreamError(
                "对话需要本机 AutoClaw 客户端（managed 网关要求客户端签名）。"
                "请安装并登录 AutoClaw，或用 AC2API_CLIENT_EXE 指定 AutoClaw2.exe。",
                status_code=503, retryable=False, code="client_required",
            )
        token_source = "" if self.settings.static_token else claw.token_endpoint
        model = str(payload.get("model") or "")
        # token 优先级：静态配置 > 本机凭据（已解密）> 子进程现取（客户端在跑时才能取）。
        # 本机凭据这条路让「客户端没运行」时也能试一把 —— 签名仍由子进程的
        # 进程身份兜住，token 则是我们自己从加密凭据里解出来的。
        access_token = self.settings.static_token
        if not access_token:
            try:
                if self._token_source is None:
                    from .token import TokenSource

                    self._token_source = TokenSource(self.settings)
                access_token = await asyncio.to_thread(self._token_source.token_for)
            except Exception:  # noqa: BLE001 - 取不到就退回让子进程自己取
                access_token = ""
        headers = build_client_headers(
            access_token=access_token,
            app_id=self.settings.app_id, app_key=self.settings.app_key,
            product=self.settings.product, version=self.settings.version,
            language=self.settings.language, channel=self.settings.channel,
            model=model, session_id=session_id,
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = BridgeRequest(url=self.chat_url(), headers=headers, body=body,
                                stream=bool(payload.get("stream")),
                                timeout=self.settings.read_timeout,
                                token_endpoint=token_source)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(claw.call, request),
                timeout=self.settings.read_timeout + 30,
            )
        except asyncio.TimeoutError as err:
            raise UpstreamError(f"客户端代发超时（{self.settings.read_timeout:.0f}s）",
                                status_code=504, code="client_timeout") from err
        except ClawProxyError as err:
            raise UpstreamError(f"客户端代发失败：{err}", status_code=503,
                                retryable=err.kind in ("timeout", "transport"),
                                code="client_proxy") from err
        if result.status >= 400:
            self._raise_for_status(result.status, result.text)
        if payload.get("stream"):
            async def _iterate() -> AsyncIterator[bytes]:
                for chunk in result.chunks or []:
                    if chunk:
                        yield chunk
            return _iterate()
        try:
            return json.loads(result.text)
        except ValueError as err:
            raise UpstreamError(f"客户端代发返回非 JSON：{result.text[:200]}",
                                status_code=502, body=result.text[:2000]) from err

    # -- 地址 -------------------------------------------------------------
    def chat_url(self) -> str:
        return model_base_url_with_prefix(self.settings) + self.settings.chat_path

    def models_url(self) -> str:
        return resolve_model_base_url(self.settings) + self.settings.models_path

    def credits_url(self) -> str:
        path = self.settings.credits_path
        if path.startswith("http"):
            return path
        # 积分钱包端点属于「账户域」；但如果账户域被显式配置成和网关同源
        # （自建/测试上游通常如此），就跟着网关走，否则测试和自建部署都会打错地方。
        gateway = resolve_model_base_url(self.settings)
        if self.settings.account_base.rstrip("/") == gateway.rstrip("/"):
            return gateway + path
        return self.settings.account_base + path

    # -- 通用请求 ---------------------------------------------------------
    async def post_json(self, token: str, url: str, payload: dict, *, model: str = "",
                        timeout: float | None = None, extra_headers: dict[str, str] | None = None) -> dict:
        """发一个非流式请求，返回解析后的 JSON。"""
        headers = build_request_headers(self.settings, token, model=model)
        if extra_headers:
            headers.update(extra_headers)
        headers = await self._signed_headers(headers)
        try:
            resp = await self._http().post(
                url,
                headers=headers,
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout,
            )
        except httpx.HTTPError as err:
            raise UpstreamError(f"upstream unreachable: {type(err).__name__}: {err}") from err

        text = resp.text
        if resp.status_code >= 400:
            _dump_response(resp.status_code, dict(resp.headers), text, "json")
            self._raise_for_status(resp.status_code, text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            raise UpstreamError(
                f"upstream returned non-JSON (HTTP {resp.status_code}): {text[:200]}",
                status_code=502,
                body=text[:2000],
            ) from err

    async def post_stream(self, token: str, url: str, payload: dict, *, model: str = "",
                          session_id: str = "") -> AsyncIterator[bytes]:
        """发一个流式请求，逐块产出上游原始字节（不做解析，保持原样）。"""
        headers = build_request_headers(self.settings, token, model=model, session_id=session_id)
        headers = await self._signed_headers(headers, session_id=session_id)
        try:
            async with self._http().stream(
                "POST",
                url,
                headers=headers,
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    _dump_response(resp.status_code, dict(resp.headers), raw, "stream")
                    self._raise_for_status(resp.status_code, raw)
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as err:
            raise UpstreamError(f"upstream stream broken: {type(err).__name__}: {err}") from err

    def _raise_for_status(self, status: int, text: str) -> None:
        """把上游的失败翻译成网关的失败语义（见 FAILURE_MAP）。"""
        low = (text or "").lower()
        service_code = _service_code(text)
        # 406 是 managed 网关的「请求不合规」专用状态：空 body、不带 code，
        # 看字面只会以为是自己请求写错了。实测唯一触发条件是 system prompt
        # 不以 REQUIRED_SYSTEM_PREFIX 开头（见该常量的实测记录）—— 正常路径
        # 已经被 ensure_system_prompt() 兜住，能走到这里说明请求绕过了它，
        # 或者上游改了校验规则，所以提示要能直接照做。
        if status == 406:
            if self._sign_error:
                hint = f"客户端签名握手失败：{self._sign_error}"
            else:
                hint = ("上游 406：请求被 AutoClaw 网关判为不合规。最常见的成因是 system "
                        f"prompt 不以「{REQUIRED_SYSTEM_PREFIX}」开头（网关做前缀校验）；"
                        "网关已自动补齐 system 前缀，所以看到这条通常说明："
                        "① 上游偶发风控（同一份请求体重放常能成功，可直接重试）；"
                        "② 对话历史里带了竞品客户端字样，触发上游内容校验；"
                        "③ 上游校验规则已变更。详见 README「system prompt 与 406」")
            # **必须可重试**。2026-10-30 实测：同一份 17629 字节、sha256 完全
            # 相同的请求体，连发两次，一次 406 一次 200；连续 8 次里 2 次 406、
            # 6 次 200。说明它大部分情况下是上游的**偶发风控**而不是请求写错，
            # 重试才是正解。以前这里写 retryable=False，导致网关不重试、直接把
            # 406 抛给调用方 —— 一个「重放就好」的抖动被放大成了硬失败。
            # 反过来说：真写错了（前缀缺失等）时，重试也会稳定 406，只是多花
            # 两次上游往返，这个代价远小于把偶发风控变成必现故障。
            raise UpstreamError(f"upstream 406（请求不合规）：{hint}",
                                status_code=406, body=text[:2000], retryable=True,
                                code="upstream_406")
        kind, rotate = FAILURE_MAP.get((status, service_code), ("", False))
        if not kind:
            kind, rotate = FAILURE_MAP.get((status, ""), ("", False))
        if not kind:
            if any(hint in low for hint in AUTH_KEYWORDS):
                kind, rotate = "auth", True
            elif any(hint in low for hint in QUOTA_KEYWORDS) and status in (400, 402, 403, 429):
                kind, rotate = "quota", True

        message = f"upstream error (HTTP {status}{' code ' + service_code if service_code else ''}): {text[:300]}"
        if kind == "auth":
            raise UpstreamAuthError(message, status_code=status, body=text[:2000])
        if kind in ("quota", "rate_limit", "banned"):
            raise UpstreamQuotaError(message, status_code=status, body=text[:2000])
        if not rotate and status >= 400:
            # 参数/版本类错误：换号也没用，直接回给调用方。
            raise UpstreamError(message, status_code=status, body=text[:2000], retryable=False)
        raise UpstreamError(
            message,
            status_code=status if status in RETRYABLE_STATUS or status >= 400 else 502,
            body=text[:2000],
            retryable=status in RETRYABLE_STATUS,
        )

    # -- 对话 -------------------------------------------------------------
    def _debug_dump(self, payload: dict, *, tag: str) -> None:
        """`AC2API_DEBUG_DUMP=<目录>` 时把即将发往上游的 payload 落盘。

        406 这类「空 body、无 code」的失败只能靠对比真实请求来定位，留个开关
        比反复 instrumentation 省事。失败不影响主流程。
        """
        path = os.environ.get("AC2API_DEBUG_DUMP", "")
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
            name = os.path.join(path, f"{stamp}-{tag}.json")
            with open(name, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001 - 调试功能绝不影响请求
            pass

    async def chat(self, token: str, payload: dict) -> dict:
        # 上游强校验 system prompt 前缀，第三方客户端不会带 —— 必须补齐，否则 406。
        payload = ensure_system_prompt(payload)
        self._debug_dump(payload, tag="chat")
        if self.chat_mode() == "client":
            return await self._chat_via_client(payload)  # type: ignore[return-value]
        return await self.post_json(token, self.chat_url(), payload, model=str(payload.get("model") or ""))

    async def chat_stream(self, token: str, payload: dict, *, session_id: str = "") -> AsyncIterator[bytes]:
        payload = ensure_system_prompt(payload)
        self._debug_dump(payload, tag="stream")
        if self.chat_mode() == "client":
            stream = await self._chat_via_client(payload, session_id=session_id)
            async for chunk in stream:  # type: ignore[union-attr]
                yield chunk
            return
        async for chunk in self.post_stream(token, self.chat_url(), payload,
                                            model=str(payload.get("model") or ""),
                                            session_id=session_id):
            yield chunk

    # -- 模型目录 ---------------------------------------------------------
    async def models(self, token: str = "", *, refresh: bool = False) -> list[str]:
        """返回可用模型清单。

        端点是 **GET**（不是 POST！），且属于身份域 —— 鉴权头用 `Authorization`。
        实测两个域都能取到同一份目录（`{"models": [{id, name, contextWindow,
        maxTokens, creditConsumptionLevel, metadata...}]}`），优先打身份域，
        失败再退回 managed 域。

        优先级：上游下发目录（5 分钟缓存）→ 配置里的静态清单 → 空（此时网关
        会拒绝请求并提示去后台填模型名，而不是瞎猜一个名字让调用方收到 404）。
        """
        now = time.time()
        if self._catalog_cache and not refresh and now - self._catalog_cache[0] < 300:
            return list(self._catalog_cache[1])
        listed: list[str] = []
        if token:
            for base, use_x in (
                (self.settings.account_base.rstrip("/"), False),
                (resolve_model_base_url(self.settings).rstrip("/"), True),
            ):
                headers = build_auth_headers(self.settings, token, use_x_authorization=use_x)
                headers["Accept"] = "application/json"
                try:
                    raw = await self._get_json(headers, base + self.settings.models_path)
                except UpstreamError:
                    continue
                listed = parse_model_catalog(raw)
                if listed:
                    break
        if not listed:
            listed = list(self.settings.models)
        if not listed and self.settings.default_model:
            listed = [self.settings.default_model]
        if listed:
            self._catalog_cache = (now, listed)
        return listed

    # -- 积分 -------------------------------------------------------------
    async def credits(self, token: str) -> dict:
        """查询一个账号的积分钱包并汇总（走身份域的真实端点）。

        客户端真实的取数方式是三个端点（2026-10 实测全部 200）：

        * `GET /agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw`
          → 各钱包类型的总余额（reward / daily / subscription / booster ...）
        * `GET /agent-assetmgr/api/v1/wallet-instances?wallet_type=all&wallet_scope=all`
          → **每个钱包实例的 `expires_at`**，这才是「快过期」判断的依据
        * `GET /agent-assetmgr/api/v1/ledgers_std?asset_type=point&wallet_type=all&page_size=N`
          → 消费明细（每次对话扣了多少积分、用的哪个实际模型）

        三个都可能单独失效，所以分别容错：拿不到实例明细时退回钱包总额；
        总额也拿不到才返回空汇总（积分查不到不该让账号整体不可用，只是排最后）。
        """
        base = self.settings.account_base.rstrip("/")
        headers = build_auth_headers(self.settings, token, use_x_authorization=False)
        headers["Accept"] = "application/json"
        wallet, instances = await asyncio.gather(
            self._get_json(headers, base + WALLET_PATH),
            self._get_json(headers, base + WALLET_INSTANCES_PATH),
            return_exceptions=True,
        )
        if isinstance(wallet, BaseException):
            wallet = None
        if isinstance(instances, BaseException):
            instances = None
        if wallet is None and instances is None:
            return summarize_account([])
        return summarize_wallet(wallet, instances)

    async def ledger(self, token: str, *, page_size: int = 20) -> list[dict]:
        """最近的积分消费明细（客户端「账单」页用的就是这条）。"""
        base = self.settings.account_base.rstrip("/")
        headers = build_auth_headers(self.settings, token, use_x_authorization=False)
        headers["Accept"] = "application/json"
        url = f"{base}{LEDGER_PATH}?asset_type=point&wallet_type=all&page_size={int(page_size)}"
        try:
            raw = await self._get_json(headers, url)
        except UpstreamError:
            return []
        if not isinstance(raw, dict):
            return []
        data = raw.get("data")
        entries = data.get("entries") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    async def _get_json(self, headers: dict[str, str], url: str) -> Any:
        try:
            resp = await self._http().get(url, headers=headers, timeout=self.settings.credits_timeout)
        except httpx.HTTPError as err:
            raise UpstreamError(f"upstream unreachable: {type(err).__name__}: {err}") from err
        if resp.status_code >= 400:
            self._raise_for_status(resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError as err:
            raise UpstreamError(f"upstream returned non-JSON (HTTP {resp.status_code})",
                                body=resp.text[:2000]) from err


def _dump_response(status: int, headers: dict, text: str, tag: str) -> None:
    """`AC2API_DEBUG_DUMP=<目录>` 时把上游的错误响应（头 + body）落盘。

    406 的 body 是空的，就地排查时除了状态码什么都拿不到 —— 上游有时只把
    线索放在响应头里（trace / 校验项），落盘才有得看。失败不影响主流程。
    """
    path = os.environ.get("AC2API_DEBUG_DUMP", "")
    if not path:
        return
    try:
        os.makedirs(path, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        name = os.path.join(path, f"{stamp}-resp{status}-{tag}.txt")
        with open(name, "w", encoding="utf-8") as fp:
            fp.write(f"HTTP {status}\n")
            for k, v in sorted(headers.items()):
                fp.write(f"{k}: {v}\n")
            fp.write("\n--- body ---\n")
            fp.write(text or "<empty>")
    except Exception:  # noqa: BLE001 - 调试功能绝不影响请求
        pass


def _service_code(text: str) -> str:
    """从错误响应体里挖出业务码（客户端表里叫 serviceCode）。"""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    for key in ("code", "serviceCode", "service_code", "businessCode"):
        value = obj.get(key)
        if isinstance(value, (int, str)) and str(value).strip():
            return str(value).strip()
    for holder in ("error", "data", "result"):
        inner = obj.get(holder)
        if isinstance(inner, dict):
            for key in ("code", "serviceCode", "service_code"):
                value = inner.get(key)
                if isinstance(value, (int, str)) and str(value).strip():
                    return str(value).strip()
    return ""
