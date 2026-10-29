"""桌面程序入口。PyInstaller 打的就是这个文件。

单独放一个薄入口而不是直接打 ``ac2api/desktop.py``：打包产物是单文件，
入口脚本需要能自己把项目根目录加进 ``sys.path``（打包后工作目录不是源码
目录），逻辑留在包里、这里只做引导。
"""
from __future__ import annotations

import os
import sys

# 源码模式运行时（还没打包），把项目根目录放进搜索路径。
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> int:
    from ac2api.desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
