/**
 * B 站蹲饼控制台页面。
 *
 * 页面自身不保存任何状态：每次刷新都从插件后端拉状态，
 * 指令通过 bridge 转发到 ``astrbot_plugin_bilibili/console/command``。
 */

const bridge = window.AstrBotPluginPage;

const el = {
  chips: {
    engine: document.getElementById("chip-engine"),
    login: document.getElementById("chip-login"),
    fetch: document.getElementById("chip-fetch"),
    sim: document.getElementById("chip-sim"),
    dynamics: document.getElementById("chip-dynamics"),
  },
  qrBox: document.getElementById("qr-box"),
  qrPlaceholder: document.getElementById("qr-placeholder"),
  subscribers: document.getElementById("subscribers"),
  dynamics: document.getElementById("dynamics"),
  logs: document.getElementById("logs"),
  depsLogs: document.getElementById("deps-logs"),
  dispatchLogs: document.getElementById("dispatch-logs"),
  output: document.getElementById("output"),
  form: document.getElementById("command-form"),
  input: document.getElementById("command-line"),
  refresh: document.getElementById("refresh"),
};

/** 记录当前展示的二维码，避免每 5 秒重绘同一张图 */
let currentQrCode = "";

/** 向输出区追加一行文本 */
function appendOutput(text) {
  if (!text) {
    return;
  }
  const stamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  el.output.textContent += `[${stamp}] ${text}\n`;
  el.output.scrollTop = el.output.scrollHeight;
}

/** 更新状态标签 */
function setChip(node, text, on) {
  node.textContent = text;
  node.classList.toggle("on", Boolean(on));
  node.classList.toggle("off", !on);
}

/** 渲染二维码区域 */
function renderQrCode(qrcode) {
  if (!qrcode || qrcode === currentQrCode) {
    return;
  }
  currentQrCode = qrcode;
  el.qrPlaceholder.hidden = true;
  let img = el.qrBox.querySelector("img");
  if (!img) {
    img = document.createElement("img");
    img.alt = "B 站登录二维码";
    el.qrBox.appendChild(img);
  }
  img.src = `data:image/png;base64,${qrcode}`;
}

/** 格式化一组关注项 */
const formatTargets = (items) =>
  items
    .map((item) => {
      const uid = item.uid || "?";
      return item.name ? `${item.name}（${uid}）` : uid;
    })
    .join("、");

/** 渲染群订阅：群 → 关注 UP 列表 */
function renderSubscribers(state) {
  const subscribers = state.subscribers || [];
  const summary = state.subscribed_uids || [];
  el.subscribers.textContent = "";

  if (summary.length) {
    const li = document.createElement("li");
    li.textContent = `已被关注的 UP 汇总（${summary.length} 个）：${formatTargets(summary)}`;
    el.subscribers.appendChild(li);
  }

  if (!subscribers.length) {
    el.subscribers.append(
      "（暂无群订阅：群管理员在群内发送 /关注up <uid> 即可）",
    );
    return;
  }

  for (const item of subscribers) {
    const li = document.createElement("li");
    const targets = item.targets || [];
    li.textContent = `${item.platform} · 群 ${item.group_id}：`;
    const span = document.createElement("span");
    span.textContent = targets.length
      ? formatTargets(targets)
      : "（还没关注 UP，发送 /关注up <uid> 添加）";
    li.appendChild(span);
    el.subscribers.appendChild(li);
  }
}

/** 渲染最近捕获的动态 */
function renderDynamics(items) {
  el.dynamics.textContent = "";
  if (!items.length) {
    el.dynamics.append("（暂无捕获记录）");
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const author = item.author || item.uid || "未知 UP 主";
    const time = item.pubTimeText || "";
    li.textContent = `${author}${time ? ` · ${time}` : ""}：`;
    const span = document.createElement("span");
    span.textContent = (item.text || "（无正文）").slice(0, 60);
    li.appendChild(span);
    el.dynamics.appendChild(li);
  }
}

/** 渲染引擎日志 */
function renderLogs(logs) {
  el.logs.textContent = logs
    .map((line) => `[${line.level}] ${line.message}`)
    .join("\n");
  el.logs.scrollTop = el.logs.scrollHeight;
}

/** 渲染 Node 依赖安装状态（含 npm 输出尾部） */
function renderDeps(deps) {
  if (!deps) {
    return;
  }
  const state = deps.running
    ? "⏳ 正在安装…"
    : deps.installed
      ? "✅ 依赖已安装"
      : "⚠️ 依赖未安装：点「安装 Node 依赖」";
  const logs = deps.logs || [];
  el.depsLogs.textContent = [
    `${state}｜${deps.message || ""}`,
    ...(logs.length ? ["", ...logs] : []),
  ].join("\n");
  el.depsLogs.scrollTop = el.depsLogs.scrollHeight;
}

/** 渲染分发记录（每批动态是否发出去、发给哪些群） */
function renderDispatchLogs(records) {
  el.dispatchLogs.textContent = records.length
    ? records.map((item) => `[${item.time}] ${item.message}`).join("\n")
    : "（暂无分发记录）";
  el.dispatchLogs.scrollTop = el.dispatchLogs.scrollHeight;
}

/** 渲染整页状态 */
function renderState(state) {
  const status = state.status || {};
  setChip(el.chips.engine, state.running ? "引擎：运行中" : "引擎：未启动", state.running);
  setChip(el.chips.login, `登录态：${status.loggedIn ? "已登录" : "未登录"}`, status.loggedIn);
  setChip(el.chips.fetch, `蹲饼：${status.fetchRunning ? "运行中" : "已关闭"}`, status.fetchRunning);
  setChip(
    el.chips.sim,
    `模拟人格：${status.simulationRunning ? "运行中" : "已结束"}`,
    status.simulationRunning,
  );
  setChip(el.chips.dynamics, `已捕获：${status.dynamicCount ?? 0} 条`, false);

  if (!status.loggedIn) {
    renderQrCode(state.qrcode);
  }

  renderSubscribers(state);
  renderDynamics(state.dynamics || []);
  renderDeps(state.deps);
  renderDispatchLogs(state.dispatch_logs || []);
  renderLogs(state.logs || []);

  if (state.last_error) {
    appendOutput(`引擎异常：${state.last_error}`);
  }
}

/** 拉取最新状态 */
async function refresh() {
  try {
    renderState(await bridge.apiGet("console/state"));
  } catch (error) {
    appendOutput(`状态刷新失败：${error.message}`);
  }
}

/** 执行一条控制台指令 */
async function runCommand(line) {
  if (!line) {
    return;
  }
  appendOutput(`> ${line}`);
  try {
    const data = await bridge.apiPost("console/command", { line });
    const result = data.result || {};
    appendOutput(result.output || (result.ok ? "（无输出）" : "执行失败"));
    renderState(data.state || {});
  } catch (error) {
    appendOutput(`执行失败：${error.message}`);
  }
}

document.querySelectorAll("[data-line]").forEach((button) => {
  button.addEventListener("click", () => runCommand(button.dataset.line || ""));
});

el.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const line = el.input.value.trim();
  el.input.value = "";
  runCommand(line);
});

el.refresh.addEventListener("click", () => refresh());

await bridge.ready();
if (bridge.getContext()?.isDark) {
  document.body.classList.add("dark");
}
await refresh();
setInterval(() => refresh(), 5000);
