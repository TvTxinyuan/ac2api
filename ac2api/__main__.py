"""一键启动：`python -m ac2api` 或 `python run.py`。

启动前不再单独预热 —— 预热放在 create_app 的 startup 事件里（见 main.py），
这样无论是 `python -m ac2api` 还是 `uvicorn ac2api.main:app`，行为完全一致，
不会出现「一种方式启动看不到积分」的割裂。
"""
from __future__ import annotations

import os
import sys

import uvicorn

from .main import create_app
from .upstream import Settings


def build_app():
    settings = Settings.from_env()
    app = create_app(settings=settings)
    # 单机场景默认就一个「本机 AutoClaw」账号；多账号可在管理台添加。
    gw = app.state.gateway
    if len(gw.pool.all()) == 1 and gw.pool.all()[0].key == "local":
        acc = gw.pool.all()[0]
        acc.nickname = "本机 AutoClaw"
    return app


def main() -> None:
    # `python -m ac2api probe` —— 用户很容易把 `probe.py` 和 `-m ac2api` 混着念，
    # 所以把探针也挂在这里，少一次「文件找不到」的来回。
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        from .probe import main as probe_main
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        raise SystemExit(probe_main())

    app = build_app()
    try:
        uvicorn.run(
            app,
            host=os.environ.get("AC2API_HOST", "127.0.0.1"),
            port=int(os.environ.get("AC2API_PORT", "8000")),
            log_level=os.environ.get("AC2API_LOG_LEVEL", "info"),
            # 长 SSE 不能被 uvicorn 的 keep-alive 掐断。
            timeout_keep_alive=int(os.environ.get("AC2API_KEEPALIVE", "75")),
        )
    finally:
        # Ctrl+C 退出前把用量（和节流中的 key 配额）落一次盘，别丢最后十几秒。
        gw = getattr(app.state, "gateway", None)
        if gw is not None and getattr(gw, "store", None) is not None:
            gw.flush_usage()


if __name__ == "__main__":
    main()
