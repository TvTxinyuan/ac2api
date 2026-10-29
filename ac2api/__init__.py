"""ac2api —— 把 AutoClaw 的积分额度包装成 OpenAI 兼容 API。

模块划分（读代码的顺序建议照这个来）：
    credits.py    积分口径：到期解析 / 账号汇总 / 紧迫度排序（纯函数，可单测）
    pool.py       账号池：租约 / 粘性 / 加权选号 / 退避（纯内存，线程安全）
    upstream.py   上游适配：签名、token 获取、chat 转发、积分查询
    keys.py       本地 API Key：哈希存储、配额、速率、用量簿记
    main.py       FastAPI 网关：路由 + 生命周期 + 记账
    admin_page.py 管理后台单页
    probe.py      上游协议探针（独立脚本，不进包）

设计取舍集中写在 `docs/design.md`，部署写在根目录 README。
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
