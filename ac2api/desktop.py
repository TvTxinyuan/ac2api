"""桌面程序：系统托盘常驻 + 后台跑网关 + 点托盘打开管理窗口。

为什么是「托盘 + 内嵌浏览器视图」而不是重写一套 GUI：

管理页（``admin_page.py``）本来就是现成的、还在持续改进的界面。桌面版换个壳
去重画一遍，等于把同一份 UI 逻辑维护两遍，而且以后加功能要改两处。所以这里
只做三件事——**把网关跑在后台线程**、**给一个托盘图标**、**把管理页用系统
浏览器打开**。界面只有一份，Web 和桌面永远一致。

关于「为什么不用 pywebview/Electron 开个原生窗口」：pywebview 在 Windows 上
依赖 Edge WebView2，没装的环境会白屏；Electron 要带一整个 Chromium，几十上百
MB。系统浏览器是每台 Windows 都有的东西，对一个「本机自用网关」足够，且不会
因为 WebView2 版本差异出玄学问题。

单实例：用命名互斥体（mutex）保证只跑一个托盘。已经开着时第二次双击不该再起
一个网关抢 8000 端口，而是把已有的管理页调出来。
"""
from __future__ import annotations

import ctypes
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from typing import Optional

MUTEX_NAME = "Local\\ac2api-desktop-singleton"
WINDOW_TITLE_HINT = "ac2api"


