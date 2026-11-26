"use strict";

const $ = (id) => document.getElementById(id);
let csrf = "";
let readOnly = false;
let features = [];
let activeTab = "overview";
let toastTimer;

function toast(message, error = false) {
  const box = $("toast");
  box.textContent = String(message);
  box.classList.toggle("error", error);
  box.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.add("hidden"), 5000);
}

async function request(path, method = "GET", body = null) {
  const headers = {"Accept": "application/json"};
  if (body !== null) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrf) headers["X-CSRF"] = csrf;
  const response = await fetch(path, {method, headers, credentials: "same-origin", body: body === null ? undefined : JSON.stringify(body), cache: "no-store"});
  let result;
  try { result = await response.json(); } catch { throw new Error("服务器返回格式不正确"); }
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new Error(result.error || `请求失败 (${response.status})`);
  }
  return result;
}

function showLogin() {
  csrf = "";
  $("login-view").classList.remove("hidden");
  $("dashboard").classList.add("hidden");
  $("page-select").classList.add("hidden");
}

function showDashboard() {
  $("login-view").classList.add("hidden");
  $("dashboard").classList.remove("hidden");
  $("page-select").classList.remove("hidden");
  $("session-role").textContent = readOnly ? "本次登录：只读，不能修改设置或启停机器人。" : "本次登录：管理权限。登录将在12小时后过期。";
  for (const id of ["start-bot", "restart-bot", "stop-bot", "refresh-sessions"]) $(id).disabled = readOnly;
}

async function refreshSessions() {
  if (readOnly) { $("session-devices").replaceChildren(); return; }
  try {
    const result = await request("/api/sessions");
    const devices = (result.devices || []).map(device => {
      const line = row(device.device_name, `${device.current ? "当前设备 · " : ""}${device.read_only ? "只读" : "管理"}`);
      if (!device.current) {
        const button = document.createElement("button");
        button.type = "button"; button.className = "secondary"; button.textContent = "撤销登录";
        button.addEventListener("click", async () => {
          if (!confirm(`撤销“${device.device_name}”的登录？`)) return;
          try { await request("/api/session/revoke", "POST", {device_id: device.id}); await refreshSessions(); }
          catch (error) { toast(error.message, true); }
        });
        line.append(button);
      }
      return line;
    });
    $("session-devices").replaceChildren(...devices);
  } catch (error) { toast(error.message, true); }
}

function number(value) {
  return Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN") : "—";
}

function row(label, value, className = "kv-row") {
  const box = document.createElement("div");
  box.className = className;
  const name = document.createElement("span");
  name.textContent = label;
  const val = document.createElement("strong");
  val.textContent = value;
  box.append(name, val);
  return box;
}

