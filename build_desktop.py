"""把 ac2api 打包成桌面程序（onedir 文件夹版，无控制台窗口）。

    python build_desktop.py

产物：dist/ac2api/ac2api.exe（连同同目录的依赖文件一起分发）

几个刻意的选择：

* ``--onedir`` 而不是 ``--onefile``：单文件版启动时要把自身解压到临时目录，
  这个「解压一个未签名 exe」的行为会被国产安全软件（腾讯电脑管家、360 等）
  拦截，报 ``Could not create temporary directory``，且加白名单之前完全无从
  排查。文件夹版直接加载磁盘上的文件，不触发这类启发式规则。实测：
  onefile 在这台机器上起不来，onedir 正常。
* ``--noconsole``：托盘程序不该带黑框。副作用是 print 输出全丢，所以
  ``desktop.py`` 里把 stdout 重定向进了 ``~/.ac2api/desktop.log``。
* ``--collect-all pystray``：pystray 的 Windows 后端（``_win32``）是运行时按
  平台动态导入的，PyInstaller 的静态分析看不到，不显式收集就会出现
  「打包成功但一运行就 ImportError」。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(ROOT, "desktop_entry.py")
ICON = os.path.join(ROOT, "assets", "ac2api.ico")
NAME = "ac2api"


def main() -> int:
    if not os.path.exists(ICON):
        print("图标不存在，先生成：python make_icon.py")
        return 1

    # 每次从干净的 build 开始，避免残留的旧中间产物混进包里。
    for d in ("build", "dist"):
        path = os.path.join(ROOT, d)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noconsole",
        "--name", NAME,
        "--icon", ICON,
        "--add-data", f"{ICON}{os.pathsep}assets",
        "--collect-all", "pystray",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        # 明文把项目根目录塞进搜索路径：否则在别的目录下调用
        # （或从编辑器里点运行）会找不到 ac2api 包。
        "--paths", ROOT,
        ENTRY,
    ]
    print("执行：\n  " + " \\\n  ".join(args) + "\n")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        print(f"\n打包失败，返回码 {result.returncode}")
        return result.returncode

    dist_dir = os.path.join(ROOT, "dist", NAME)
    exe = os.path.join(dist_dir, f"{NAME}.exe")
    if not os.path.exists(exe):
        print(f"\n打包似乎成功但找不到 {exe}")
        return 1

    total = sum(
        os.path.getsize(os.path.join(base, f))
        for base, _dirs, files in os.walk(dist_dir)
        for f in files
    )
    print(f"\n完成：{exe}")
    print(f"整个文件夹 {total / 1024 / 1024:.1f} MB")

    # 顺手打个 zip：分发给别人时不会漏文件，也不会因为传输方式丢失依赖。
    zip_path = os.path.join(ROOT, "dist", f"{NAME}-windows-x64.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(dist_dir):
            for f in files:
                full = os.path.join(base, f)
                zf.write(full, os.path.relpath(full, os.path.join(ROOT, "dist")))
    print(f"压缩包：{zip_path}（{os.path.getsize(zip_path) / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
