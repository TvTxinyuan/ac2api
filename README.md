# ac2api —— 把 AutoClaw 的积分变成 OpenAI 兼容 API

把 AutoClaw（智谱「澳龙」桌面客户端）账号里的**积分额度**，包一层标准 OpenAI 协议暴露出来。
于是 Cherry Studio / Cline / Continue / Codex / LangChain / 任何只认 `base_url + api_key` 的
客户端，都能直接使用你的 AutoClaw 积分。

思路仿照 [xiaofan6ya/wb2api](https://github.com/xiaofan6ya/wb2api)（原 `workbuddy2api`），但
**不是**简单复制：AutoClaw 的鉴权、签名、对话链路都和 WorkBuddy 完全不同，下面
「和 wb2api 的差异」一节写清了每一处为什么这么设计。

---

## 它是怎么工作的

```
   你的客户端 ——(OpenAI 协议 + ac2api key)——> ac2api
                                               │
                           ┌───────────────────┴────────────────────┐
                    积分 / 到期 / 模型目录                       对话
                  autoglm-api.zhipuai.cn              autoglm-acceleration-api.zhipuai.cn
                   （普通 Bearer 即可）                  （X-Authorization + system 前缀）
```

上游本身就是 OpenAI 风格（`messages` / `tools` / SSE），所以网关不做协议翻译，
只做三件上游没提供的事：

1. **鉴权接续**：token 由本机客户端持有，外部拿不到。ac2api 自己从客户端凭据
   里解出来（DPAPI + AES-GCM），过期了用 refreshToken 自动续。
2. **把「积分」变成「配额」**：客户端只认一个 key，ac2api 给每个 key 记账，
   可以限「这个 key 最多消耗多少积分」，超了返回 402。
3. **让要过期的积分先被用掉**：这是本项目真正的价值点，见下一节。

### 对话链路：一个 406 踩了很久，真因却很朴素

对话端点和身份域不是同一个域名，也不是同一套鉴权头：

| 域 | 用途 | 鉴权头 |
|---|---|---|
| `autoglm-api.zhipuai.cn` | 钱包、积分、模型目录 | `Authorization: Bearer <token>` |
| `autoglm-acceleration-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw` | 对话 | `X-Authorization: Bearer <token>` |

把 token 放进 `Authorization` 打对话端点 → `401 {"code":401001,"message":"Invalid
token"}`（那一层根本不读这个头）；放进 `X-Authorization` 才是对的。

然后就是那个折磨人的 `406`（空 body、无 code、无任何说明）。前期一度以为是缺少
客户端的 Ed25519 请求签名，为此把客户端的签名器（握手 + HKDF + AES-GCM + PoW）
整个逆向还原了一遍（代码还留在 `ac2api/signer.py`）。**但真因不是签名。**

> 406 是**四条互相独立的请求校验**，任意一条不满足都返回同样的空 body。
> 上游的报错文案只提了第一条，所以第 2–4 条是自己二分出来的。

| # | 规则 | 处理 |
|---|---|---|
| 1 | system prompt 必须以 `"You are AutoClaw. Answer the user directly and concisely."` 开头 | `ensure_system_prompt()` |
| 2 | 正文不能出现 `codex cli` / `claude code` | `scrub_agent_phrases()` |
| 3 | 正文不能出现 `You are Codex` / `You are Claude Code` 句式 | `scrub_identity()` |
| 4 | `max_tokens` 必须落在 `[10000, 131072]`（或干脆不发） | `normalize_max_tokens()` |

第 1 条是抓包拿到客户端真实请求后，把 64 KB 的 body 逐项二分出来的：同一 token、
同一 URL、同一个 `X-Request-Model`，只要 system 前缀对上就 200，否则 406。
签名头（`X-Client-Sig` / `X-Client-Pow`）**一个都不需要**。

所以 `ac2api/upstream.py` 里有个 `ensure_system_prompt()`：调用方没带 system
就补上；带了但不合规就把前缀拼在前面、原 system 降级为后文（用户的内容一个字
不丢）。这也是为什么**第三方客户端不用做任何改造**就能用。

#### 还有第二条 406 规则：竞品客户端名黑名单

前缀合规**不等于**放行。上游正文里还会拦两个词（实测，大小写不敏感，词间
多余空白也算）：

| 正文里出现 | 结果 |
|---|---|
| `codex cli` | **406** |
| `claude code` | **406** |
| `Codex and CLI`（分开写） | 200 |
| `OpenAI Codex`（顺序反了） | 200 |
| Cursor / Copilot / Cline / Windsurf / aider / Gemini CLI / Trae / 通义灵码 … | 200 |

所以黑名单是**字面量匹配**，不是"识别竞品客户端"。麻烦的是 Codex 桌面端的
`instructions` 里天生带 `running in the Codex CLI`，原样转发就每次 406。

`scrub_agent_phrases()` 会把这些词替换成中性说法（`terminal coding agent`，
句首保持大写），system / user / 多模态文本块都会洗。见
`BLOCKED_AGENT_PHRASES`。

#### 第三条 406 规则：`You are <竞品>` 句式

这条和第 2 条**不是一回事**——`You are Codex.` 里既没有 `codex cli` 也没有
`claude code`，但照样 406。它拦的是句式，不是词：

| system 里出现 | 结果 |
|---|---|
| `You are Codex.` / `you are codex.` / `You are CODEX.` / `You are Codex` | **406** |
| `You are Claude Code.` / `You are claude code.` | **406** |
| `Hi. You are Codex.`（行内任意位置） | **406** |
| `Codex` / `I am Codex` / `This is Codex`（词单独出现） | 200 |
| `You are  Codex.`（**两个**空格） | 200 |
| `You are a Codex.`（中间插词） | 200 |
| `You are OpenAI Codex.`（顺序反了） | 200 |
| `You are ChatGPT.` / `You are Claude.` / `You are Cursor.` / `You are Gemini.` … | 200 |
| 同样内容放在 **user** 消息里 | 200（只查 system） |

也就是说匹配规则是 `\byou are (codex|claude code)\b`：**单个空格**、大小写不敏感、
只查 system。Codex 桌面端的 instructions 第一句正好就是 `You are Codex.`，
所以光洗第 2 条是过不去的。

`scrub_identity()` 把它替换成 `You are a terminal coding agent`。
`scrub_text()` 是两条规则一起过，`_scrub_message()` 走的是它。

#### 第四条 406 规则：`max_tokens` 有取值区间

这条最阴，因为它跟前三条**完全独立**，而且错的值恰恰是各家的默认值。

实测（真账号、四个模型、system 前缀都合规）：

| `max_tokens` | 结果 |
|---|---|
| 不发这个字段 | **200** |
| 1024 / 4096 / 8192 / 8193 | **406** |
| 10000 / 16384 / 32768 / 131072 | 200 |
| 131073 / 150000 / 200000 / 393216 | **406** |
| `max_completion_tokens: 4096`（换字段名） | 200 |

也就是只放行 **[10000, 131072]**。上界跟模型无关——`zai_auto` 在模型目录里写的是
393216，真发 393216 照样 406，所以这是网关侧的固定区间，不是逐模型的 `maxTokens`。

**Codex 默认 `max_output_tokens = 4096`，正好死在 406 上。** 表现就是配置全对、
key 也通，但每次对话都 `unexpected status 406 ... upstream 406`。

`normalize_max_tokens()` 在转发前把值夹进区间（调用方给多少就夹多少，不擅自
放宽；一个没给就不加），`max_completion_tokens` 统一成 `max_tokens`。

> 排查建议：`406` 空 body 在上述四条规则里任一不满足都会出现，报错文本只提示了
> 第一条。真遇到时依次确认：system 前缀 → `codex cli` 词 → `You are <竞品>`
> 句式 → `max_tokens` 区间。

自查一次四条规则是否都生效：

```python
from ac2api.upstream import ensure_system_prompt, normalize_max_tokens
p = {"messages": [{"role": "system", "content": "You are Codex."}], "max_tokens": 4096}
print(ensure_system_prompt(normalize_max_tokens(p))["messages"][0]["content"])
# You are AutoClaw. Answer the user directly and concisely.
# You are a terminal coding agent
```

#### 并发：`pick()` 必须在线程里等

只有一个账号时，客户端并发两个请求必然撞车。`AccountPool.pick(wait_s=...)`
会排队等前一个归还租约，而不是立刻 503。但它是**同步阻塞**的，如果直接在
async 路由里调用，会把整个事件循环卡死（表现：三个并发全部超时，日志里
`lat=120029`）。所以 `main.py` 里必须写成：

```python
account = await asyncio.to_thread(gw.pool.pick, session_key=sid, exclude=tried,
                                  wait_s=POOL_WAIT_S)
```

### 计费口径

上游的 `usage` 里**只有 token 数，没有积分字段**，且扣费不是 1 token = 1 积分。
实测（连打 4 次、共 423 token）恰好扣 4 积分，即约 **100 token 折 1 积分、
每次调用至少 1 积分**。所以本地记账按这个口径折算（向上取整、最低 1），
可用 `AC2API_TOKENS_PER_CREDIT` 调整。这只影响你给每个 API Key 设的
`credit_limit`，不影响上游真实扣费。

**当前进度（真实账号实测 2026-10-29，客户端 2.2.4，客户端**关闭**状态下）：**

| 能力 | 状态 |
|---|---|
| 解出 accessToken / refreshToken、自动续期 | ✔ |
| 钱包余额与分包裹到期时间 | ✔ 19954 积分 / 2 个包裹 |
| 模型目录 | ✔ 3 个可用模型 |
| 到期感知选号、API Key 记账与限额、管理台 | ✔ |
| `/v1/chat/completions` 非流式 | ✔ 真实回复 |
| `/v1/chat/completions` 流式 SSE | ✔ 真实流 |
| `/v1/responses`（Codex / ChatGPT 桌面端） | ✔ 真实回复 |
| 本地记账与上游真实扣费一致 | ✔ 3 轮对话记 4 积分，钱包同步减 4 |

不需要安装/运行 AutoClaw 客户端进程，只要它的凭据还在本机（装在哪个账号下都行，
解不开时也可以用 `AC2API_STATIC_TOKEN` 直接喂 token）。

---

## 核心：积分到期感知调度

积分有保质期，而「平均分摊到每个账号」是最差的调度策略 —— 那会让每个号都
在临到期时剩一堆额度，然后一起作废。所以选号不看「谁额度多」，而是看
**谁的积分最快要作废**（`ac2api/credits.py: urgency_rank`）：

| 层级 | 含义 | 处理 |
|---|---|---|
| 0 | 有**7 天内**到期的剩余额度 | 最优先，抢在作废前烧掉 |
| 1 | 有过期时间但还早 | 次优先，按到期时间从近到远 |
| 2 | 全部长期有效 | 兜底，只在没有前两类时使用 |

同一层内再按权重打分，取前 5 个里随机一个（保证优先级稳定，又不让并发全
压在一个号上）：

```
score = 10 × 剩余额度(万) + 8 × 紧迫度 + min(5, 0.5 × 空闲小时数)
```

本机实测效果（单账号 19995 积分）：

```
credits total_remaining: 19995.0  pkg: 2  soonest_days: 3.84
  - 奖励积分 9995.0  3 天后到期
  - 奖励积分 10000.0 6 天后到期
```

两个包都在 7 天内 → 进「快过期」层 → 优先被烧掉。没有这套调度，3 天后那 9995
积分会整包作废。

### 三个真实会踩的坑

1. **到期字段有占位值。** 某些资源包的 `DeductionEndTime` 是 2034 年的占位
   时间，直接信它会把「快过期」误判成「长期有效」。`credits.py` 里按
   `FAR_FUTURE_EXPIRY_DAYS`（730 天）识别并改判。
2. **同一份额度会重复计数。** 接口同时返回「按周期」和「按包」两种视图，
   直接相加会把额度翻倍。`extract_resources` 做了去重。
3. **时区。** 上游时间戳是毫秒 UTC，本地是 UTC+8，直接 `datetime.fromtimestamp`
   会差 8 小时、把「3 天后到期」算成「11 小时后到期」。统一按 UTC 处理。

---

## 快速开始

有两种用法，功能完全一样：

- **想双击就用**：下载 `ac2api-windows-x64.zip`，解压后运行 `ac2api.exe`（见下面的[桌面版](#桌面版双击即用)）。
- **想跑在命令行 / Linux / 自动启动**：用下面的 Python 方式。

```bash
pip install -r requirements.txt
python -m ac2api            # 默认 127.0.0.1:8000
```

### 桌面版（双击即用）

`ac2api.exe` 是一个托盘程序：双击后图标待在右下角托盘，网关在后台跑，点托盘
菜单里的「打开管理界面」就是那个 Web 管理台。

- 没有黑窗口，没有命令行。退出用托盘菜单的「退出」。
- 托盘图标颜色就是状态：<br>
  🟢 正常 · 🟠 有账号快到期或积分偏低 · 🔴 全部账号不可用 · 🔵 正在启动
- 托盘菜单还能：复制接入地址、看状态与用法、打开日志。
- 只允许开一个实例。重复双击会直接打开已有实例的管理页，不会抢端口。
- 想换端口就设环境变量 `AC2API_PORT`。

打包是 PyInstaller **onedir**（整个文件夹）。没有用 onefile 自解压是因为
Windows Defender + 国内安全软件对自解压 exe 误报率很高，会被直接拦掉。

### 数据放在哪

默认 `~/.ac2api`（`keys.json`、`desktop.log`）。用 `AC2API_DATA_DIR` 可以改。

**但用户主目录可能写不进去**——安全软件或组策略会拦住陌生 exe 写用户目录。
这时 ac2api 会自动退到程序旁边的 `ac2api-data/`，再退到系统临时目录。它不会
静默失败，管理台的「数据目录」一栏显示的就是实际在用的路径。

> 这个回退是被真实故障逼出来的：打包后的 exe 一切功能正常，只有 key 存不下、
> 日志一片空白，排查了很久才确认是写用户目录被拒。现在有专门的用例守着
> （`selftest.py` 里的 `test_data_dir_fallback`）。

首次启动有两种接入方式：

### A. 装了 AutoClaw 客户端（推荐）

不用填任何 token。ac2api 会自动找到客户端的数据目录，解开凭据文件
（Windows DPAPI + AES-GCM，只在本机、本用户下可解），并在过期时自动 refresh。

```bash
python -m ac2api.probe      # 先体检：token / 积分 / 模型目录 / 对话
```

### B. 没装客户端

需要手工提供 accessToken：

```bash
set AC2API_TOKEN=eyJhbGciOi...
python -m ac2api
```

或者起一个外部的 token 服务，让 ac2api 去取：

```bash
set AC2API_TOKEN_URL=http://127.0.0.1:9000/get_token
```

### 拿到 API key

打开 <http://127.0.0.1:8000/admin>，页面上直接写着**接入地址**和**生成 key**的按钮。
命令行也行：

```bash
curl -X POST http://127.0.0.1:8000/admin/api/keys -H "Content-Type: application/json" ^
     -d "{\"name\":\"cline\",\"credit_limit\":100}"
```

### 在客户端里填

| 客户端 | Base URL | 说明 |
|---|---|---|
| Cherry Studio / Cline / Continue | `http://127.0.0.1:8000/v1` | 选 OpenAI 兼容 |
| Codex CLI | `base_url = "http://127.0.0.1:8000/v1"` + `wire_api = "responses"` | 用 `/v1/responses` |
| LangChain | `base_url="http://127.0.0.1:8000/v1"` | `ChatOpenAI` |

---

## 接口

| 端点 | 说明 |
|---|---|
| `GET /health` | 存活 + 账号数（不需要 key） |
| `GET /v1/models` | 模型清单（取不到上游时回退静态表） |
| `POST /v1/chat/completions` | 标准 OpenAI 对话，支持 `stream` |
| `POST /v1/responses` | Responses 协议（Codex / ChatGPT 桌面端），支持 `stream` |
| `GET /v1/balance`、`/user/balance` | 当前 key 的配额与池子剩余 |
| `GET /admin` | 管理台（账号、key、实时日志、接入信息） |
| `GET /admin/api/overview` | 账号 / key / 日志 JSON |
| `POST /admin/api/keys` | 生成 key（`name`、`credit_limit`、`rate_limit`） |
| `POST /admin/api/keys/{name}/toggle` | 停用 / 启用 key |
| `POST /admin/api/accounts` | 增删改账号 |
| `POST /admin/api/refresh` | 立刻刷新所有账号积分 |

---

## 配置

全部走环境变量，见 `.env.example`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AC2API_HOST` / `AC2API_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `AC2API_STATIC_TOKEN` | 空 | 手工 accessToken（不装客户端时用） |
| `AC2API_TOKEN_URL` | 空 | 外部 token 服务地址 |
| `AC2API_UPSTREAM_BASE` | `https://autoglm-acceleration-api.zhipuai.cn` | 模型网关 |
| `AC2API_ACCOUNT_BASE` | `https://autoglm-api.zhipuai.cn` | 身份域（积分/钱包） |
| `AC2API_DEFAULT_MODEL` | 空 | 请求没带 model 时补这个 |
| `AC2API_MODELS` | 空 | 逗号分隔的模型白名单 |
| `AC2API_DISCOVER` | `1` | 是否扫本机客户端的托管配置 |
| `AC2API_ADMIN_TOKEN` | 空 | 给管理接口加一层口令 |
| `AC2API_CHAT_MODE` | `direct` | `direct` / `client` / `auto`。`direct` 直连上游（默认、推荐）；`client` 借本机客户端代发（兜底，需客户端开着）；`auto` 有客户端才代发 |
| `AC2API_TOKENS_PER_CREDIT` | `100` | 本地额度记账口径：多少 token 折 1 积分（不影响上游真实扣费） |
| `AC2API_SIGN_CREDENTIAL` | 空 | 客户端签名凭证 `id.secret`。**现在不需要**，只为兼容保留 |
| `AC2API_CLIENT_TOKEN_ENDPOINT` | `http://127.0.0.1:18432/get_token` | 客户端本地取 token 服务（仅 `client` 模式使用） |
| `AC2API_DATA_DIR` | `~/.ac2api` | key 存储目录 |
| `AC2API_KEYS_FILE` | `$AC2API_DATA_DIR/keys.json` | key 文件路径（覆盖上面那条） |

### API Key 存在哪里

key **落盘保存**，网关重启后照样有效——这一点很重要：早期版本只放内存，重启后
Codex / CC Switch 里填的 key 全变 401，看起来像填错了，其实是网关自己把钥匙丢了。

- 位置：`~/.ac2api/keys.json`（可用 `AC2API_DATA_DIR` / `AC2API_KEYS_FILE` 改）
- 内容：**只有 `sha256(pepper + key)`**，不存明文；文件被拷走也换不回可用的 key
- 同时保存配额、用量计数、启停状态，重启后限额和账单接着算
- 原子写（先写临时文件再 `os.replace`），断电不会写出半个文件
- 用量计数按 15 秒节流写盘，Ctrl+C 退出时会兜底 flush 一次

> 早期版本写过的 `keys.json` 里可能残留测试 key（`smoke`、`from-admin` 等），
> 直接编辑该文件删掉对应记录、重启即可。

---

## 和 wb2api 的差异

| 方面 | wb2api（WorkBuddy） | ac2api（AutoClaw） |
|---|---|---|
| 上游协议 | 就是 OpenAI，只换鉴权头 | 也是 OpenAI 形状，但要换域名 + 换头名（`X-Authorization`）+ system 前缀 |
| token 来源 | 上游直接给 | 加密存在客户端凭据里，要 DPAPI + AES-GCM 解 |
| 品牌/版本头 | 无 | `X-Version` / `X-Tm` / `X-Product` / `X-Auth-Appid` / `X-Auth-TimeStamp` / `X-Auth-Sign` 六件套 |
| 鉴权头名 | `Authorization` | 身份域用 `Authorization`，模型网关用 `X-Authorization`（写错直接 401）|
| 选号 | 三因子加权 | 同样三因子，但多了「到期层级」前置过滤（积分会过期） |
| 端点 | `/v1/chat/completions` 等 | 同上，另加 `/v1/responses`（给 Codex） |

---

## 406 排查记录（留给后来人，别再走一遍）

这一节只记录**走过的弯路**，正解见上面「对话链路」。当时的错误假设是「缺客户端
Ed25519 签名」，于是把这些都试了一遍，全部无效：

- 各种 Header 组合（`X-Request-Model` 有无、UA、`X-Session-Id`、`X-Query-Id`、
  `X-Client-Type`、version、stream、Accept）。
- 真实 session/run id（从客户端 `runtime.sqlite` 里读出来的）。
- 换 token / 用 refreshToken 续期后再打。
- 用**自造** Ed25519 私钥按客户端格式签名（格式完全正确、`X-Client-Sig` 结构
  对得上）—— 仍 406。这次反证其实已经在提示：**问题不在签名**。
- 打身份域找对话端点 —— 身份域没有 chat 端点。
- MITM 抓握手 —— 客户端 3~5ms 内主动断连，自定义 CA、证书链、双 IP 监听都试过；
  直接 `HTTP_PROXY` 劫持也不信任自签 CA。

真正解开的一步是**用 MITM 抓到客户端完整的一次对话请求体**（64 KB），逐项二分后
发现：把 body 原样重放 → 200；只把 system prompt 换成一句短的 → 406。于是定位到
前缀校验，问题解决。

第 2–4 条规则都是后来接 Codex 时才暴露的，一条比一条隐蔽：

- **第 4 条（`max_tokens`）**：前缀和竞品名都修好了，Codex 依然 406。把失败的请求体
  dump 下来逐个字段删，发现删掉 `max_tokens` 就 200 —— Codex 默认发 4096，而只有
  `[10000, 131072]` 才放行。再扫一遍区间才定出两条边界（`8193` 过不了、`10000` 过；
  `131072` 过、`131073` 过不了），并且上界对四个模型都一样。
- **第 3 条（`You are Codex.`）**：修完第 4 条，用 `instructions="You are Codex."`
  单独打一次仍然是 406，而 `"You are a coding agent…"` 却能过。沿着这个差异穷举
  `You are <X>` 的各种 X 才定下来：只有 `codex` / `claude code` 两个词命中，必须是
  单个空格，只查 system。**它跟第 2 条是不同的规则**——`You are Codex.` 不含
  `codex cli` 这个子串，所以洗第 2 条时完全不会被碰到。

排查方法上唯一有效的一条：**把真正发往上游的 payload 落盘**（`AC2API_DEBUG_DUMP=<目录>`），
然后拿它去逐项二分。所有假设都是被重放的 body 推翻或确认的，靠猜没有一次猜对。

`ac2api/clawproxy.py`（伪装进程名借客户端代发）是绕这条弯路时写的，现在默认不启用，
留作上游改规则时的兜底。

## 另一个隐形坑：系统代理会吃掉 127.0.0.1 的请求

如果你开着 Clash / v2ray 之类的挂机代理（典型配置 `127.0.0.1:7890` + `ProxyEnable=1`），
又遇到**空 body 的 `502 Bad Gateway`**，`Content-Length: 0`、`Connection: close`，
网关日志里却什么都看不到 —— 那不是上游故障。

原因是 httpx 默认 `trust_env=True`，会自动读 Windows 系统代理，而且**不认
`ProxyOverride` 里对 `localhost` / `127.*` 的豁免**。于是连「访问本机 token 服务」
「访问本机假上游」的请求都被塞给代理，代理回一个空 502。

本项目已经在三个出站口全部直连：`upstream.py` 的 `UpstreamClient._http()`、
`token.py` 的本地 token 请求、`clawproxy.py` 的 `fetch_local_token()`。
需要经代理访问上游时，用 `AC2API_UPSTREAM_PROXY` 显式指定，别依赖系统代理。

判断技巧：代理问题只影响**真的走网络**的请求。`fastapi.testclient.TestClient`
不走网络，如果它能拿到 200 而 `httpx` 拿到 502，基本就是这个坑。

---

## 开发自测

```bash
python selftest.py      # 单元测试（积分口径 / 选号 / 租约 / key 配额 / token）
python smoke_test.py    # 端到端：真起 uvicorn + 假上游，跑完全部接口
```

两个脚本都不需要真实账号，smoke_test 用假上游验证协议透传、换号重试、SSE、
Responses 翻译、积分口径与管理接口。

### 自己打包桌面版

```bash
pip install -r requirements-desktop.txt
python make_icon.py         # 生成 assets/ac2api.ico
python build_desktop.py     # 产物：dist/ac2api/ + dist/ac2api-windows-x64.zip
```

> 打包和跑脚本**别用 `.venv`**。在虚拟环境里 `pip install` 可能长时间卡在
> `Looking in indexes:` 不动；用系统 Python 装依赖再打包是稳的。

---

## 目录

```
ac2api/
  __main__.py     启动入口
  main.py         FastAPI 应用 + 路由
  pool.py         账号池：租约、粘性、退避、三因子选号
  credits.py      积分口径：到期层级、去重、占位值识别
  keys.py         API key：sha256 存储、配额、限速
  store.py        key 落盘（~/.ac2api/keys.json，原子写、只存哈希）+ 数据目录回退
  upstream.py     上游适配：鉴权头、system 前缀、错误码映射、模型目录、钱包、对话
  token.py        凭据解密（DPAPI + AES-GCM）、refresh、TokenSource
  responses.py    Responses ↔ Chat Completions 翻译（含流式）
  desktop.py      桌面版：托盘、单实例、后台网关线程、日志
  clawproxy.py    兜底：借客户端之手代发对话（默认不用）
  signer.py       客户端签名还原 + Ed25519 签名器（排查 406 的遗留物，默认不用）
  probe.py        上游体检
  admin_page.py   管理台单页
desktop_entry.py  桌面版入口（给 PyInstaller 用）
build_desktop.py  打包脚本
make_icon.py      图标生成
```

## 许可

仅供个人学习研究。请遵守 AutoClaw / 智谱的服务条款，不要用于商业分发。