function renderStatus(data) {
  const processes = data.processes || {};
  const supervisor = Boolean(processes.supervisor?.running);
  const run = Boolean(processes.run?.running);
  const state = supervisor && run ? "运行中" : supervisor ? "启动中或部分异常" : "未运行";
  $("overall-state").textContent = state;
  $("state-light").className = "state-light " + (supervisor && run ? "running" : supervisor ? "" : "stopped");
  $("updated-at").textContent = data.updated ? `数据更新：${data.updated.replace("T", " ")}` : "状态时间未知";
  const daily = data.daily || {};
  const usage = data.ai_usage || {};
  $("stat-comments").textContent = number(daily.comments || 0);
  $("stat-shares").textContent = number(daily.shares || 0);
  $("stat-calls").textContent = number(usage.calls || 0);
  $("stat-tokens").textContent = number(usage.input_tokens || 0);
  $("stat-output-tokens").textContent = number(usage.output_tokens || 0);
  $("stat-cached-tokens").textContent = number(usage.cached_tokens || 0);
  $("stat-fan-thanks").textContent = number(daily.fan_thanks || 0);
  const dailyExtra = Object.entries(daily).filter(([key]) => !["comments", "shares", "fan_thanks"].includes(key));
  $("daily-details").replaceChildren(...dailyExtra.map(([key, value]) => row(`今日 · ${key}`, number(value))));
  const actions = Object.entries(data.actions || {});
  $("action-details").replaceChildren(...(actions.length ? actions.map(([key, value]) => row(`动作 · ${key}`, number(value))) : [row("动作记录", "今日暂无记录")]));
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  $("alerts").classList.toggle("hidden", alerts.length === 0);
  $("alerts").textContent = alerts.join("；");
  $("restart-note").classList.toggle("hidden", !data.pending_restart);
  $("last-operation").textContent = data.last_operation || "";
  const processNames = {supervisor:"守护进程", run:"主程序", "chat-run":"聊天进程", "health-run":"健康检查"};
  $("processes").replaceChildren(...Object.entries(processNames).map(([key, name]) => {
    const p = processes[key] || {};
    const item = row(name, p.running ? `运行中 · PID ${p.pid}` : "未运行", "process-item");
    item.lastChild.className = p.running ? "good" : "bad";
    return item;
  }));
  const rounds = Object.entries(data.rounds_24h || {});
  $("rounds").replaceChildren(...rounds.map(([mode, value]) => row(`${mode} 轮次`, `${number(value.rounds || 0)} 轮 · 成功 ${number(value.success || 0)} · 失败 ${number(value.failure || 0)}`)));
  const events = Object.entries(data.events_24h || {}).filter(([, value]) => Number(value) > 0);
  $("events").replaceChildren(...events.map(([key, value]) => row(key, number(value))));
  if (!rounds.length && !events.length) $("rounds").replaceChildren(row("记录", "过去 24 小时暂无轮次或事件"));
  const stale = data.stale_audio || {};
  const environment = [row("磁盘剩余", data.disk_free_gb == null ? "—" : `${data.disk_free_gb} GB`), row("媒体缓存", `${(Number(stale.cache_bytes || 0) / 2**30).toFixed(2)} GB`), row("待清理音频", `${(Number(stale.bytes || 0) / 2**30).toFixed(2)} GB · ${number(stale.files || 0)} 个`)];
  $("environment").replaceChildren(...environment);
}

const FEATURE_GROUPS = window.KOISHI_CATALOG || [];
const FEATURE_PAGES = new Set(["private", "comments", "greetings", "birthdays", "content", "station", "persona", "knowledge", "learning", "media", "recognition", "safety", "health"]);

function renderFeatures() {
  const query = $("feature-search").value.trim().toLowerCase();
  $("feature-count").textContent = `${features.length} 项 · ${FEATURE_GROUPS.length} 组`;
  const byPath = new Map(features.map(item => [item.path, item]));
  for (const page of FEATURE_PAGES) {
    const containers = [];
    for (const meta of FEATURE_GROUPS.filter(item => item.page === page)) {
      const items = meta.paths.map(path => byPath.get(path)).filter(Boolean);
      if (!items.length) continue;
      if (query && !`${meta.title} ${meta.summary} ${meta.detail} ${items.map(item => `${item.label} ${item.path}`).join(" ")}`.toLowerCase().includes(query)) continue;
      const group = document.createElement("details");
      group.className = "feature-group";
      group.open = Boolean(query);
      const heading = document.createElement("summary");
      const title = document.createElement("strong");
      title.textContent = meta.title;
      const count = document.createElement("span");
      count.textContent = `${items.length} 项`;
      heading.append(title, count);
      const intro = document.createElement("p");
      intro.className = "group-intro";
      intro.textContent = meta.summary;
      const detail = document.createElement("p");
      detail.className = "group-detail";
      detail.textContent = meta.detail;
      group.append(heading, intro, detail);
      for (const item of items) {
        const line = document.createElement("label");
        line.className = "feature-row";
        const info = document.createElement("span");
        const label = document.createElement("span");
        label.className = "feature-label";
        label.textContent = item.label;
        const path = document.createElement("span");
        path.className = "feature-path";
        path.textContent = item.path;
        info.append(label, path);
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = item.enabled;
        checkbox.disabled = readOnly;
        checkbox.setAttribute("aria-label", `${item.label} 开关`);
        checkbox.addEventListener("change", async () => {
          const intended = checkbox.checked;
          checkbox.disabled = true;
          try {
            const result = await request("/api/feature", "POST", {path:item.path, enabled:intended});
            item.enabled = intended;
            toast(result.message);
            $("restart-note").classList.remove("hidden");
          } catch (error) { checkbox.checked = !intended; toast(error.message, true); }
          finally { checkbox.disabled = false; }
        });
        line.append(info, checkbox);
        group.append(line);
      }
      containers.push(group);
    }
    if (!containers.length) containers.push(row("本页", query ? "没有匹配功能；可切换目录查看" : "暂无开关"));
    $(`features-${page}`).replaceChildren(...containers);
  }
}

