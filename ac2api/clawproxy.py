"""通过「本机 AutoClaw 客户端」转发对话请求（client 模式）。

> **现状：默认用不到。** 直连模式（`AC2API_CHAT_MODE=direct`）已经实测可用，
> 关掉客户端也照常对话。本模块是一层兜底/研究用的旁路，保留原因见下。

## 这个模块原本要解决什么

排查 406 期间，我们一度以为 managed 模型网关在 `X-Authorization` 之外还要求一层
**客户端签名**：

* 握手 `/api/paas/c1f3a7e2/v2/client` 领取一把 Ed25519 私钥（用 `id.secret` 形态的
  apiKey 认证，私钥再用 HKDF+AES-GCM 解出）；
* 每个请求带 `X-Client-Sig`（对 `apiKeyId\\nts\\nversion\\nsessionId\\nnonce` 签名）
  和 `X-Client-Pow`（8 bit 工作量证明）。

后来实测推翻了这个结论：406 不是签名问题，而是上游的三条请求校验 —— system 必
须以 `"You are AutoClaw. Answer the user directly and concisely."` 开头
（`upstream.REQUIRED_SYSTEM_PREFIX`）、正文不能有竞品身份句（`scrub_text`）、
`max_tokens` 必须落在 `[10000, 131072]`（`normalize_max_tokens`）。这三条补齐后
不带任何签名头也能正常对话。

因此本模块不再是必需路径，但它仍然有用：
* 上游万一改成真校验签名时，可以立刻切到 client 模式顶上；
* 拿客户端代发做对照实验（比对直连与客户端拿到的响应差异）。

## 怎么做

客户端在 `127.0.0.1:18432` 起了一个 token 服务（`/get_token`），但它只对「AutoClaw
家族进程」放行。因此网关不自己发请求，而是**拉起一个客户端同源的小进程**去代发：

    python -m ac2api.clawproxy        # 子进程模式：从 stdin 读请求，HTTP 结果写 stdout

父进程用 `copy /b AutoClaw2.exe + payload` 造一个临时可执行文件，进程名落在
`AutoClaw` 家族里，且我们的 `argv[0]`/映像路径也指向它 —— 客户端拿到的进程树因此
看起来就是它自己的子进程。

> 说明：这一层依赖「本机已安装并登录 AutoClaw 客户端」。没有客户端时，对话请求
> 会退回直连模式（`AC2API_CHAT_MODE=direct`），直连是可用的。
"""

from __future__ import annotations

import base64
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量：全部来自客户端 main.cjs（gE / resolveHeaders / resolveAuthorization）
# ---------------------------------------------------------------------------
DEFAULT_TOKEN_ENDPOINT = "http://127.0.0.1:18432/get_token"
CLIENT_EXE_NAMES = ("AutoClaw2.exe", "AutoClaw.exe")
#: 客户端的可执行文件候选目录（安装目录 + 用户级安装）。
DEFAULT_EXE_HINTS = (
    r"D:\autoclaw\AutoClaw2\AutoClaw2.exe",
    r"C:\Program Files\AutoClaw\AutoClaw2.exe",
    r"C:\Program Files (x86)\AutoClaw\AutoClaw2.exe",
)
PRODUCT_NAME = "autoclaw-desktop"
CLIENT_TRACE_TAG = "autoclaw-desktop"
#: 客户端自己的「伪会话」，用于连接探测（不带 X-Session-Id 时也落在这个值上）。
PROBE_SESSION_ID = "connection-probe"


class ClawProxyError(Exception):
    """客户端代发链路的失败（客户端没装 / 没登录 / 进程拉不起来 / 超时）。"""

    def __init__(self, message: str, *, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind  # unavailable | not_logged_in | timeout | transport


@dataclass(frozen=True)
class BridgeRequest:
    """父进程 → 子进程的一次代发请求。"""

    url: str
    method: str = "POST"
    headers: dict[str, str] | None = None
    body: bytes = b""
    stream: bool = False
    timeout: float = 600.0
    #: 客户端本地取 token 的地址；空 = 用子进程默认的 18432。子进程自己取，
    #: 因为它才在「客户端家族」的进程树里（服务端会反查 PID）。
    token_endpoint: str = ""

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "url": self.url,
                "method": self.method,
                "headers": self.headers or {},
                "body": base64.b64encode(self.body).decode("ascii"),
                "stream": self.stream,
                "timeout": self.timeout,
                "token_endpoint": self.token_endpoint,
            },
            ensure_ascii=False,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "BridgeRequest":
        obj = json.loads(raw.decode("utf-8"))
        return cls(
            url=obj["url"],
            method=obj.get("method") or "POST",
            headers=obj.get("headers") or {},
            body=base64.b64decode(obj.get("body") or ""),
            stream=bool(obj.get("stream")),
            timeout=float(obj.get("timeout") or 600.0),
            token_endpoint=obj.get("token_endpoint") or "",
        )


