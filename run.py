"""一键启动：`python run.py`（等价于 `python -m ac2api`）。

用这个文件而不是 `python -m ac2api`，是因为很多人会在错误的目录下执行
`-m`，然后得到 `No module named ac2api`。这里显式把项目根目录塞进
sys.path，所以在任何目录下 `python C:\\path\\to\\run.py` 都能跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让 `import ac2api` 一定能找到同目录下的包
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ac2api.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
