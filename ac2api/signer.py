"""客户端请求签名（zwork.zhipu_client_sign 的 Python 还原版）。

> **现状：本模块默认不启用，也不需要。** 早期排查以为 `406` 是签名缺失导致
> 的，后来实测证明 `406` 来自上游的三条请求校验：system prompt 必须以
> `"You are AutoClaw. Answer the user directly and concisely."` 开头
> （`upstream.REQUIRED_SYSTEM_PREFIX`）、正文不能出现竞品身份句
> （`upstream.scrub_text`）、`max_tokens` 必须落在 `[10000, 131072]`
> （`upstream.normalize_max_tokens`）。三条补齐之后，不带任何签名头也能正常
> 对话。本模块保留给「上游改成真校验签名」时的备选，以及自建上游做逆向实验。

## 这个签名是什么

managed 模型网关（`…/autoclaw-proxy/proxy/autoclaw/chat/completions`）的
客户端 `zwork.zhipu_client_sign` 会额外发一组由**客户端持有私钥**生成的
签名头（`X-Client-Sig` / `X-Client-Pow`）。这套机制是从客户端 bundle 里
逐行读出来的：

1. 客户端手里有一对凭证 `id.secret`（`eGr` 用 `.` 切成两段，缺一不可）。
2. 用 `secret` 走 HKDF-SHA256（salt=`WD_CLIENT_SIGN_KDF_SALT`）派生两把密钥：
   - info `getSignKey_hmac` → 握手请求的 HMAC 密钥；
   - info `ed25519_priv`   → 解开握手响应的 AES-256-GCM 密钥。
3. `POST {origin}/api/paas/c1f3a7e2/v2/client`，body
   `{apiKey: "id.secret", ts, nonce, sig}`，其中
   `sig = base64(hmac_sha256(hmac_key, "get_sign_key\n{id}\n{ts}\n{nonce}"))`；
   鉴权头 `Authorization: Bearer id.secret`。
4. 响应 `data.privateCipher` 是 base64(AES-256-GCM)：
   前 12 字节 IV、末 16 字节 tag、AAD 是 `id`，明文是 base64 的 PKCS#8 DER
   Ed25519 私钥。
5. 之后每个请求加这些头：

   | 头 | 值 |
   | --- | --- |
   | `X-Session-Id` | 会话 id（探测连接时固定 `connection-probe`） |
   | `X-Client-Ts` | 毫秒时间戳 |
   | `X-Client-Version` | 客户端版本号 |
   | `X-Client-Nonce` | 16 字节随机 hex |
   | `X-Client-Sig` | base64(Ed25519(`"id\nts\nversion\nsessionId\nnonce"`)) |
   | `X-App-Id` | app id（默认 `autoclaw`） |
   | `X-Client-Pow` | 工作量证明，见下 |
   | `Authorization` | 原请求没有才补 `Bearer id.secret` |

6. 工作量证明：`digest = sha256("{id}\n{appId}\n{sessionId}\n{ts}")` 取 hex
   前 32 个字符，然后从 `t=0` 递增找 `sha256("{digest}\n{t}")` 的前导零比特数
   ≥ 8 的第一个 `t`（`Math.clz32` 口径：整个 32 位字的高位零）。

## 这个模块只负责「有凭证时怎么签」

凭证从哪来不归它管（见 `signkey` 模块）。没有凭证时调用方应当走别的路径，
而不是伪造签名 —— 服务端会回 `VERIFY_SIGNATURE_INVALID`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

try:  # 可选依赖：只有真的要签名时才需要
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    HAVE_CRYPTO = True
except Exception:  # noqa: BLE001 - 没装 cryptography 时退化成「不能签」
    HAVE_CRYPTO = False

# --- 客户端常量（原样照抄，改动即失效） -------------------------------------
HANDSHAKE_PATH = "/api/paas/c1f3a7e2/v2/client"
KDF_SALT = b"WD_CLIENT_SIGN_KDF_SALT"
KDF_INFO_HMAC = b"getSignKey_hmac"
KDF_INFO_ED25519 = b"ed25519_priv"
GCM_IV_LEN = 12
GCM_TAG_LEN = 16
DEFAULT_APP_ID = "autoclaw"
DEFAULT_SESSION_ID = "connection-probe"
POW_BITS = 8
POW_MAX_ITERATIONS = 5_000_000
# 客户端只对 bigmodel.cn / z.ai 系域名启用签名（`tGr`）。
SIGN_HOSTS = ("bigmodel.cn", "z.ai")

# 服务端验签失败时返回的错误码（客户端据此决定 rekey / hard_stop）
REKEY_CODES = frozenset({"VERIFY_SIGNATURE_INVALID", "VERIFY_APIKEY_EXPIRED"})
HARD_STOP_CODES = frozenset({
    "VERIFY_POW_REQUIRED", "VERIFY_POW_INVALID", "VERIFY_TS_OUT_OF_WINDOW",
    "VERIFY_MISSING_FIELD", "VERIFY_INVALID_FORMAT", "VERIFY_PROTOCOL_ERROR",
    "VERIFY_APIKEY_DISABLED",
})


class SignError(Exception):
    """签名链路本身出问题（凭证格式、握手被拒、没装 cryptography）。"""


def parse_credential(raw: str) -> tuple[str, str] | None:
    """`id.secret` → (id, secret)；格式不对返回 None（对应客户端 `eGr`）。"""
    text = (raw or "").strip()
    parts = text.split(".")
    if len(parts) != 2:
        return None
    key_id, secret = parts
    if not key_id or not secret:
        return None
    if key_id != key_id.strip() or secret != secret.strip():
        return None
    return key_id, secret


def supports_signing(url: str) -> bool:
    """客户端只为 bigmodel.cn / z.ai 系域名签名，其它域名一律不加签名头。"""
    if not HAVE_CRYPTO:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    return any(host == root or host.endswith("." + root) for root in SIGN_HOSTS)


def _hkdf(secret: str, info: bytes) -> bytes:
    if not HAVE_CRYPTO:
        raise SignError("签名需要 cryptography（pip install cryptography）")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=KDF_SALT, info=info).derive(
        secret.encode("utf-8")
    )


def handshake_body(key_id: str, secret: str, *, ts: str, nonce: str) -> dict[str, str]:
    """握手请求体。

    签名串 `get_sign_key\n{id}\n{ts}\n{nonce}` —— 分隔符是**换行**，不是空格
    （bundle 里是模板字符串的裸换行，`x8t` 的模板字面量跨行写）。
    """
    mac_key = _hkdf(secret, KDF_INFO_HMAC)
    message = f"get_sign_key\n{key_id}\n{ts}\n{nonce}"
    digest = hmac.new(mac_key, message.encode("utf-8"), hashlib.sha256).digest()
    api_key = f"{key_id}.{secret}"
    return {
        "apiKey": api_key,
        "ts": ts,
        "nonce": nonce,
        "sig": base64.b64encode(digest).decode("ascii"),
    }


def decrypt_private_key(key_id: str, secret: str, private_cipher: str) -> Any:
    """把握手响应里的 `privateCipher` 解成 Ed25519 私钥对象。"""
    if not HAVE_CRYPTO:
        raise SignError("签名需要 cryptography（pip install cryptography）")
    try:
        blob = base64.b64decode(private_cipher, validate=True)
    except Exception as err:  # noqa: BLE001
        raise SignError(f"privateCipher 不是合法 base64：{err}") from err
    if len(blob) <= GCM_IV_LEN + GCM_TAG_LEN:
        raise SignError("privateCipher 太短")
    key = _hkdf(secret, KDF_INFO_ED25519)
    iv = blob[:GCM_IV_LEN]
    tag = blob[-GCM_TAG_LEN:]
    ciphertext = blob[GCM_IV_LEN:-GCM_TAG_LEN]
    try:
        # cryptography 的 AESGCM 期望 tag 直接拼在密文尾部。
        plain = AESGCM(key).decrypt(iv, ciphertext + tag, key_id.encode("utf-8"))
    except Exception as err:  # noqa: BLE001
        raise SignError(f"私钥解密失败（凭证不匹配？）：{err}") from err
    try:
        return serialization.load_der_private_key(base64.b64decode(plain), password=None)
    except Exception as err:  # noqa: BLE001
        raise SignError(f"私钥格式不对：{err}") from err


def leading_zero_bits(digest: bytes) -> int:
    """等价于客户端的 `rGr`：高字节起数连续零比特。"""
    count = 0
    for byte in digest:
        if byte == 0:
            count += 8
            continue
        # 256 // byte 的 bit_length 再减 1，就是该字节的前导零
        count += 8 - byte.bit_length()
        break
    return count


def solve_pow(key_id: str, app_id: str, session_id: str, ts: str, *,
              bits: int = POW_BITS, max_iterations: int = POW_MAX_ITERATIONS) -> str:
    """算工作量证明；找不到（理论上不会）返回空串，客户端也是如此。

    种子与计数器之间同样是换行：`sha256("{seed}\n{counter}")`。
    """
    seed = hashlib.sha256(f"{key_id}\n{app_id}\n{session_id}\n{ts}".encode("utf-8")).hexdigest()[:32]
    for counter in range(max_iterations):
        digest = hashlib.sha256(f"{seed}\n{counter}".encode("utf-8")).digest()
        if leading_zero_bits(digest) >= bits:
            return str(counter)
    return ""


@dataclass
class SignedHeaders:
    """一次请求的签名头；`as_dict` 给 httpx 用。"""

    session_id: str
    ts: str
    version: str
    nonce: str
    signature: str
    pow: str = ""
    app_id: str = DEFAULT_APP_ID
    api_key: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, str]:
        headers = {
            "X-Session-Id": self.session_id,
            "X-Client-Ts": self.ts,
            "X-Client-Version": self.version,
            "X-Client-Nonce": self.nonce,
            "X-Client-Sig": self.signature,
        }
        if self.pow:
            headers["X-App-Id"] = self.app_id
            headers["X-Client-Pow"] = self.pow
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra)
        return headers


class ClientSigner:
    """持有一把已握手的私钥，能反复给请求签名（对应客户端 `cJ`）。

    `handshake` 只要给一个「发 POST 拿 JSON」的同步函数，便于测试与替换传输层：
    `handshake(fetch)` 里 `fetch(url, headers, body) -> dict`。
    """

    def __init__(self, *, base_url: str, credential: str, version: str = "",
                 app_id: str = DEFAULT_APP_ID, now: Callable[[], float] | None = None):
        parsed = parse_credential(credential)
        if parsed is None:
            raise SignError("凭证格式应为 id.secret")
        self.base_url = base_url.rstrip("/")
        self.key_id, self.secret = parsed
        self.api_key = f"{self.key_id}.{self.secret}"
        self.version = (version or "").strip()
        self.app_id = app_id or DEFAULT_APP_ID
        self._now = now or time.time
        self._private_key: Any | None = None
        self._fetched_at = 0.0
        self._pending_ts = ""
        self._pending_nonce = ""

    @property
    def handshake_url(self) -> str:
        return self.base_url + HANDSHAKE_PATH

    @property
    def ready(self) -> bool:
        return self._private_key is not None

    def handshake_headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def handshake_payload(self, *, ts: str | None = None, nonce: str | None = None) -> dict[str, str]:
        """本次握手要发的 JSON 体。

        拆出来是为了让**异步**调用方（网关的 httpx.AsyncClient）也能自己发请求，
        只把「怎么发」交给它，算签名和解析响应仍归本类。
        """
        self._pending_ts = ts or str(int(self._now() * 1000))
        self._pending_nonce = nonce or secrets.token_hex(16)
        return handshake_body(self.key_id, self.secret,
                              ts=self._pending_ts, nonce=self._pending_nonce)

    def finish_handshake(self, status: int, text: str) -> None:
        """解析握手响应并装好私钥；失败抛 `SignError`。"""
        try:
            raw = json.loads(text)
        except ValueError as err:
            raise SignError(f"握手响应不是 JSON（HTTP {status}）：{text[:200]}") from err
        if not isinstance(raw, dict):
            raise SignError("握手响应不是 JSON 对象")
        code = raw.get("code")
        data = raw.get("data")
        cipher = data.get("privateCipher") if isinstance(data, dict) else None
        if code != 200 or not isinstance(cipher, str) or not cipher:
            msg = raw.get("msg") or raw.get("message") or "unexpected response"
            raise SignError(f"握手被拒（HTTP {status}）：code={code} msg={msg}")
        self._private_key = decrypt_private_key(self.key_id, self.secret, cipher)
        self._fetched_at = self._now()

    def handshake(self, fetch: Callable[[str, dict[str, str], bytes], dict]) -> None:
        """同步握手。`fetch` 抛异常或响应不合规都会抛 `SignError`。"""
        body = self.handshake_payload()
        raw = fetch(self.handshake_url, self.handshake_headers(),
                    json.dumps(body).encode("utf-8"))
        self.finish_handshake(200, json.dumps(raw))

    def signed_headers(self, *, session_id: str = DEFAULT_SESSION_ID,
                       has_authorization: bool = True) -> dict[str, str]:
        """给一个请求生成签名头（不握手；调用方先 `handshake`）。"""
        if self._private_key is None:
            raise SignError("还没握手，拿不到私钥")
        version = self.version
        if not version:
            raise SignError("没有客户端版本号，无法签名")
        ts = str(int(self._now() * 1000))
        nonce = secrets.token_hex(16)
        # 签名串 `apiKeyId\nts\nversion\nsessionId\nnonce`（bundle: `nGr`）。
        message = f"{self.key_id}\n{ts}\n{version}\n{session_id}\n{nonce}"
        signature = base64.b64encode(self._private_key.sign(message.encode("utf-8"))).decode("ascii")
        pow_value = solve_pow(self.key_id, self.app_id, session_id, ts)
        return SignedHeaders(
            session_id=session_id, ts=ts, version=version, nonce=nonce,
            signature=signature, pow=pow_value, app_id=self.app_id,
            api_key="" if has_authorization else self.api_key,
        ).as_dict()

    def sign(self, headers: dict[str, str] | None, *, session_id: str = DEFAULT_SESSION_ID) -> dict[str, str]:
        """在现有头基础上补签名头（客户端 `signOutgoing` 的行为）。"""
        merged = dict(headers or {})
        has_auth = any(k.lower() in ("authorization", "x-api-key") for k in merged)
        merged.update(self.signed_headers(session_id=session_id, has_authorization=has_auth))
        return merged


def detect_failure_code(body: str) -> str:
    """从响应体里认出 `VERIFY_xxx` 错误码（客户端 `P8t`）。"""
    import re

    match = re.search(r"VERIFY_[A-Z_]+", body or "")
    return match.group(0) if match else ""


def failure_action(code: str) -> str:
    """客户端 `C8t`：rekey（重新握手）/ hard_stop（别再试）/ count_only。"""
    if code in REKEY_CODES:
        return "rekey"
    if code in HARD_STOP_CODES:
        return "hard_stop"
    return "count_only"


def make_signer_from_env(base_url: str, *, version: str = "",
                         app_id: str = DEFAULT_APP_ID) -> ClientSigner | None:
    """从 `AC2API_SIGN_CREDENTIAL`（形如 `id.secret`）建签名器；没配返回 None。"""
    raw = (os.environ.get("AC2API_SIGN_CREDENTIAL") or "").strip()
    if not raw:
        return None
    return ClientSigner(base_url=base_url, credential=raw, version=version, app_id=app_id)
