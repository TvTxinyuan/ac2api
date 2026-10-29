"""accessToken 的来源：refreshToken 续期 → 本机凭据 → 外部服务 → 手工填。

为什么需要这个模块
------------------
用 AutoClaw 网关时，聊天请求必须带 `X-Authorization: Bearer <accessToken>`。
accessToken 是短命的（几小时），一直靠人手贴是没法用的。客户端自己的续期链路是：

    POST {identity_gateway}/userapi/v1/refresh
    {"refresh_token": <rt>, "source_id": "autoclaw", "device_id": <设备 ID>}
    -> {"code": 0, "data": {"access_token": ..., "refresh_token": ...}}
    业务码 400002 时回退 POST /userapi/v1/agent-refresh（同样的 body）

而 refreshToken 就躺在客户端的账号目录里（Chromium `os_crypt` + DPAPI 加密的
`account-credentials.enc`）。所以本模块把这条路打通，**不需要抓包、不需要跑客户端**：

    1. 用 DPAPI 解出 AES-GCM 主密钥（`Local State` 的 `os_crypt.encrypted_key`）；
    2. 用它解 `accounts/<accountKey>/account-credentials.enc` 拿到 refreshToken；
    3. 拿 refreshToken 去 `/userapi/v1/refresh` 换新的 accessToken。

换回来的新 refreshToken 会回写到账号目录（等价于客户端自己续期），所以这份
凭据不会因为「网页端登录过」而被顶掉。全程只读本机自己账户的数据，不涉及他人。

也可以完全不走本机：`AC2API_TOKEN_URL` 指向任何能吐出 token 的服务即可。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# 常量（来自客户端 main.cjs / 实测）
# ---------------------------------------------------------------------------
#: 客户端 userData 目录（Windows 安装版）。
CLIENT_DATA_HINTS = (
    "{APPDATA}/AutoClaw-official",
    "{APPDATA}/AutoClaw",
    "{APPDATA}/Zwork-official",
    "{HOME}/.config/AutoClaw-official",
    "{HOME}/Library/Application Support/AutoClaw-official",
)
#: `Local State` 里的 Chromium 主密钥字段。
LOCAL_STATE = "Local State"
#: 账号目录下的凭据文件。
CREDENTIAL_FILE = "account-credentials.enc"
#: 账号指针文件名（客户端用它记录「当前账号」）。
POINTER_FILE = "account-pointer.json"

#: 续期端点（identity_gateway 域下）。
REFRESH_PATH = "/userapi/v1/refresh"
AGENT_REFRESH_PATH = "/userapi/v1/agent-refresh"
#: 业务码 400002 = 该端点不认这个 refreshToken，回退 agent-refresh。
FALLBACK_CODE = 400002
#: 续期时上报的来源标识。
SOURCE_ID = "autoclaw"

#: accessToken 剩余寿命低于这个秒数就提前续期（避免请求途中过期）。
REFRESH_LEEWAY_S = 180.0
#: 续期失败后的最短重试间隔，避免每次请求都去撞上游。
REFRESH_FAIL_BACKOFF_S = 60.0


class TokenError(Exception):
    """拿不到可用 accessToken。"""


# ---------------------------------------------------------------------------
# Windows DPAPI + AES-GCM
# ---------------------------------------------------------------------------
def _dpapi_unprotect(blob: bytes) -> bytes:
    """调 Windows CryptUnprotectData（当前用户作用域）。"""
    if os.name != "nt":
        raise TokenError("DPAPI 只能在 Windows 上解，当前系统请用 AC2API_TOKEN_URL")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise TokenError("DPAPI 解密失败（是否换了 Windows 用户？）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _aes_gcm_open(key: bytes, payload: bytes) -> bytes:
    """Chromium v10 密文：3 字节前缀 + 12 字节 nonce + 密文 + 16 字节 tag。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作提示
        raise TokenError("需要 cryptography 库（pip install cryptography）") from exc
    if len(payload) < 3 + 12 + 16:
        raise TokenError("凭据文件太短，格式不符")
    nonce = payload[3:15]
    return AESGCM(key).decrypt(nonce, payload[15:], None)


def _master_key(data_dir: str) -> bytes:
    path = os.path.join(data_dir, LOCAL_STATE)
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    raw = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if not raw.startswith(b"DPAPI"):
        raise TokenError("Local State 里的密钥不是 DPAPI 格式")
    return _dpapi_unprotect(raw[5:])


