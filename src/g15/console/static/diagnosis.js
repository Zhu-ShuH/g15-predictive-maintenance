import { createConsoleStore, deviceKey, latestWindow, selectedDeviceStatus,
  maintenanceAdvice, locationSelection, finite } from "./state.js";
import { fleetLink, fleetSummary, mountColumnBalance } from "./fleet.js";

const $ = (id) => document.getElementById(id);
const text = (id, value) => {
  const element = $(id), next = String(value ?? "");
  if (element && element.textContent !== next) element.textContent = next;
};
const number = (value, digits = 0) => finite(value)
  ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
const timeFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai",
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const timeText = (value) => {
  if (!value) return "—";
  const parsed = value instanceof Date ? value : new Date(value);
  return Number.isFinite(parsed.getTime()) ? timeFormat.format(parsed) : "—";
};
const DIMENSIONS = Object.freeze([
  { name: "level_offset", label: "电平偏移", members: ["mean"] },
  { name: "vibration_strength", label: "振动强度", members: ["rms"] },
  { name: "variability", label: "波动程度", members: ["std"] },
  { name: "impact", label: "冲击特征", members: ["ptp", "kurtosis_excess", "crest_factor"] },
  { name: "spectral", label: "频谱变化", members: ["dominant_freq_hz", "zero_crossing_rate"] },
]);
function node(tag, content = "", className = "") {
  const element = document.createElement(tag); element.textContent = content;
  if (className) element.className = className; return element;
}
function svgNode(tag, attributes = {}, content) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (content !== undefined) element.textContent = content; return element;
}
export function diagnosisModel(state) {
  const latest = latestWindow(state.windows), source = maintenanceAdvice(state).items[0] || null;
  const raw = latest?.risk_dimensions;
  const dimensions = DIMENSIONS.map((definition) => {
    const value = raw && typeof raw === "object" ? raw[definition.name] : null;
    return { ...definition, risk: finite(value) && value >= 0 && value <= 1 ? value : null };
  });
  return { latest, status: selectedDeviceStatus(state), source, dimensions,
    alarms: Array.isArray(state.alarms) ? state.alarms.slice(0, 5) : [] };
}
function updateLink(id, path, selected, view = null) {
  const element = $(id); if (element) element.href = fleetLink(path, selected, view);
}
function renderRadar(dimensions, latest) {
  const container = $("diagnosis-radar");
  if (!latest || latest.risk_dimensions === null || latest.risk_dimensions === undefined) {
    container.replaceChildren(node("p", "该窗口的旧版数据没有保存五维风险，无法重建。", "fleet-empty")); return;
  }
  if (dimensions.some((row) => row.risk === null)) {
    container.replaceChildren(node("p", "存在未知风险维度，不绘制完整五维雷达；请逐项查看右侧状态。", "fleet-empty")); return;
  }
  const width = 360, height = 280, cx = 180, cy = 132, radius = 78, count = dimensions.length;
  const point = (index, scale = 1) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / count;
    return [cx + Math.cos(angle) * radius * scale, cy + Math.sin(angle) * radius * scale];
  };
  const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `最新已存窗口 seq ${latest.start_seq} 的五维风险分解` });
  for (const ring of [0.25, 0.5, 0.75, 1]) {
    const points = dimensions.map((_row, index) => point(index, ring).join(",")).join(" ");
    svg.append(svgNode("polygon", { points, class: "radar-grid" }));
  }
  dimensions.forEach((row, index) => {
    const [x, y] = point(index), [lx, ly] = point(index, 1.2);
    svg.append(svgNode("line", { x1: cx, y1: cy, x2: x, y2: y, class: "radar-axis" }));
    svg.append(svgNode("text", { x: lx, y: ly, class: "radar-label",
      "text-anchor": lx < cx - 5 ? "end" : lx > cx + 5 ? "start" : "middle" },
    `${row.label} ${Math.round(row.risk * 100)}%`));
  });
  svg.append(svgNode("polygon", { points: dimensions.map((row, index) => point(index, row.risk).join(",")).join(" "), class: "radar-value" }));
  container.replaceChildren(svg);
  text("diagnosis-radar-scope", `最新已存窗口 seq ${latest.start_seq} · 五维未加权风险；不参与健康总分重算。`);
}
function renderMeasurements(model) {
  const latest = model.latest;
  text("diagnosis-source-meta", latest?.risk_dimensions ?
    `最新已存窗口 seq ${latest.start_seq} · 信封版本 ${latest.features_version}；五个风险分组只用于解释，不改变健康总分。` :
    "该窗口没有可用五维字段；旧版数据不会按当前规则重算或伪造。");
  $("diagnosis-measurements").replaceChildren(...model.dimensions.map((row) => {
    const card = node("div", "", "measurement");
    const heading = node("div", "", "measurement-heading");
    heading.append(node("span", `${row.label} · ${row.name}`),
      node("span", row.risk === null ? "风险未知" : `风险 ${number(row.risk * 100, 1)}%`));
    const detail = node("p", `包含 ${row.members.join(" / ")}${row.members.length > 1 ? "；取组内最大风险" : ""}`, "measurement-detail");
    const progress = node("progress", "", "measurement-track"); progress.max = 1;
    if (row.risk !== null) progress.value = row.risk;
    progress.setAttribute("aria-label", `${row.label} ${row.risk === null ? "风险未知" : `风险 ${number(row.risk * 100, 1)}%`}`);
    card.append(heading, detail, progress); return card;
  }));
}
function renderEvidence(state, model) {
  const list = $("diagnosis-evidence-list");
  list.replaceChildren(...model.alarms.map((alarm) => {
    const item = node("li", "", "evidence-item");
    if (model.source && alarm.rule_id === model.source.ruleId && alarm.start_seq === model.source.startSeq) {
      item.dataset.source = "true";
    }
    const content = node("div"), heading = node("div", "", "evidence-heading");
    const level = node("span", alarm.level === "red" ? "红色" : alarm.level === "yellow" ? "黄色" : "未知", "evidence-level");
    level.dataset.level = alarm.level;
    heading.append(level, node("strong", `${alarm.rule_id} · seq ${alarm.start_seq}`),
      node("time", timeText(alarm.created_at), "evidence-time"));
    content.append(heading, node("p", alarm.reason || "来源未提供原因", "evidence-reason")); item.append(content);
    return item;
  }));
  $("diagnosis-evidence-empty").hidden = model.alarms.length > 0;
  text("diagnosis-evidence-count", `已加载 ${state.alarms.length} 条 · 显示最近 ${model.alarms.length} 条${state.alarmCursor ? " · 仍有更早记录" : ""}`);
}
export function startDiagnosisPage() {
  if (document.body.dataset.fleetMode !== "diagnosis") return null;
  const store = createConsoleStore({ selection: locationSelection(location.search) });
  let catalogSignature = "";
  function selectKey(key) {
    const device = store.getState().devices.find((row) => deviceKey(row) === key);
    if (!device) return;
    void store.selectDevice(key);
    history.pushState({}, "", fleetLink("/diagnosis", device));
  }
  function render(state) {
    const model = diagnosisModel(state), selected = state.selected, latest = model.latest;
    const signature = JSON.stringify(state.devices.map((row) => deviceKey(row)));
    const select = $("diagnosis-device-select");
    if (catalogSignature !== signature) {
      select.replaceChildren(...state.devices.map((device) => {
        const option = node("option", device.device_id); option.value = deviceKey(device); return option;
      }));
      if (!state.devices.length) select.append(node("option", "设备目录为空"));
      catalogSignature = signature;
    }
    select.disabled = !state.devices.length || state.healthStatus === "loading";
    if (selected) select.value = deviceKey(selected);
    text("diagnosis-run", `运行标识：${selected?.run_id || "—"}`);
    const recommended = fleetSummary(state).rows.filter((row) => ["red", "yellow"].includes(row.status.level)).slice(0, 2);
    $("diagnosis-recommended").replaceChildren(...(recommended.length ? recommended.map((row) => {
      const button = node("button", `${row.device.device_id}（${row.status.level === "red" ? "红" : "黄"}）`);
      button.type = "button"; button.addEventListener("click", () => selectKey(row.key)); return button;
    }) : [node("span", "当前无红/黄设备")]));
    text("diagnosis-device", selected ? `${selected.device_id} · ${selected.run_id}` : "暂无选择设备");
    text("diagnosis-score", latest ? number(latest.health_score, 2) : "—");
    text("diagnosis-status", model.status.label); $("diagnosis-status").dataset.level = model.status.level;
    $("diagnosis-score").dataset.level = model.status.level;
    text("diagnosis-rule", latest ? `${latest.scoring_version} · ${latest.rule_id}` : "—");
    text("diagnosis-window", latest ? `[${latest.start_seq}, ${latest.end_seq_exclusive}) · ${latest.sample_rate_hz} Hz` : "—");
    text("fleet-status", model.status.label); $("fleet-status").dataset.level = model.status.level;
    text("fleet-updated", state.lastRead ? `最近读取（北京时间）${timeText(state.lastRead)}` : "尚未成功读取");
    for (const [id, path, view] of [["nav-console", "/", "health"], ["nav-operations", "/operations", null],
      ["nav-screen", "/screen", null], ["diagnosis-report-link", "/", "reports"], ["diagnosis-advice-link", "/", "maintenance"]]) {
      updateLink(id, path, selected, view);
    }
    const message = state.catalogError || state.healthError || (state.catalogStatus === "loading" ? "正在读取设备目录…" : "");
    text("fleet-message", message); $("fleet-message").hidden = !message;
    $("fleet-refresh").disabled = state.catalogStatus === "loading" || state.healthStatus === "loading";
    $("diagnosis-read").disabled = !selected || state.healthStatus === "loading";
    renderRadar(model.dimensions, latest); renderMeasurements(model); renderEvidence(state, model);
  }
  $("diagnosis-device-select").addEventListener("change", (event) => selectKey(event.target.value));
  $("diagnosis-read").addEventListener("click", () => { void store.refresh(); });
  $("fleet-refresh").addEventListener("click", () => { void store.refresh(); });
  window.addEventListener("popstate", () => {
    const selection = locationSelection(location.search), state = store.getState();
    const device = state.devices.find((row) => row.device_id === selection.device &&
      (!selection.run || row.run_id === selection.run));
    if (device && (!state.selected || deviceKey(device) !== deviceKey(state.selected))) void store.selectDevice(deviceKey(device));
  });
  const stopColumnBalance = mountColumnBalance(".diagnosis-layout", ".diagnosis-left", ".diagnosis-right");
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) { stopColumnBalance(); store.destroy(); }
  });
  store.subscribe(render); void store.init(); return store;
}
if (typeof document !== "undefined") startDiagnosisPage();
