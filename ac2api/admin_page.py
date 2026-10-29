"""管理后台页面（单文件、零构建）。

为什么不是 React/Vite
--------------------
这个项目最典型的使用场景是「本机开一个客户端，一条命令把积分变成 API」。
引入前端工具链会让部署从「装 requirements 就能跑」变成「还要装 node、跑
build、配静态目录」。所以这里是一个自包含的 HTML：内联 CSS + 原生 JS，
由 FastAPI 直接吐出来。功能覆盖日常运维就够：看号、看积分到期、造 key、
看请求日志。

注意：页面里的 `x-admin-token` 从 localStorage 读，和 AC2API_ADMIN_TOKEN 对应；
没配置时后端不校验，本地自用零配置。
"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ac2api 管理台</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
    --text:#e6e9ef; --muted:#8b93a3; --accent:#4f8cff; --ok:#35c48a;
    --warn:#f5a623; --danger:#ef5b5b;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       background:var(--bg);color:var(--text)}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;
         border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
  header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.3px}
  header .badge{font-size:12px;color:var(--muted)}
  main{padding:20px;max-width:1280px;margin:0 auto;display:grid;gap:18px}
  section{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  section > h2{margin:0;padding:12px 16px;font-size:13px;font-weight:600;color:var(--muted);
               border-bottom:1px solid var(--line);letter-spacing:.6px;text-transform:uppercase}
  .body{padding:16px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
  .card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .card .k{font-size:12px;color:var(--muted)}
  .card .v{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
  .card.warn .v{color:var(--warn)}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:13px;white-space:nowrap}
  th{color:var(--muted);font-weight:500;font-size:12px}
  tr:last-child td{border-bottom:none}
  .tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;border:1px solid var(--line)}
  .tag.ok{color:var(--ok);border-color:#1f5f47}
  .tag.warn{color:var(--warn);border-color:#6b4c12}
  .tag.bad{color:var(--danger);border-color:#6b2727}
  button{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:7px 14px;
         font-size:13px;cursor:pointer}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--text)}
  button:disabled{opacity:.5;cursor:default}
  input{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:8px;
        padding:7px 10px;font-size:13px;outline:none}
  input:focus{border-color:var(--accent)}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .muted{color:var(--muted)}
  .keybox{background:#0b0d11;border:1px dashed var(--accent);border-radius:8px;padding:10px 12px;
          font-family:ui-monospace,Consolas,monospace;font-size:13px;word-break:break-all;margin-top:10px}
  .scroll{max-height:420px;overflow:auto}
  .kv{display:grid;grid-template-columns:110px 1fr;gap:8px 14px;align-items:center}
  .kv .k{font-size:12px;color:var(--muted)}
  .field{display:flex;gap:8px;align-items:center}
  .field code{flex:1;background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:7px 10px;
              font-family:ui-monospace,Consolas,monospace;font-size:13px;word-break:break-all;color:var(--text)}
  button.mini{padding:5px 10px;font-size:12px}
  .hint{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.7}
  pre.snip{background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:10px 0 0;
           font-family:ui-monospace,Consolas,monospace;font-size:12px;white-space:pre-wrap;color:#c8d0dd}
</style>
</head>
<body>
<header>
  <h1>ac2api</h1>
  <span class="badge" id="health">连接中…</span>
  <span style="flex:1"></span>
  <input id="adminToken" placeholder="x-admin-token（未配置可留空）" style="width:260px">
  <button class="ghost" onclick="reload()">刷新</button>
</header>

<main>
  <section>
    <h2>接入信息 · 填到客户端里</h2>
    <div class="body">
      <div class="kv">
        <div class="k">API 地址</div>
        <div class="field">
          <code id="baseUrl">—</code>
          <button class="mini ghost" onclick="copyText('baseUrl', this)">复制</button>
        </div>
        <div class="k">API Key</div>
        <div class="field">
          <code id="pickKey">还没有 key，先在下面生成一个</code>
          <button class="mini ghost" onclick="copyText('pickKey', this)">复制</button>
        </div>
        <div class="k">可用模型</div>
        <div class="field">
          <code id="modelList">—</code>
          <button class="mini ghost" onclick="copyText('modelList', this)">复制</button>
        </div>
      </div>
      <div class="hint" id="usageHint"></div>
      <pre class="snip" id="snip"></pre>
    </div>
  </section>

  <section>
    <h2>积分总览</h2>
    <div class="body cards" id="cards"></div>
  </section>

  <section>
    <h2>账号池 · 按「最快要作废」排序</h2>
    <div class="body">
      <div class="row" style="margin-bottom:12px">
        <input id="newAccount" placeholder="账号标识（如 alice@example.com）" style="width:280px">
        <button onclick="addAccount()">加入账号</button>
        <button class="ghost" onclick="refreshCredits()">全部刷新积分</button>
        <span class="muted" id="expiringHint"></span>
      </div>
      <div class="scroll"><table>
        <thead><tr>
          <th>账号</th><th>状态</th><th>剩余积分</th><th>7 天内到期</th>
          <th>最早到期</th><th>在途</th><th>备注</th>
        </tr></thead>
        <tbody id="accounts"></tbody>
      </table></div>
    </div>
  </section>

  <section>
    <h2>API Key</h2>
    <div class="body">
      <div class="row" style="margin-bottom:12px">
        <input id="keyName" placeholder="名称，如 my-laptop" style="width:200px">
        <input id="keyLimit" placeholder="本地积分配额（留空=不限）" style="width:230px">
        <button onclick="createKey()">生成 Key</button>
      </div>
      <div id="newKey"></div>
      <div class="scroll" style="margin-top:12px"><table>
        <thead><tr><th>名称</th><th>状态</th><th>已用</th><th>配额</th>
          <th>请求数</th><th>速率</th><th></th></tr></thead>
        <tbody id="keys"></tbody>
      </table></div>
    </div>
  </section>

  <section>
    <h2>最近请求</h2>
    <div class="body scroll"><table>
      <thead><tr><th>时间</th><th>Key</th><th>账号</th><th>模型</th><th>流式</th>
        <th>状态</th><th>耗时</th><th>积分</th><th>错误</th></tr></thead>
      <tbody id="logs"></tbody>
    </table></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const num = (v) => (v === null || v === undefined) ? "—"
  : Number(v).toLocaleString("zh-CN", {maximumFractionDigits: 2});
const t = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";

// ---- 接入信息：API 地址 / key / 模型，直接抄进客户端 ----
const BASE_URL = location.origin + "/v1";
// 刚生成的 key 只在这里出现一次，所以额外存一份到 localStorage 方便再次复制。
let lastKey = localStorage.getItem("ac2api_last_key") || "";

/** 把某块文本复制到剪贴板，顺便把按钮闪一下。 */
async function copyText(id, btn) {
  const text = $(id).textContent.trim();
  const old = btn.textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // http 页面在部分浏览器里没有 clipboard API，退回老办法。
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  btn.textContent = "已复制";
  setTimeout(() => { btn.textContent = old; }, 1200);
}

/** 渲染「接入信息」卡片：地址、key、模型、示例代码。 */
function renderAccess(d) {
  const models = (d.models || []).map(m => m.id);
  const activeKey = d.keys.find(k => k.status === "active");
  // 优先进程页面记住的那个 key；否则提示去生成。
  const keyText = lastKey || (activeKey ? "（key 已隐藏，在下面重新生成一个即可）" : "还没有 key，先在下面生成一个");
  $("baseUrl").textContent = BASE_URL;
  $("pickKey").textContent = keyText;
  $("modelList").textContent = models.length ? models.join("、") : "上游未下发模型目录";

  const model = models[0] || "zai_auto";
  const shownKey = lastKey || "<你的key>";
  $("usageHint").innerHTML =
    `把上面的「API 地址」和「API Key」填进 OpenAI 兼容客户端即可（Cherry Studio / NextChat / Cline 等）。<br>` +
    `当前请求链路：<b>${d.health.chat_mode}</b> —— ` +
    (d.health.chat_mode === "direct"
      ? "直连官方网关。对话、积分、模型目录均可用，<b>不依赖本机 AutoClaw 客户端是否开启</b>。"
      : "经本机 AutoClaw 客户端代发（兜底路径，需客户端开着）。");
  $("snip").textContent =
`curl ${BASE_URL}/chat/completions \\
  -H "Authorization: Bearer ${shownKey}" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${model}","messages":[{"role":"user","content":"你好"}]}'`;
}

$("adminToken").value = localStorage.getItem("ac2api_admin") || "";
$("adminToken").onchange = (e) => localStorage.setItem("ac2api_admin", e.target.value);

async function api(path, opts = {}) {
  const headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {});
  const token = $("adminToken").value.trim();
  if (token) headers["x-admin-token"] = token;
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  if (!res.ok) throw new Error((await res.text()).slice(0, 300));
  return res.json();
}

function statusTag(s) {
  const map = {active: ["ok", "可用"], exhausted: ["bad", "积分耗尽"], cooling: ["warn", "冷却中"],
               disabled: ["bad", "已停用"], no_token: ["warn", "无 token"]};
  const [cls, label] = map[s] || ["warn", s];
  return `<span class="tag ${cls}">${label}</span>`;
}

function render(d) {
  const p = d.stats.pool, h = d.health;
  renderAccess(d);
  $("health").textContent = `上游 ${h.upstream} · 对话端点 ${h.chat_path} · ${p.active}/${p.accounts} 个账号可用`;
  $("cards").innerHTML = [
    ["可用账号", `${p.active} / ${p.accounts}`, ""],
    ["总剩余积分", num(p.total_remaining), ""],
    ["7 天内到期", num(p.expiring_soon_remaining), "warn"],
    ["在途请求", p.in_flight, ""],
  ].map(([k, v, cls]) => `<div class="card ${cls}"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  $("expiringHint").textContent = p.expiring_accounts.length
    ? `⚠ ${p.expiring_accounts.length} 个账号有 7 天内到期的积分，调度已优先使用它们`
    : "没有即将作废的积分";

  $("accounts").innerHTML = d.accounts.length ? d.accounts.map(a => `<tr>
    <td>${a.nickname || a.key}<div class="muted" style="font-size:12px">${a.key}</div></td>
    <td>${statusTag(a.status)}</td>
    <td>${num(a.remaining)}<span class="muted"> / ${num(a.total_capacity)}</span></td>
    <td>${a.expiring_soon_remaining ? `<b style="color:var(--warn)">${num(a.expiring_soon_remaining)}</b>` : "—"}</td>
    <td>${a.soonest_expire_at ? `${t(a.soonest_expire_at/1000)}<div class="muted" style="font-size:12px">${a.soonest_days_left ?? ""} 天</div>` : "长期有效"}</td>
    <td>${a.in_flight}</td>
    <td class="muted" style="white-space:normal;max-width:320px">${a.last_error || ""}</td>
  </tr>`).join("") : `<tr><td colspan="7" class="muted">还没有账号</td></tr>`;

  $("keys").innerHTML = d.keys.length ? d.keys.map(k => `<tr>
    <td>${k.name}</td>
    <td>${k.status === "active" ? '<span class="tag ok">启用</span>' : '<span class="tag bad">停用</span>'}</td>
    <td>${num(k.credit_used)}</td>
    <td>${k.credit_limit === null ? "不限" : num(k.credit_limit)}</td>
    <td>${k.request_count}</td>
    <td>${k.rate_limit}/分</td>
    <td><button class="ghost" onclick="toggleKey('${k.name}')">${k.status === "active" ? "停用" : "启用"}</button></td>
  </tr>`).join("") : `<tr><td colspan="7" class="muted">还没有 key，先在上面生成一个</td></tr>`;

  $("logs").innerHTML = d.logs.length ? d.logs.map(r => `<tr>
    <td>${t(r.ts)}</td><td>${r.key_name}</td><td>${r.account_key}</td><td>${r.model}</td>
    <td>${r.stream ? "是" : "否"}</td>
    <td>${r.status === "ok" ? '<span class="tag ok">成功</span>' : `<span class="tag bad">${r.status}</span>`}</td>
    <td>${r.latency_ms} ms</td><td>${num(r.credits)}</td>
    <td class="muted" style="white-space:normal;max-width:360px">${r.error || ""}</td>
  </tr>`).join("") : `<tr><td colspan="9" class="muted">暂无请求</td></tr>`;
}

async function reload() {
  try { render(await api("/admin/api/overview")); }
  catch (e) { $("health").textContent = "加载失败：" + e.message; }
}

async function createKey() {
  const body = {name: $("keyName").value.trim() || undefined};
  const limit = $("keyLimit").value.trim();
  if (limit) body.credit_limit = Number(limit);
  const data = await api("/admin/api/keys", {method: "POST", body: JSON.stringify(body)});
  lastKey = data.api_key;
  localStorage.setItem("ac2api_last_key", lastKey);
  $("newKey").innerHTML = `<div class="keybox">${data.api_key}</div>
    <div class="muted" style="margin-top:6px">${data.note}</div>`;
  reload();
}

async function toggleKey(name) {
  await api(`/admin/api/keys/${encodeURIComponent(name)}/toggle`, {method: "POST"});
  reload();
}

async function addAccount() {
  const key = $("newAccount").value.trim();
  if (!key) return;
  await api("/admin/api/accounts", {method: "POST", body: JSON.stringify({key})});
  $("newAccount").value = "";
  reload();
}

async function refreshCredits() {
  $("expiringHint").textContent = "刷新中…";
  await api("/admin/api/refresh", {method: "POST"});
  reload();
}

reload();
setInterval(reload, 15000);
</script>
</body>
</html>
"""
