#!/usr/bin/env python3
"""AutoClaw 上游探针（只读，不改任何东西）。

作用
----
在把流量交给网关之前，先用本机**已经登录的** AutoClaw 账号做一次体检：

* token 能不能取到、有没有过期；
* 钱包/积分余额与**到期日**（调度就靠这个「先烧快过期的」）；
* 上游下发的模型目录；
* 对话能不能通，以及走的是「直连」还是「客户端代发」。

运行
----
    python -m ac2api.probe                 # 全部检查
    python -m ac2api.probe --only credits  # 只看积分/到期
    python -m ac2api.probe --token <tok>   # 手工指定 token（不读本机凭据）

前提
----
积分与模型目录用本机凭据里的 token 直连即可；**对话**需要 AutoClaw 客户端
正在运行（见 README「对话必须借客户端之手」）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

from .credits import extract_resources
from .token import TokenSource, is_expired
from .upstream import (
    DEFAULT_APP_ID,
    DEFAULT_APP_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_PATH,
    DEFAULT_CREDITS_PATH,
    DEFAULT_TOKEN_URL,
    Settings,
    UpstreamClient,
    UpstreamError,
)

#: 全部沿用 upstream.py 的默认值 —— 探针和网关必须打同一个地址，
#: 否则「探针通了、网关不通」这种最难查的问题就出现了。
BASE = DEFAULT_BASE_URL
TOKEN_URL = DEFAULT_TOKEN_URL
APP_ID = DEFAULT_APP_ID
APP_KEY = DEFAULT_APP_KEY

#: 对话端点候选（按可能性排序）。AutoClaw 的技能脚本用的都是
#: `/agentdr/v1/assistant/...`，所以把 assistant 家族放在前面；
#: 网关当前的默认值也排在第一个，命中即「不用改配置」。
CHAT_PATHS = [
    DEFAULT_CHAT_PATH,
    "/agentdr/v1/assistant/chat/completions",
    "/agentdr/v1/assistant/completions",
    "/agentdr/v1/assistant/chat",
    "/agentdr/v1/assistant/conversation",
    "/agentdr/v1/assistant/model/chat/completions",
    "/api/paas/v4/chat/completions",
    "/v4/chat/completions",
    "/v1/chat/completions",
]

#: 积分/额度相关端点候选。第一个是网关默认值。
CREDIT_PATHS = [
    DEFAULT_CREDITS_PATH,
    "/agentpay/v1/assistant/member-list",
    "/agentpay/v1/assistant/product-info",
    "/agentpay/v1/assistant/alipay-product-info",
    "/agentpay/v1/assistant/subscribe-info",
    "/agentpay/v1/assistant/balance",
    "/agentpay/v1/assistant/user-score",
    "/agentpay/v1/assistant/credit",
    "/agentpay/v1/assistant/score-list",
]

MODEL_CANDIDATES = [
    "glm-5.3-flash",
    "glm-5",
    "glm-4.7",
    "glm-4.6",
    "autoglm",
    "glm-4-plus",
]


def build_auth_headers(app_id: str = APP_ID, app_key: str = APP_KEY, ts: int | None = None) -> dict:
    ts_s = str(ts if ts is not None else int(time.time()))
    return {
        "X-Auth-Appid": app_id,
        "X-Auth-TimeStamp": ts_s,
        "X-Auth-Sign": hashlib.md5(f"{app_id}&{ts_s}&{app_key}".encode()).hexdigest(),
    }


def fetch_local_token(token_url: str = TOKEN_URL) -> str:
    req = urllib.request.Request(token_url, headers={"Accept": "application/json, text/plain"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8", "replace").strip()
    return parse_token(raw)


def parse_token(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("Bearer "):
        return raw[7:].strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        for key in ("token", "access_token", "bearer_token"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.removeprefix("Bearer ").strip()
        data = obj.get("data")
        if isinstance(data, dict):
            for key in ("token", "access_token", "bearer_token"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val.removeprefix("Bearer ").strip()
    return ""


def call(url: str, token: str, body: dict | None, *, method: str = "POST", timeout: int = 40) -> tuple[int, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        **build_auth_headers(),
    }
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")
    except Exception as err:  # noqa: BLE001 - 探针里所有异常都只是「这条不通」
        return 0, f"{type(err).__name__}: {err}"


async def probe_credits(token: str, client: UpstreamClient) -> None:
    print("\n=== 积分 / 钱包 / 到期 ===")
    try:
        summary = await client.credits(token)
    except Exception as err:  # noqa: BLE001
        print(f"  查询失败：{type(err).__name__}: {err}")
        return
    if not summary.get("package_count"):
        print("  没拿到任何钱包 —— 上游字段可能又变了，或 token 域不对。")
        return
    print(f"  总额 {summary['total_remaining']:.2f} / {summary['total_capacity']:.2f}"
          f"（{summary['package_count']} 个钱包，最近到期 {summary['soonest_days_left']} 天）")
    print(f"  7 天内到期额度：{summary['expiring_soon_remaining']:.2f}")
    for res in summary.get("resources") or []:
        print(f"    - {res.get('display_name')}  剩余 {res.get('remaining')}"
              f"  容量 {res.get('total')}  {res.get('days_left_label') or '长期有效'}")


async def probe_models(token: str, client: UpstreamClient) -> list[str]:
    print("\n=== 模型目录 ===")
    try:
        models = await client.models(token, refresh=True)
    except Exception as err:  # noqa: BLE001
        print(f"  拉取失败：{type(err).__name__}: {err}")
        return []
    if not models:
        print("  上游没给模型列表。")
        return []
    for name in models:
        print(f"    {name}")
    print(f"\n>>> 把这一行填到 .env（可选）：\n    AC2API_MODELS={','.join(models)}")
    return models


def _safe_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


async def probe_chat(token: str, client: UpstreamClient, models: list[str] | None = None) -> None:
    print("\n=== 对话 ===")
    candidates = list(models or []) or list(MODEL_CANDIDATES)
    for model in candidates[:3]:
        payload = {"model": model,
                   "messages": [{"role": "user", "content": "ping"}],
                   "stream": False, "max_tokens": 8}
        try:
            result = await client.chat(token, payload)
        except UpstreamError as err:
            print(f"  {model}: 失败 —— {err}")
            continue
        try:
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = str(result)[:160]
        print(f"  {model}: OK —— {str(text)[:120]!r}")
        return
    print("  对话没有成功。")
    from .clawproxy import token_service_alive

    if client.chat_mode() == "client" and not token_service_alive(
            client.settings.client_token_endpoint):
        print("  原因：本机 token 服务（127.0.0.1:18432）没在监听 → AutoClaw 客户端没运行。")
        print("  签名私钥只由客户端在运行时握手持有，所以**对话必须让客户端跑着**；")
        print("  积分 / 模型目录不受影响（它们直连即可）。")
    else:
        print("  客户端在跑但上游仍拒绝，通常是客户端版本升级改了签名算法：")
        print("  升级 ac2api，或先把客户端更新到与本项目实测一致的版本。")


async def run_probe(token: str, client: UpstreamClient, only: str) -> None:
    """全部检查放在**同一个事件循环**里跑。

    不能让每个探测各起一个 `asyncio.run()`：UpstreamClient 的 httpx 连接池
    绑定在第一个循环上，循环关掉后再用就是「Event loop is closed」。
    """
    models: list[str] = []
    try:
        if only in ("credits", "all"):
            await probe_credits(token, client)
        if only in ("models", "all"):
            models = await probe_models(token, client)
        if only in ("chat", "all"):
            await probe_chat(token, client, models)
    finally:
        await client.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(description="AutoClaw 上游协议探针")
    ap.add_argument("--token", help="手工指定 token（默认从本机客户端凭据解出）")
    ap.add_argument("--only", choices=("chat", "credits", "models", "all"), default="all")
    args = ap.parse_args()

    settings = Settings.from_env()
    client = UpstreamClient(settings)
    token = args.token or settings.static_token or ""
    source = "命令行/静态配置"
    if not token:
        try:
            token = TokenSource(settings).token_for()
            source = "本机客户端凭据"
        except Exception as err:  # noqa: BLE001
            print(f"[!] 取不到 token：{err}", file=sys.stderr)
            print("    请先安装并登录 AutoClaw 客户端，或用 --token 手工传。", file=sys.stderr)
            return 2
    print(f"token 来源 = {source}，长度 = {len(token)}，"
          f"过期 = {is_expired(token)}")
    print(f"对话链路 = {client.chat_mode()}（auto 下装了客户端且指向官方网关才代发）")
    print(f"对话地址 = {client.chat_url()}")

    asyncio.run(run_probe(token, client, args.only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
