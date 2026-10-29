"""API Key 的磁盘持久化。

## 为什么需要

网关最初把 key 只放在内存里（照着 wb2api 的做法），结果是**进程一重启，
用户填在 Codex / CC Switch 里的 key 就全部失效**，表现为 401
``invalid_api_key``。这不是用户配置错了，而是网关自己把钥匙弄丢了。

## 存什么、不存什么

存的是 ``sha256(pepper + key)``，不是明文 —— 校验只需要哈希。文件被拷走也
换不回可用的 key。同时还存配额、用量计数、状态，这样重启后限额和账单接着算，
不会因为重启就把额度"洗白"。

## 文件长什么样

JSON，原子写（先写 ``.tmp`` 再 ``os.replace``），避免断电写坏。默认位置
``~/.ac2api/keys.json``；可用 ``AC2API_DATA_DIR`` / ``AC2API_KEYS_FILE`` 改。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

#: 文件格式版本，将来改结构时用来判断是否需要迁移。
FORMAT_VERSION = 1


def _probe_writable(directory: Path) -> bool:
    """真正落一个文件试试，而不是看 ``os.access``。

    用户目录在 Windows 上几乎总是被 ACL 判定为可写，实际写入却可能被安全
    软件或沙箱策略拦下来。只有写一下才知道。探测用的名字和真实数据文件
    一样以 ``.json`` 结尾 —— 有些拦截规则按扩展名区分。
    """
    probe = directory / ".ac2api-write-probe.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        os.replace(probe, directory / ".ac2api-write-probe.bak.json")
        (directory / ".ac2api-write-probe.bak.json").unlink()
        return True
    except Exception:  # noqa: BLE001 - 任何失败都当作不可写
        try:
            probe.unlink()
        except Exception:  # noqa: BLE001
            pass
        return False


def default_data_dir() -> Path:
    """数据目录：用户显式指定的 > 用户主目录 > 程序旁边 > 临时目录。

    之所以要回退：Windows 上沙箱策略或安全软件会挡住写用户主目录，这时
    ``~/.ac2api`` 建不起来，key 就没法持久化。退到程序所在目录至少能保住
    功能；管理台会显示实际用的路径，用户不会一头雾水。
    """
    override = (os.environ.get("AC2API_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser()

    primary = Path.home() / ".ac2api"
    if _probe_writable(primary):
        return primary

    # 回退 1：程序（exe 或包）所在目录下的 ac2api-data/
    try:
        import sys
        base = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
        fallback = base / "ac2api-data"
        if _probe_writable(fallback):
            return fallback
    except Exception:  # noqa: BLE001
        pass

    # 回退 2：系统临时目录（至少比彻底失败好）
    return Path(tempfile.gettempdir()) / "ac2api"


def default_keys_file() -> Path:
    override = (os.environ.get("AC2API_KEYS_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return default_data_dir() / "keys.json"


class KeyStore:
    """key 集合的 JSON 落地。构造时自动加载，每次变更后 ``save()``。

    并发安全靠一把锁 + 原子替换；网关本身是单进程 asyncio，够用。
    """

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path else default_keys_file()
        self._lock = threading.Lock()
        #: 最近一次读/写失败的原因，管理台会展示出来（不吞错误）。
        self.last_error = ""

    # -- 读写 -------------------------------------------------------------
    def load(self) -> list[dict[str, Any]]:
        """读出全部 key 记录；文件不存在或损坏时返回空表（并把原因记下来）。"""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            self.last_error = f"读取 {self.path} 失败：{type(err).__name__}: {err}"
            return []
        if not isinstance(raw, dict):
            self.last_error = f"{self.path} 格式不对（顶层应是对象）"
            return []
        items = raw.get("keys")
        if not isinstance(items, list):
            self.last_error = f"{self.path} 里没有 keys 列表"
            return []
        return [it for it in items if isinstance(it, dict) and it.get("key_hash")]

    def save(self, records: list[dict[str, Any]]) -> bool:
        """原子写入；失败返回 False 并记录原因（不抛，免得影响正在服务的请求）。"""
        payload = {"version": FORMAT_VERSION, "keys": records}
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(self.path.parent), prefix=".keys-", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, ensure_ascii=False, indent=2)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp_name, self.path)
                except BaseException:
                    # 写一半就失败：把临时文件清掉，别在目录里留垃圾。
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
        except OSError as err:
            self.last_error = f"写入 {self.path} 失败：{type(err).__name__}: {err}"
            return False
        self.last_error = ""
        return True