# ---------------------------------------------------------------------------
# 客户端账号目录
# ---------------------------------------------------------------------------
def candidate_data_dirs() -> list[str]:
    """可能存放客户端数据的目录（`AC2API_CLIENT_DATA` 优先）。"""
    out: list[str] = []
    override = os.environ.get("AC2API_CLIENT_DATA", "").strip()
    if override:
        out.append(override)
    subs = {
        "APPDATA": os.environ.get("APPDATA", ""),
        "HOME": os.path.expanduser("~"),
    }
    for hint in CLIENT_DATA_HINTS:
        path = hint
        for name, value in subs.items():
            path = path.replace("{" + name + "}", value)
        if "{" in path or not path:
            continue
        if os.path.isdir(path) and path not in out:
            out.append(path)
    return out


def strip_bearer(token: str) -> str:
    """去掉 `Bearer ` 前缀。凭据里存的 token 是带前缀的，而请求体里要裸 token。"""
    token = (token or "").strip()
    return token[7:].strip() if token.lower().startswith("bearer ") else token


@dataclass
class LocalCredential:
    """从本机客户端凭据文件里解出来的一份账号凭据。"""

    data_dir: str
    account_key: str
    access_token: str
    refresh_token: str
    device_id: str
    #: 凭据文件路径与解密出的原始 JSON，续期后可回写。
    path: str = ""

    @property
    def has_refresh(self) -> bool:
        return bool(self.refresh_token)


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _account_keys(data_dir: str) -> list[str]:
    """列出账号目录，当前账号排最前（有 account-pointer 就按指针）。"""
    root = os.path.join(data_dir, "accounts")
    try:
        names = [n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))]
    except OSError:
        return []
    # 指针文件名各版本不一，这里宽松地扫一遍键名里含 account 的顶层 json。
    current = ""
    for name in os.listdir(data_dir):
        if not name.endswith(".json") or "account" not in name.lower():
            continue
        blob = _read_json(os.path.join(data_dir, name))
        if isinstance(blob, dict):
            for key in ("accountKey", "account_key", "activeAccountKey", "active"):
                value = blob.get(key)
                if isinstance(value, str) and value in names:
                    current = value
                    break
        if current:
            break
    if current:
        names.remove(current)
        names.insert(0, current)
    return names


def _jwt_device_id(token: str) -> str:
    """从 accessToken 的 JWT payload 里读 device_id。

    实测客户端签发的 token 里就有 `device_id`（`source_id` 为 `agent` /
    `autoclaw`），比凭据文件可靠 —— 凭据里根本没有这个字段。
    """
    raw = (token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) < 2:
        return ""
    body = parts[1]
    body += "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except (ValueError, UnicodeEncodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("device_id", "deviceId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _device_id_of(data_dir: str, blob: dict[str, Any], access_token: str = "") -> str:
    """设备 ID：优先凭据里的，其次 token 里的 JWT 声明，再次客户端其它配置里的。"""
    for key in ("deviceId", "device_id"):
        value = blob.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    from_jwt = _jwt_device_id(access_token)
    if from_jwt:
        return from_jwt
    cfg = _read_json(os.path.join(data_dir, "device.json"))
    if isinstance(cfg, dict):
        for key in ("deviceId", "device_id", "id"):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    # 兜底：客户端首次运行生成的持久 UUID 文件。
    for name in ("device-id", "deviceId", "machine-id"):
        path = os.path.join(data_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            continue
        if text:
            return text
    return ""


def read_local_credentials(data_dir: str) -> list[LocalCredential]:
    """解出该目录下所有账号的凭据（当前账号优先）。"""
    key = _master_key(data_dir)
    out: list[LocalCredential] = []
    for account_key in _account_keys(data_dir):
        path = os.path.join(data_dir, "accounts", account_key, CREDENTIAL_FILE)
        try:
            with open(path, "rb") as fh:
                payload = fh.read()
            blob = json.loads(_aes_gcm_open(key, payload).decode("utf-8"))
        except (OSError, ValueError, TokenError):
            continue
        if not isinstance(blob, dict):
            continue
        access = str(blob.get("accessToken") or blob.get("access_token") or "").strip()
        refresh = str(blob.get("refreshToken") or blob.get("refresh_token") or "").strip()
        out.append(LocalCredential(
            data_dir=data_dir,
            account_key=account_key,
            access_token=access,
            refresh_token=refresh,
            device_id=_device_id_of(data_dir, blob, access),
            path=path,
        ))
    return out


def find_local_credentials(account_key: str = "") -> list[LocalCredential]:
    """在可能的目录里找凭据；给了 account_key 就只返回那一份。

    `account_key` 既可以是凭据里的账号哈希（64 位十六进制），也可以是网关
    自己的账号标识。网关在「只用一个本机账号」的场景会造一个 key 叫 `local`
    的兜底账号 —— 这种非哈希 key 不应该被当成筛选条件卡死，否则就变成
    「装了客户端却提示找不到凭据」。
    """
    key = (account_key or "").strip()
    if key and not _looks_like_account_key(key):
        key = ""
    found: list[LocalCredential] = []
    for data_dir in candidate_data_dirs():
        try:
            creds = read_local_credentials(data_dir)
        except (OSError, ValueError, TokenError):
            continue
        for cred in creds:
            if not key or cred.account_key == key:
                found.append(cred)
    return found


def _looks_like_account_key(key: str) -> bool:
    """账号哈希的形态：长十六进制串（AutoClaw 用 64 位 hex）。"""
    if len(key) < 16 or len(key) > 128:
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in key)