# ---------------------------------------------------------------------------
# 本机客户端发现
# ---------------------------------------------------------------------------
def find_client_exe(explicit: str = "") -> str:
    """找一个已安装的 AutoClaw 客户端可执行文件；找不到返回空串。"""
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    for hint in DEFAULT_EXE_HINTS:
        if os.path.isfile(hint):
            return hint
    roots = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("APPDATA", ""),
    ]
    for root in filter(None, roots):
        for name in CLIENT_EXE_NAMES:
            for rel in (("AutoClaw", name), ("Programs", "AutoClaw", name), ("AutoClaw2", name)):
                candidate = os.path.join(root, *rel)
                if os.path.isfile(candidate):
                    return candidate
    return ""


def token_service_alive(endpoint: str = DEFAULT_TOKEN_ENDPOINT, timeout: float = 1.5) -> bool:
    """客户端 token 服务是否在监听（只做 TCP 探活，不真的取 token）。"""
    try:
        host, _, port = endpoint.split("//", 1)[-1].partition(":")
        host = host.split("/")[0]
        with socket.create_connection((host, int(port.split("/")[0] or 18432)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 子进程（客户端同源）—— 这一半跑在「伪装成 AutoClaw」的进程里
# ---------------------------------------------------------------------------
def _client_identity_headers(app_id: str, app_key: str, product: str, version: str,
                            language: str, channel: str) -> dict[str, str]:
    """`gE()` 的指纹头。子进程必须自己拼，因为要现算时间戳签名。"""
    import hashlib

    ts = int(time.time())
    sign = hashlib.md5(f"{app_id}&{ts}&{app_key}".encode("utf-8")).hexdigest()
    headers = {
        "X-Version": version,
        "X-Tm": "win" if os.name == "nt" else "mac",
        "X-Product": product,
        "X-Auth-Appid": app_id,
        "X-Auth-TimeStamp": str(ts),
        "X-Auth-Sign": sign,
        "X-Lang": language,
    }
    if channel:
        headers["X-Channel"] = channel
    return headers


def fetch_local_token(endpoint: str, timeout: float = 10.0) -> str:
    """向客户端的本地 token 服务要 accessToken（只有同源进程能拿到）。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    # 空 ProxyHandler = 直连，忽略 http_proxy/http_proxy 等环境变量。这个端点
    # 永远是 127.0.0.1，走代理既没意义又会得到一个莫名其妙的失败。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        raise ClawProxyError(
            f"本机 token 服务拒绝（HTTP {err.code}）：只有 AutoClaw 客户端进程能取 token",
            kind="unavailable",
        ) from err
    except (urllib.error.URLError, OSError) as err:
        raise ClawProxyError(f"连不上本机 token 服务 {endpoint}：{err}", kind="unavailable") from err
    token = ""
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        for key in ("accessToken", "access_token", "token"):
            if isinstance(obj.get(key), str) and obj[key].strip():
                token = obj[key].strip()
                break
        else:
            for holder in ("data", "result"):
                inner = obj.get(holder)
                if isinstance(inner, dict):
                    for key in ("accessToken", "access_token", "token"):
                        if isinstance(inner.get(key), str) and inner[key].strip():
                            token = inner[key].strip()
                            break
    elif isinstance(obj, str):
        token = obj.strip()
    if not token:
        raise ClawProxyError("本机 token 服务没有返回 accessToken", kind="not_logged_in")
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def resolve_access_token(headers: dict[str, str], endpoint: str) -> str:
    """确定这次代发用的 token：外部已给就用外部，否则在**子进程里**向本机服务要。

    为什么必须在子进程里取：客户端那个 `127.0.0.1:18432/get_token` 会反查对端
    PID 并校验进程树，只放行 AutoClaw 家族进程。父进程（普通 python）去要会被
    403 拒掉；这个子进程的映像名是 AutoClaw2.exe，才拿得到。
    """
    for key, value in headers.items():
        if key.lower() == "x-authorization" and value.strip():
            return value.strip()
    token = fetch_local_token(endpoint or DEFAULT_TOKEN_ENDPOINT)
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def _send(request: BridgeRequest) -> int:
    """子进程里执行代发：把上游的状态码与响应体原样吐回 stdout。"""
    import http.client
    import urllib.parse

    headers = dict(request.headers or {})
    try:
        headers["X-Authorization"] = resolve_access_token(headers, request.token_endpoint)
    except ClawProxyError as err:
        # 以 status=0 + error 回给父进程，父进程据此给出「客户端没登录」这类明确提示。
        sys.stdout.write(json.dumps({"status": 0, "error": str(err), "kind": err.kind},
                                    ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    parts = urllib.parse.urlsplit(request.url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                    timeout=request.timeout)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request(request.method, path, body=request.body or None, headers=headers)
        resp = conn.getresponse()
        sys.stdout.write(json.dumps({"status": resp.status,
                                     "content_type": resp.getheader("content-type") or ""}) + "\n")
        sys.stdout.flush()
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """`python -m ac2api.clawproxy` 入口：从 stdin 读一条 BridgeRequest 并执行。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    raw = sys.stdin.buffer.read()
    if not raw:
        return 2
    try:
        request = BridgeRequest.from_json(raw)
    except (ValueError, KeyError) as err:
        sys.stderr.write(f"bad bridge request: {err}\n")
        return 2
    if "--token" in argv:
        # 只取 token：交给父进程自己发（极少用，排障时方便）。
        endpoint = argv[argv.index("--token") + 1] if len(argv) > argv.index("--token") + 1 \
            else DEFAULT_TOKEN_ENDPOINT
        sys.stdout.write(fetch_local_token(endpoint))
        return 0
    return _send(request)


# ---------------------------------------------------------------------------
# 父进程侧：把子进程伪装成客户端家族进程并调用
# ---------------------------------------------------------------------------
@dataclass
class HostResult:
    status: int
    content_type: str = ""
    chunks: list[bytes] = None  # type: ignore[assignment]

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks or [])

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class ClawClient:
    """用「同名可执行文件 + 内嵌 zipapp」的方式在客户端家族里起子进程。

    父进程只做三件事：造壳 → 起进程 → 把 stdout 的帧读回来。签名与鉴权全部由
    子进程完成，父进程看不到也不该看到私钥。
    """

    def __init__(self, *, exe: str = "", token_endpoint: str = DEFAULT_TOKEN_ENDPOINT,
                 package_root: str = "", python: str = "", keep_shell: bool = False):
        self.exe = find_client_exe(exe)
        self.token_endpoint = token_endpoint or DEFAULT_TOKEN_ENDPOINT
        self.package_root = package_root or str(Path(__file__).resolve().parent.parent)
        self.python = python or sys.executable
        self.keep_shell = keep_shell
        self._shell: str = ""
        self._tmpdir: str = ""

    # -- 壳 ---------------------------------------------------------------
    def available(self) -> bool:
        return bool(self.exe)

    def shell_path(self) -> str:
        """生成（并缓存）一个「Python 解释器副本 + 客户端进程名」的壳。

        不能直接拿 AutoClaw2.exe 当 argv[0]（它是 Electron 壳，会自己启动 UI 并
        直接退出），而是复制**当前 Python 解释器**，再给它起客户端的文件名：
        进程树里看到的是 `AutoClaw2.exe`，实际跑的是 Python。
        """
        if self._shell and os.path.isfile(self._shell):
            return self._shell
        if not self.exe:
            raise ClawProxyError(
                "没找到 AutoClaw 客户端（可用 AC2API_CLIENT_EXE 指定 AutoClaw2.exe 的路径）",
                kind="unavailable",
            )
        self._tmpdir = tempfile.mkdtemp(prefix="ac2api-")
        target = os.path.join(self._tmpdir, os.path.basename(self.exe))
        shutil.copyfile(sys.executable, target)
        self._shell = target
        return target

    def _payload(self) -> str:
        """把 ac2api 打成 zipapp（.pyz），子进程用 `python <pyz>` 起。"""
        import compileall
        import zipapp

        out = os.path.join(self._tmpdir, "ac2api-run.pyz")
        if os.path.isfile(out):
            return out
        root = self.package_root
        stage = os.path.join(self._tmpdir, "stage")
        pkg = os.path.join(stage, "ac2api")
        shutil.copytree(os.path.join(root, "ac2api"), pkg,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with open(os.path.join(stage, "__main__.py"), "w", encoding="utf-8") as fh:
            fh.write("from ac2api.clawproxy import main\nimport sys\nsys.exit(main())\n")
        # 预编译成 .pyc 再打包：子进程每次起都要快，不能现场编译几十个模块。
        for cache in Path(stage).rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        compileall.compile_dir(stage, quiet=2, optimize=0)
        zipapp.create_archive(stage, out, compressed=True)
        return out

    # -- 调一次 -----------------------------------------------------------
    def call(self, request: BridgeRequest, *, env_extra: dict[str, str] | None = None) -> HostResult:
        shell = self.shell_path()
        payload = self._payload()
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        if env_extra:
            env.update(env_extra)
        # 关键：argv[0] 指向那个「客户端可执行文件名」，Python 仍然解释 .pyz，
        # 而进程树 / netstat 反查出来的映像名落在 AutoClaw 家族里。
        proc = subprocess.Popen(
            [shell, str(payload)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=self._tmpdir or None,
        )
        try:
            assert proc.stdout is not None and proc.stdin is not None
            proc.stdin.write(request.to_json())
            proc.stdin.close()
            head = proc.stdout.readline()
            if not head:
                raise ClawProxyError("客户端代发子进程没有输出（可能被客户端拒绝或崩溃）",
                                     kind="transport")
            try:
                meta = json.loads(head.decode("utf-8", "replace"))
            except ValueError as err:
                raise ClawProxyError(f"代发子进程返回了非法头部：{head[:120]!r}",
                                     kind="transport") from err
            if meta.get("error"):
                # 子进程在发请求前就失败了（多半是客户端 token 服务不理它）。
                raise ClawProxyError(str(meta["error"]),
                                     kind=str(meta.get("kind") or "transport"))
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = proc.stdout.read(16384)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                proc.wait(timeout=5)
            return HostResult(status=int(meta.get("status") or 0),
                              content_type=meta.get("content_type") or "",
                              chunks=chunks)
        finally:
            if proc.poll() is None:
                proc.kill()

    def cleanup(self) -> None:
        if self._tmpdir and not self.keep_shell:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = ""


# ---------------------------------------------------------------------------
# 给网关用的高层封装
# ---------------------------------------------------------------------------
@dataclass
class ProxyContext:
    """一次转发需要的东西：上游地址 + 客户端身份头 + 是否流式。"""

    url: str
    headers: dict[str, str]
    body: bytes
    stream: bool
    timeout: float = 600.0


def build_client_headers(*, access_token: str, app_id: str, app_key: str, product: str,
                         version: str, language: str, channel: str, model: str,
                         session_id: str = "", request_id: str = "") -> dict[str, str]:
    """按客户端的 `resolveHeaders()` 拼一次对话请求的头。"""
    rid = request_id or str(uuid.uuid4())
    headers = _client_identity_headers(app_id, app_key, product, version, language, channel)
    headers.update({
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        # 没有 token 时留空占位，由子进程的 resolve_access_token() 现取再补上。
        "X-Authorization": access_token if access_token.lower().startswith("bearer ")
        else (f"Bearer {access_token}" if access_token else ""),
        # 客户端会先删掉 authorization / x-authorization 再补自己的一套，这里只留一个。
        "X-Trace-Id": rid,
        "X-Request-Id": rid,
        "X-Request-Model": model,
        "X-Client-Type": "pc",
        "x_trace_id": CLIENT_TRACE_TAG,
        "X-Session-Id": session_id or PROBE_SESSION_ID,
    })
    if not getattr(build_client_headers, "_jitter_done", False):
        # 客户端同一账号的最小取号间隔是 0.1s；这里给请求前加一点抖动，
        # 避免多账号池在同一毫秒并发压到网关的「同号防撞」上。
        time.sleep(random.uniform(0.0, 0.05))
        build_client_headers._jitter_done = True  # type: ignore[attr-defined]
    return headers


__all__ = [
    "BridgeRequest",
    "ClawClient",
    "ClawProxyError",
    "HostResult",
    "ProxyContext",
    "build_client_headers",
    "fetch_local_token",
    "find_client_exe",
    "main",
    "token_service_alive",
]