function renderSettings(data) {
  const rowBox = $("dry-run-row");
  const info = document.createElement("div");
  info.className = "setting-info";
  const title = document.createElement("strong");
  title.textContent = "预演模式";
  const sub = document.createElement("small");
  sub.textContent = "开启后先预演，不实际执行机器人动作";
  info.append(title, sub);
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = Boolean(data.dry_run);
  toggle.disabled = readOnly;
  toggle.setAttribute("aria-label", "预演模式");
  toggle.addEventListener("change", async () => {
    const wanted = toggle.checked;
    toggle.disabled = true;
    try {
      const result = await request("/api/setting", "POST", {key:"dry_run", value:wanted});
      toast(result.message);
      $("restart-note").classList.remove("hidden");
    } catch (error) { toggle.checked = !wanted; toast(error.message, true); }
    finally { toggle.disabled = false; }
  });
  rowBox.replaceChildren(info, toggle);
  const limits = [];
  for (const [key, item] of Object.entries(data.limits || {})) {
    const line = document.createElement("div");
    line.className = "setting-row";
    const label = document.createElement("label");
    label.className = "setting-info";
    label.textContent = item.label;
    const input = document.createElement("input");
    input.type = "number"; input.min = String(item.min); input.max = String(item.max); input.step = "1";
    input.value = String(item.value); input.setAttribute("aria-label", item.label);
    input.disabled = readOnly;
    input.addEventListener("change", async () => {
      const value = Number(input.value);
      if (!Number.isInteger(value) || value < item.min || value > item.max) { input.value = String(item.value); toast(`请输入 ${item.min}～${item.max} 之间的整数`, true); return; }
      input.disabled = true;
      try { const result = await request("/api/setting", "POST", {key, value}); item.value = value; toast(result.message); $("restart-note").classList.remove("hidden"); }
      catch (error) { input.value = String(item.value); toast(error.message, true); }
      finally { input.disabled = false; }
    });
    line.append(label, input); limits.push(line);
  }
  $("limit-settings").replaceChildren(...limits);
}

async function refresh() {
  if (!csrf) return;
  $("refresh").disabled = true;
  try {
    const [status, featureData, settings] = await Promise.all([request("/api/status"), request("/api/features"), request("/api/settings")]);
    renderStatus(status);
    features = featureData.features || [];
    renderFeatures();
    renderSettings(settings);
  } catch (error) { toast(error.message, true); }
  finally { $("refresh").disabled = false; }
}

function switchTab(name) {
  activeTab = name;
  for (const button of document.querySelectorAll(".tabs button")) button.classList.toggle("active", button.dataset.tab === name);
  for (const section of document.querySelectorAll(".tab-content")) section.classList.toggle("hidden", section.id !== `tab-${name}`);
  $("feature-toolbar").classList.toggle("hidden", !FEATURE_PAGES.has(name));
  $("page-select").value = name;
  const page = PAGES[name] || PAGES.overview;
  $("hero-title").textContent = page.title;
  $("hero-subtitle").textContent = page.subtitle;
  $("hero-eyebrow").textContent = page.eyebrow;
  const stage = document.querySelector(".character-stage");
  stage.dataset.art = name;
  const selectedArt = name === "media" ? (mediaArt || page.art) : name === "persona" ? (personaArt || page.art) : page.art;
  for (const id of ["art-base", "art-hair", "art-sleeves", "art-skirt"]) $(id).src = selectedArt;
  stage.classList.remove("enter");
  void stage.offsetWidth;
  stage.classList.add("enter");
}