# ---------------------------------------------------------------------------
# JWT 解析（只为判断过期，不校验签名）
# ---------------------------------------------------------------------------
def jwt_expiry(token: str) -> float:
    """取 JWT 的 exp（秒级时间戳）；解析不出来返回 0。"""
    parts = (token or "").strip().removeprefix("Bearer ").strip().split(".")
    if len(parts) < 2:
        return 0.0
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0.0
    exp = payload.get("exp") if isinstance(payload, dict) else None
    return float(exp) if isinstance(exp, (int, float)) else 0.0


def is_expired(token: str, *, leeway: float = REFRESH_LEEWAY_S, now: float | None = None) -> bool:
    """token 是否已过期（或将在 leeway 秒内过期）。解析不出 exp 时按「未过期」处理。"""
    exp = jwt_expiry(token)
    if not exp:
        return False
    return exp - leeway <= (time.time() if now is None else now)


# ---------------------------------------------------------------------------
# 续期
# ---------------------------------------------------------------------------
@dataclass
class RefreshedToken:
    access_token: str
    refresh_token: str


def parse_refresh_response(blob: Any) -> RefreshedToken | None:
    """解析续期响应：宽松处理 `data`/`result` 包裹与 camel/snake 两种命名。

    实测成功响应是 `{"code":0,"data":{"access_token":..,"refresh_token":..}}`；
    这里多认一层 `result`，免得服务端换壳就失效。
    """
    if not isinstance(blob, dict):
        return None
    inner: Any = blob
    for holder in ("data", "result"):
        value = blob.get(holder)
        if isinstance(value, dict):
            inner = value
            break
    access = refresh = ""
    for key in ("access_token", "accessToken"):
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            access = value.strip()
            break
    for key in ("refresh_token", "refreshToken"):
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            refresh = value.strip()
            break
    if not access:
        return None
    return RefreshedToken(access_token=access, refresh_token=refresh)