# ---------------------------------------------------------------------------
# 日志：打包成 --noconsole 后没有控制台，print 会直接丢掉。
# 出问题时光看「程序一闪而过」排查不了，所以所有输出同时写一份文件。
# ---------------------------------------------------------------------------
class Tee:
    """把 stdout 同时写进日志文件。日志按大小滚动，避免无限增长。

    ``path`` 写不进去时（安全软件拦截、目录只读）会自动换一个能写的文件，
    这样「程序闪一下就没」时用户仍然有东西可查，而不是一个空目录。
    """

    def __init__(self, path: str, max_bytes: int = 2 * 1024 * 1024) -> None:
        self.path = _pick_writable_log(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._roll()

    def _roll(self) -> None:
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                backup = self.path + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(self.path, backup)
        except OSError:
            pass

    def write(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass
        # 有控制台时照样打出来，方便开发时直接看。
        # 注意：打包成 --noconsole 后 sys.__stdout__ 是 None，这一路会静默失败，
        # 但上面的文件写入必须照样执行 —— 否则桌面无窗口版就没有任何日志可查。
        try:
            original = sys.__stdout__
            if original is not None:
                original.write(text)
                original.flush()
        except (OSError, ValueError, AttributeError):
            pass

    def flush(self) -> None:
        try:
            if sys.__stdout__ is not None:
                sys.__stdout__.flush()
        except (OSError, ValueError):
            pass

    # uvicorn / argparse 之类的库会拿 sys.stdout 当真正的文件对象用，
    # 检查 isatty（决定要不要输出彩色 ANSI）和 encoding。
    # 不实现这些，uvicorn 的 dictConfig 会直接抛
    # ValueError: Unable to configure formatter 'default'。
    def isatty(self) -> bool:
        try:
            return bool(sys.__stdout__ is not None and sys.__stdout__.isatty())
        except (OSError, ValueError):
            return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def fileno(self) -> int:
        # 没有真实 fd：打包成 --noconsole 后标准句柄可能是无效的。
        raise OSError("Tee has no fileno")


def data_dir() -> str:
    """桌面版的数据目录：与 CLI 共用同一个，用户不用记两套路径。"""
    from .store import default_data_dir
    return str(default_data_dir())


def log_path() -> str:
    return os.path.join(data_dir(), "desktop.log")


def _pick_writable_log(preferred: str) -> str:
    """挑一个真能写进去的日志文件。

    优先用户要的位置；写不进就退到程序旁边，最后退到系统临时目录。宁可
    日志跑到别处，也好过用户完全看不到任何记录。
    """
    candidates = [preferred]

    try:
        import sys
        base = os.path.dirname(
            os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
        )
        candidates.append(os.path.join(base, "ac2api-data", "desktop.log"))
    except Exception:  # noqa: BLE001
        pass

    import tempfile as _tempfile
    candidates.append(os.path.join(_tempfile.gettempdir(), "ac2api-desktop.log"))

    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                pass
            return path
        except OSError:
            continue
    return preferred  # 全都不行就只能返回原位置，写入时再静默失败


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------
_mutex_handle = None


def acquire_single_instance() -> bool:
    """抢单实例锁。返回 True 表示我们是第一个；False 表示已有实例在跑。

    用 Win32 命名互斥体而不是端口探测：端口探测在「上一个实例正在退出」时会
    误判，而互斥体在进程结束（哪怕崩溃）时由内核自动释放。
    """
    global _mutex_handle
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        if not already:
            _mutex_handle = handle
        return not already
    except Exception:  # noqa: BLE001 - 抢不到锁不该阻止程序启动
        return True


def release_single_instance() -> None:
    global _mutex_handle
    if _mutex_handle is None or os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex(_mutex_handle)
        kernel32.CloseHandle(_mutex_handle)
    except Exception:  # noqa: BLE001
        pass
    _mutex_handle = None


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------
def icon_path() -> Optional[str]:
    """图标文件位置。打包后 PyInstaller 会把 asset 解到 _MEIPASS。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "assets", "ac2api.ico"))
        candidates.append(os.path.join(base, "ac2api.ico"))
    candidates.append(os.path.join(here, "ac2api.ico"))
    candidates.append(os.path.join(os.path.dirname(here), "assets", "ac2api.ico"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def make_icon_image(status: str = "ok"):
    """有 ico 就用 ico；没有就现画一个，保证托盘一定有图标。

    status 只影响颜色：ok=青绿、warn=橙、error=红。托盘图标是用户唯一能一眼
    看到状态的入口，所以状态变化时顺手换色，比弹通知克制。
    """
    from PIL import Image, ImageDraw

    colors = {
        "ok": (46, 204, 160),
        "warn": (240, 170, 60),
        "error": (225, 80, 80),
        "busy": (120, 150, 220),
    }
    color = colors.get(status, colors["ok"])

    path = icon_path()
    if path:
        try:
            img = Image.open(path)
            img.load()
            return img.convert("RGBA")
        except Exception:  # noqa: BLE001
            pass

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 3, size - 3), fill=color)
    # 中间画一个「>」提示这是个命令行/接口类工具
    draw.line((22, 20, 40, 32), fill=(255, 255, 255, 235), width=6)
    draw.line((40, 32, 22, 44), fill=(255, 255, 255, 235), width=6)
    return img


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------
def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def pick_port(host: str, preferred: int) -> int:
    """优先用配置的端口；被占了就往后找。

    桌面版不该因为「8000 被别的程序占用」就起不来——用户看到的应该是程序
    起来了、地址换了一个，而不是一个闪退。端口会被写进管理页地址，所以
    实时告诉用户最终是哪个。
    """
    if not port_in_use(host, preferred):
        return preferred
    for port in range(preferred + 1, preferred + 40):
        if not port_in_use(host, port):
            return port
    return preferred


# ---------------------------------------------------------------------------
# 网关后台线程
# ---------------------------------------------------------------------------
class GatewayThread(threading.Thread):
    """在后台线程里跑 uvicorn。

    uvicorn.Server 自己有 signal handler，而 signal handler 只能在主线程装。
    所以这里显式关掉它（install_signal_handlers=False），退出由主线程通过
    ``server.should_exit`` 统一下达。
    """

    def __init__(self, app, host: str, port: int) -> None:
        super().__init__(name="ac2api-gateway", daemon=True)
        self.app = app
        self.host = host
        self.port = port
        self.server = None
        self._ready = threading.Event()
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        import uvicorn

        # 不传 install_signal_handlers：那不是 Config 的参数。uvicorn 的
        # capture_signals() 自己会判断「非主线程就跳过装 handler」，所以在
        # 后台线程里 run() 本来就是安全的，退出统一由主线程置 should_exit。
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level=os.environ.get("AC2API_LOG_LEVEL", "warning"),
            timeout_keep_alive=int(os.environ.get("AC2API_KEEPALIVE", "75")),
        )
        self.server = uvicorn.Server(config)
        try:
            self.server.run()
        except BaseException as err:  # noqa: BLE001
            self.error = err
            self._ready.set()
            raise
        finally:
            self._ready.set()

    def wait_ready(self, timeout: float = 20.0) -> bool:
        """等端口真的能连上，而不是等线程启动。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error is not None:
                return False
            if port_in_use(self.host, self.port):
                return True
            time.sleep(0.1)
        return False

    def stop(self, timeout: float = 8.0) -> None:
        if self.server is not None:
            self.server.should_exit = True
        self.join(timeout)


def build_app_and_start(host: str, port: int) -> tuple[GatewayThread, str, object]:
    """建 app、起线程、返回 (线程, base_url)。失败会抛异常给调用方。"""
    from .__main__ import build_app

    app = build_app()
    thread = GatewayThread(app, host, port)
    thread.start()
    if not thread.wait_ready():
        detail = f"{type(thread.error).__name__}: {thread.error}" if thread.error else "启动超时"
        raise RuntimeError(f"网关启动失败（{detail}）")
    return thread, f"http://{host}:{port}", app


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _notify(icon, message: str, title: str = "ac2api") -> None:
    try:
        icon.notify(message, title)
    except Exception:  # noqa: BLE001 - 某些 Windows 版本通知会失败，不影响主功能
        pass


def _log(text: str) -> bool:
    """兜底日志：直接追加写文件，不经过 sys.stdout。

    ``run_desktop`` 启动早期如果 ``Tee`` 因为任何原因没装上，走这里仍然能
    留下记录。会复用 ``Tee`` 的回退逻辑，所以即便主目录不可写也有地方落。
    成功返回 True。
    """
    try:
        path = _pick_writable_log(log_path())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


def run_desktop() -> int:
    """托盘模式主循环。返回进程退出码。"""
    # 先把日志接管好。打包成 --noconsole 后没有任何可见输出，这里是唯一的
    # 排查入口，所以它本身绝不能成为失败点：Tee 建不起来就直接往文件追加，
    # 不能让一个日志问题连带把整个程序憋死。
    try:
        tee = Tee(log_path())
        sys.stdout = tee
        sys.stderr = tee
    except Exception:  # noqa: BLE001
        tee = None
    _log(f"\n[ac2api] 桌面版启动 {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if not acquire_single_instance():
        print("[ac2api] 已有实例在运行，打开它的管理页后退出")
        host = os.environ.get("AC2API_HOST", "127.0.0.1")
        port = int(os.environ.get("AC2API_PORT", "8000"))
        # 已有实例可能因为端口占用换了端口，逐个试一下能连上的那个。
        for candidate in range(port, port + 40):
            if port_in_use(host, candidate):
                webbrowser.open(f"http://{host}:{candidate}/admin")
                return 0
        _alert("ac2api 已在运行，但找不到它的管理端口。请检查托盘图标。")
        return 0

    try:
        import pystray
    except ImportError:
        _alert("缺少 pystray 依赖。\n\n请运行：pip install pystray pillow")
        return 1

    host = os.environ.get("AC2API_HOST", "127.0.0.1")
    preferred = int(os.environ.get("AC2API_PORT", "8000"))
    port = pick_port(host, preferred)
    if port != preferred:
        print(f"[ac2api] 端口 {preferred} 被占用，改用 {port}")

    try:
        gateway, base_url, app = build_app_and_start(host, port)
    except Exception as err:  # noqa: BLE001
        print("[ac2api] 启动失败：\n" + traceback.format_exc())
        _alert(f"网关启动失败：\n\n{err}\n\n日志：{log_path()}")
        release_single_instance()
        return 1

    admin_url = f"{base_url}/admin"
    print(f"[ac2api] 网关已就绪 {base_url}")

    state = {"opened": False}

    def open_admin(icon=None, item=None) -> None:
        webbrowser.open(admin_url)

    def copy_base_url(icon=None, item=None) -> None:
        _copy_to_clipboard(base_url)
        if icon is not None:
            _notify(icon, f"已复制：{base_url}")

    def copy_admin_url(icon=None, item=None) -> None:
        _copy_to_clipboard(admin_url)
        if icon is not None:
            _notify(icon, f"已复制：{admin_url}")

    def open_log(icon=None, item=None) -> None:
        try:
            os.startfile(log_path())  # noqa: S606 - 刻意用系统默认程序打开日志
        except OSError:
            _alert(f"日志文件：{log_path()}")

    def show_info(icon, item=None) -> None:
        _alert(
            "ac2api 正在运行\n\n"
            f"接口地址：{base_url}\n"
            f"管理界面：{admin_url}\n\n"
            "在支持 OpenAI 协议的客户端里填上面的「接口地址」，\n"
            "模型名可填 glm-5.3-flash。API key 在管理界面里生成。\n\n"
            f"数据目录：{data_dir()}"
        )

    def quit_app(icon, item=None) -> None:
        print("[ac2api] 用户从托盘退出")
        icon.visible = False
        try:
            gateway.stop()
        finally:
            _flush_usage(app)
            icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开管理界面", open_admin, default=True),
        pystray.MenuItem("复制接口地址", copy_base_url),
        pystray.MenuItem("复制管理地址", copy_admin_url),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("状态与用法", show_info),
        pystray.MenuItem("查看日志", open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon("ac2api", make_icon_image("ok"), "ac2api — 网关运行中", menu)

    # 首次启动直接把管理页弹出来：用户双击程序后总得看到点反应，
    # 只出现一个托盘图标容易被忽略。之后从托盘菜单控制。
    def on_setup(ic) -> None:
        ic.visible = True
        if not state["opened"]:
            state["opened"] = True
            open_admin()
            _notify(ic, f"网关已启动：{base_url}")

    icon.run(setup=on_setup)

    # icon.run 返回说明托盘被关掉了，这里兜底收尾。
    gateway.stop()
    _flush_usage(app)
    release_single_instance()
    print("[ac2api] 已退出")
    return 0


def _flush_usage(app) -> None:
    """退出前把用量落盘，别丢节流中的最后一小段配额。"""
    try:
        gw = getattr(app.state, "gateway", None)
        if gw is not None and getattr(gw, "store", None) is not None:
            gw.flush_usage()
    except Exception:  # noqa: BLE001
        pass


def _copy_to_clipboard(text: str) -> None:
    """复制到剪贴板。装得上 pyperclip 就用它，否则退回 Win32 API。"""
    try:
        import pyperclip  # type: ignore[import-not-found]

        pyperclip.copy(text)
        return
    except Exception:  # noqa: BLE001
        pass
    if os.name != "nt":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE|GMEM_ZEROINIT
            if not handle:
                return
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(13, handle)  # CF_UNICODETEXT
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001
        pass


def _alert(message: str, title: str = "ac2api") -> None:
    """没有控制台时的错误提示。用 Win32 消息框，不额外依赖 tkinter。"""
    if os.name == "nt":
        try:
            ctypes.WinDLL("user32", use_last_error=True).MessageBoxW(
                None, message, title, 0x00000040 | 0x00000000  # MB_ICONINFORMATION
            )
            return
        except Exception:  # noqa: BLE001
            pass
    print(message)


def main() -> int:
    return run_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