const PAGES = {
  overview: {title:"概况", subtitle:"看见今天发生的事", eyebrow:"KOISHI · CONTROL", art:"/koishi-dance.png"},
  private: {title:"私信", subtitle:"聊天与私信回复", eyebrow:"KOISHI · MESSAGE", art:"/koishi-interaction.png"},
  comments: {title:"评论", subtitle:"公开互动与 @ 回复", eyebrow:"KOISHI · TALK", art:"/koishi-welcome.png"},
  greetings: {title:"问候", subtitle:"早晚安与新粉", eyebrow:"KOISHI · HELLO", art:"/koishi-greetings.png"},
  birthdays: {title:"生日", subtitle:"提醒、故事和贺卡", eyebrow:"KOISHI · WISH", art:"/koishi-content.png"},
  content: {title:"创作", subtitle:"周期内容与素材", eyebrow:"KOISHI · CREATE", art:"/koishi-writing.png"},
  station: {title:"小站", subtitle:"小站内容和评论", eyebrow:"KOISHI · COMMUNITY", art:"/koishi-station.png"},
  persona: {title:"人格", subtitle:"恋恋的表达方式", eyebrow:"KOISHI · PERSONA", art:"/koishi-aristocrat.png"},
  knowledge: {title:"知识", subtitle:"东方资料与问答", eyebrow:"KOISHI · KNOW", art:"/koishi-learning.png"},
  learning: {title:"学习", subtitle:"长期积累与记忆", eyebrow:"KOISHI · LEARN", art:"/koishi-longlearning.png"},
  media: {title:"媒体", subtitle:"视频理解与下载", eyebrow:"KOISHI · MEDIA", art:"/koishi-swim-emerald.png"},
  recognition: {title:"识图", subtitle:"角色与样本识别", eyebrow:"KOISHI · VISION", art:"/koishi-media.png"},
  safety: {title:"安全", subtitle:"规则与内容检查", eyebrow:"KOISHI · GUARD", art:"/koishi-safety.png"},
  health: {title:"健康", subtitle:"运行巡检和心跳", eyebrow:"KOISHI · HEALTH", art:"/koishi-health.png"},
  settings: {title:"设置", subtitle:"调整节奏与限额", eyebrow:"KOISHI · TUNE", art:"/koishi-settings.png"},
  logs: {title:"日志", subtitle:"追踪运行痕迹", eyebrow:"KOISHI · HISTORY", art:"/koishi-logs.png"},
};
let mediaArt = "";
let personaArt = "";

document.querySelectorAll(".tabs button").forEach(button => button.addEventListener("click", () => switchTab(button.dataset.tab)));
$("page-select").addEventListener("change", event => switchTab(event.target.value));
document.querySelectorAll("[data-art-choice]").forEach(button => button.addEventListener("click", () => {
  mediaArt = button.dataset.artChoice;
  for (const item of document.querySelectorAll("[data-art-choice]")) item.classList.toggle("active", item === button);
  switchTab("media");
}));
document.querySelectorAll("[data-persona-art]").forEach(button => button.addEventListener("click", () => {
  personaArt = button.dataset.personaArt;
  for (const item of document.querySelectorAll("[data-persona-art]")) item.classList.toggle("active", item === button);
  switchTab("persona");
}));
$("feature-search").addEventListener("input", renderFeatures);
$("refresh").addEventListener("click", refresh);
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    const result = await request("/api/login", "POST", {code: $("pairing-code").value, device_name: $("device-name").value, read_only: $("login-read-only").checked});
    csrf = result.csrf;
    readOnly = Boolean(result.read_only);
    $("pairing-code").value = "";
    showDashboard();
    await refresh();
    await refreshSessions();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => {
  try { await request("/api/logout", "POST", {}); showLogin(); toast("已退出登录"); }
  catch (error) { toast(error.message, true); }
});
$("refresh-sessions").addEventListener("click", refreshSessions);
for (const [id, action, label] of [["start-bot","start","启动"],["restart-bot","restart","重启"],["stop-bot","stop","停止"]]) {
  $(id).addEventListener("click", async () => {
    if (!confirm(`确定${label}社交机器人吗？`)) return;
    const button = $(id); button.disabled = true;
    try { const result = await request("/api/robot", "POST", {action}); toast(result.message); await refresh(); }
    catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
}
async function loadLog(which) {
  try {
    const result = await request(`/api/logs?which=${encodeURIComponent(which)}`);
    $("log-content").textContent = (result.lines || []).join("\n") || "暂无日志。";
  } catch (error) { toast(error.message, true); }
}
$("log-supervisor").addEventListener("click", () => loadLog("supervisor"));
$("log-health").addEventListener("click", () => loadLog("health"));
setInterval(() => { if (!document.hidden && csrf) refresh(); }, 30000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
request("/api/session").then(result => {
  if (result.logged_in) { csrf = result.csrf; readOnly = Boolean(result.read_only); showDashboard(); refresh(); refreshSessions(); }
  else showLogin();
}).catch(error => { showLogin(); toast(error.message, true); });