def refresh_access_token(base_url: str, refresh_token: str, device_id: str, *, timeout: float = 15.0,
                         settings: Any = None) -> RefreshedToken:
    """用 refreshToken 换一套新凭据。`base_url` 是 identity_gateway 的 base_url。"""
    if not refresh_token:
        raise TokenError("没有 refreshToken，无法续期")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings is not None:
        # 续期也要求签名头，否则被网关判 400。
        from .upstream import build_auth_headers

        headers.update(build_auth_headers(settings, ""))
    body = {"refresh_token": strip_bearer(refresh_token), "source_id": SOURCE_ID, "device_id": device_id}
    last = ""
    # trust_env=False：不读系统代理。Windows 的"Internet 选项"代理（clash 之类）
    # 对 127.0.0.1 的豁免 httpx 不认，会让本机 token 服务的请求被塞给代理，
    # 表现为空 body 的 502，很难和"token 服务没起来"区分。见 upstream._http。
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        for path in (REFRESH_PATH, AGENT_REFRESH_PATH):
            try:
                resp = client.post(base_url.rstrip("/") + path, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last = f"{path}: {exc}"
                continue
            try:
                blob = resp.json()
            except ValueError:
                last = f"{path}: HTTP {resp.status_code} 非 JSON"
                continue
            code = blob.get("code") if isinstance(blob, dict) else None
            parsed = parse_refresh_response(blob)
            if parsed:
                return parsed
            last = f"{path}: HTTP {resp.status_code} code={code}"
            if code != FALLBACK_CODE:
                break
    raise TokenError(f"续期失败（{last}）")


def _write_back(cred: LocalCredential, new: RefreshedToken) -> None:
    """把新凭据写回客户端凭据文件（失败不影响网关可用）。"""
    if not cred.path or not new.refresh_token:
        return
    try:
        key = _master_key(cred.data_dir)
        with open(cred.path, "rb") as fh:
            blob = json.loads(_aes_gcm_open(key, fh.read()).decode("utf-8"))
        if not isinstance(blob, dict):
            return
        blob["accessToken"] = new.access_token
        blob["refreshToken"] = new.refresh_token
        blob.setdefault("access_token", new.access_token)
        blob.setdefault("refresh_token", new.refresh_token)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        sealed = b"v10" + nonce + AESGCM(key).encrypt(nonce, json.dumps(blob).encode("utf-8"), None)
        tmp = cred.path + ".ac2api.tmp"
        with open(tmp, "wb") as fh:
            fh.write(sealed)
        os.replace(tmp, cred.path)
    except Exception:  # noqa: BLE001 - 回写是加分项，不能因此中断对话
        return


class TokenSource:
    """按优先级拿 accessToken：手工/外部服务 → 本机凭据 → refreshToken 续期。

    结果按 (账号, token) 缓存，过期前不重复解密/续期；续期失败有退避，
    不会因为上游抖动把每个请求都变慢。
    """

    def __init__(self, settings: Any):
        self.settings = settings
        self._cache: dict[str, tuple[str, float]] = {}   # source_key -> (token, next_check_at)
        self._failed: dict[str, float] = {}              # source_key -> 下次允许重试时间
        self.last_error = ""

    # -- 对外 ---------------------------------------------------------------
    def token_for(self, account_key: str = "", *, force: bool = False) -> str:
        """给某个账号取一个可用 token（空 account_key = 默认号）。"""
        key = account_key or "__default__"
        now = time.time()
        cached = self._cache.get(key)
        if cached and not force and cached[1] > now:
            return cached[0]
        if not force and self._failed.get(key, 0) > now:
            # 刚失败过：有旧 token 就先用着，等退避结束再试。
            return cached[0] if cached else ""

        token = ""
        try:
            token = self._acquire(account_key, force=force)
        except TokenError as exc:
            self.last_error = str(exc)
            self._failed[key] = now + REFRESH_FAIL_BACKOFF_S
            return cached[0] if cached else ""
        if not token:
            return cached[0] if cached else ""
        self._failed.pop(key, None)
        self._cache[key] = (token, time.time() + 60)
        return token

    def invalidate(self, account_key: str = "") -> None:
        """上游说 token 无效：下次强制重取。"""
        self._cache.pop(account_key or "__default__", None)

    # -- 内部 ---------------------------------------------------------------
    def _acquire(self, account_key: str, *, force: bool) -> str:
        static = self.settings.static_token.strip()
        if static and not account_key:
            if not is_expired(static):
                return static
            # 手工给的过期了：如果能续期就续，否则照旧返回，让上游明确报错。
        external = self._from_service()
        if external:
            return external

        creds = find_local_credentials(account_key)
        if not creds:
            if static:
                return static
            raise TokenError("本机找不到 AutoClaw 凭据（可用 AC2API_STATIC_TOKEN / AC2API_TOKEN_URL）")
        cred = creds[0]
        if cred.access_token and not force and not is_expired(cred.access_token):
            return cred.access_token
        if not cred.has_refresh:
            if cred.access_token:
                return cred.access_token
            raise TokenError("凭据里没有 refreshToken，无法续期")
        # device_id 实测**不能为空**（空值上游回 400001）。凭据里通常没有这个
        # 字段，所以退化用账号哈希 —— 它与客户端请求里用的值一致。
        device_id = cred.device_id or _jwt_device_id(cred.access_token) or cred.account_key
        new = refresh_access_token(self.settings.account_base, cred.refresh_token, device_id,
                                   settings=self.settings)
        _write_back(cred, new)
        return new.access_token

    def _from_service(self) -> str:
        url = self.settings.token_url.strip()
        if not url:
            return ""
        from .upstream import fetch_remote_token

        try:
            return fetch_remote_token(url)
        except Exception:  # noqa: BLE001 - 外部服务挂了不应阻断本机凭据
            return ""


def device_id_for(account_key: str = "") -> str:
    """取本机该账号的 device_id（续期/请求头都可能用到）。"""
    creds = find_local_credentials(account_key)
    return creds[0].device_id if creds else ""
