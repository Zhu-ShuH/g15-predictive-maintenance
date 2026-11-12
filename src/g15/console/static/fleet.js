import { createConsoleStore, deviceKey, deviceStatus, latestWindow, chronological,
  liveDevice, liveStatus, maintenanceAdvice, locationSelection, plotLayout, finite } from "./state.js";

const $ = (id) => document.getElementById(id);
const text = (id, value) => {
  const element = $(id), next = String(value ?? "");
  if (element && element.textContent !== next) element.textContent = next;
};
const number = (value, digits = 0) => finite(value)
  ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
const timeFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const timeText = (value) => {
  if (!value) return "—";
  const parsed = value instanceof Date ? value : new Date(value);
  return Number.isFinite(parsed.getTime()) ? timeFormat.format(parsed) : "—";
};
function node(tag, content = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = content;
  if (className) element.className = className;
  return element;
}
function svgNode(tag, attributes = {}, content) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (content !== undefined) element.textContent = content;
  return element;
}
export function fleetLink(path, device, view = null) {
  if (!device) return path;
  const query = new URLSearchParams({ device: device.device_id, run: device.run_id });
  if (view) query.set("view", view);
  return `${path}?${query}`;
}
export function fleetRows(state) {
  const rank = { red: 0, error: 1, yellow: 2, unknown: 3, healthy: 4 };
  return (Array.isArray(state.devices) ? state.devices : []).map((device) => {
    const key = deviceKey(device), preview = state.previews?.[key];
    return { device, key, latest: preview?.latest || null,
      status: deviceStatus(state, device, preview), live: liveDevice(state, device) };
  }).sort((a, b) => rank[a.status.level] - rank[b.status.level] ||
    (a.latest?.health_score ?? 101) - (b.latest?.health_score ?? 101) ||
    a.device.device_id.localeCompare(b.device.device_id));
}
export function fleetSummary(state) {
  const counts = { total: 0, red: 0, yellow: 0, healthy: 0, unknown: 0, error: 0 };
  const rows = fleetRows(state);
  counts.total = rows.length;
  for (const row of rows) counts[row.status.level] += 1;
  let liveTotals = null;
  if (state.live && Array.isArray(state.live.devices)) {
    const sum = (field) => state.live.devices.reduce((total, row) => total + row[field], 0);
    liveTotals = { captured: sum("captured_samples"), processed: sum("processed_samples"),
      windows: sum("windows_written"), queue: sum("queue_depth") };
  }
  return { counts, liveTotals, rows };
}
function updateLink(id, path, selected, view = null) {
  const element = $(id);
  if (element) element.href = fleetLink(path, selected, view);
}
function renderDistribution(summary) {
  const donut = $("fleet-donut"), list = $("fleet-distribution");
  if (!donut || !list) return;
  const entries = [
    ["red", "红色活动", "var(--fleet-red)"], ["yellow", "黄色活动", "var(--fleet-yellow)"],
    ["healthy", "无活动告警", "var(--fleet-healthy)"], ["unknown", "未知 / 过期", "var(--fleet-unknown)"],
    ["error", "读取失败", "var(--fleet-red)"],
  ];
  const total = summary.counts.total;
  const svg = svgNode("svg", { viewBox: "0 0 140 140", role: "img",
    "aria-label": `共 ${total} 台登记设备，健康状态按真实查询分类` });
  svg.append(svgNode("circle", { cx: 70, cy: 70, r: 48, fill: "none",
    stroke: "var(--fleet-line)", "stroke-width": 16 }));
  let offset = 0;
  for (const [key, _label, color] of entries) {
    const count = summary.counts[key], share = total ? count / total * 100 : 0;
    if (!share) continue;
    svg.append(svgNode("circle", { cx: 70, cy: 70, r: 48, fill: "none", stroke: color,
      "stroke-width": 16, pathLength: 100, "stroke-dasharray": `${share} ${100 - share}`,
      "stroke-dashoffset": -offset, transform: "rotate(-90 70 70)" }));
    offset += share;
  }
  svg.append(svgNode("text", { x: 70, y: 70, class: "donut-center" }, number(total)));
  svg.append(svgNode("text", { x: 70, y: 86, class: "donut-caption" }, "登记设备"));
  donut.replaceChildren(svg);
  list.replaceChildren(...entries.map(([key, label]) => {
    const row = node("div"), name = node("dt", label); name.dataset.level = key;
    const count = node("dd", number(summary.counts[key]));
    const percent = node("dd", total ? `${(summary.counts[key] / total * 100).toFixed(1)}%` : "—", "distribution-percent");
    row.append(name, count, percent); return row;
  }));
}
function renderTrend(state) {
  const container = $("fleet-trend");
  if (!container) return;
  const points = chronological(state.windows), selected = state.selected;
  text("fleet-selection", selected ? `${selected.device_id} · ${selected.run_id}` : "暂无选择设备");
  container.setAttribute("aria-busy", String(state.healthStatus === "loading"));
  if (!points.length) {
    container.replaceChildren(node("p", state.healthStatus === "loading" ? "正在读取真实健康窗口…" :
      state.healthStatus === "error" ? state.healthError : "所选运行暂无已提交健康窗口。", "fleet-empty"));
    text("fleet-trend-meta", "暂无可绘制窗口"); return;
  }
  const layout = plotLayout(800, 230), left = layout.left + 10, right = layout.right;
  const first = points[0].start_seq, last = points.at(-1).start_seq, span = Math.max(last - first, 1);
  const x = (row) => left + (row.start_seq - first) / span * (right - left);
  const y = (score) => layout.bottom - score / 100 * layout.plotHeight;
  const rate = points[0].sample_rate_hz;
  const seconds = finite(rate) && rate > 0 && points.every((row) => row.sample_rate_hz === rate);
  const svg = svgNode("svg", { viewBox: "0 0 800 230", role: "img",
    "aria-label": `${selected?.device_id || "设备"}的 ${points.length} 个真实健康窗口` });
  for (const tick of [0, 25, 50, 75, 100]) {
    svg.append(svgNode("line", { x1: left, x2: right, y1: y(tick), y2: y(tick), class: "trend-grid" }));
    svg.append(svgNode("text", { x: left - 9, y: y(tick) + 3, "text-anchor": "end", class: "trend-axis" }, tick));
  }
  const path = points.map((row, index) => `${index ? "L" : "M"}${x(row).toFixed(2)},${y(row.health_score).toFixed(2)}`).join(" ");
  svg.append(svgNode("path", { d: `${path}L${x(points.at(-1))},${layout.bottom}L${x(points[0])},${layout.bottom}Z`, class: "trend-area" }));
  svg.append(svgNode("path", { d: path, class: "trend-line" }));
  if (points.length === 1) svg.append(svgNode("circle", { cx: x(points[0]), cy: y(points[0].health_score), r: 4, class: "trend-point" }));
  for (const [row, anchor] of [[points[0], "start"], [points.at(-1), "end"]]) {
    const value = seconds ? `${number(row.start_seq / rate, 1)} s` : `seq ${row.start_seq}`;
    svg.append(svgNode("text", { x: x(row), y: 224, "text-anchor": anchor, class: "trend-axis" }, value));
  }
  container.replaceChildren(svg);
  const scores = points.map((row) => row.health_score);
  text("fleet-trend-meta", `已加载 ${points.length} 窗 · 健康分 ${number(Math.min(...scores), 1)}–${number(Math.max(...scores), 1)}${state.windowCursor ? " · 部分范围" : " · 已提交全部窗口"}`);
}
function renderScreen(state, summary, selectDevice, cache) {
  const matrix = $("fleet-matrix");
  if (!matrix) return;
  const matrixSignature = JSON.stringify([summary.rows.map((row) => [row.key,
    row.latest?.start_seq, row.latest?.health_score, row.status.level, row.status.label]),
  state.selected ? deviceKey(state.selected) : null]);
  if (cache.matrix !== matrixSignature) {
    const focused = document.activeElement?.closest("[data-device-key]")?.dataset.deviceKey;
    const existing = new Map([...matrix.querySelectorAll("[data-device-key]")]
      .map((button) => [button.dataset.deviceKey, button]));
    matrix.replaceChildren(...summary.rows.map((row) => {
      let button = existing.get(row.key);
      if (!button) {
        button = node("button", "", "matrix-device"); button.type = "button";
        button.dataset.deviceKey = row.key;
        button.append(node("strong"), node("small"));
        button.addEventListener("click", selectDevice);
      }
      button.dataset.level = row.status.level;
      button.setAttribute("aria-pressed", String(state.selected && deviceKey(state.selected) === row.key));
      button.setAttribute("aria-label", `${row.device.device_id}，${row.status.label}，最新健康分 ${row.latest ? number(row.latest.health_score, 1) : "未知"}`);
      button.title = row.device.run_id;
      button.firstChild.textContent = row.device.device_id;
      button.lastChild.textContent = row.latest ? number(row.latest.health_score, 1) : "—";
      return button;
    }));
    if (!summary.rows.length) matrix.append(node("p", "设备目录暂无记录。", "fleet-empty"));
    if (focused) matrix.querySelectorAll("[data-device-key]").forEach((button) => {
      if (button.dataset.deviceKey === focused) button.focus({ preventScroll: true });
    });
    cache.matrix = matrixSignature;
  }
  const advice = maintenanceAdvice(state), adviceContainer = $("fleet-advice");
  const cards = advice.items.slice(0, 2).map((item) => {
    const card = node("article", "", "advice-compact"); card.dataset.level = item.level;
    const heading = node("div", "", "advice-compact-heading");
    heading.append(node("span", `${item.ruleId} · seq ${item.startSeq}`), node("strong", item.urgency));
    card.append(heading, node("p", item.basis));
    card.append(node("ul", "", "")); const actions = card.querySelector("ul");
    actions.append(...item.actions.slice(0, 2).map((action) => node("li", action)));
    return card;
  });
  if (advice.missingActiveRules.length || advice.partial) {
    cards.unshift(node("p", "当前建议只覆盖已加载告警；来源缺失或更早记录未加载时不会补造。", "fleet-note"));
  }
  adviceContainer.replaceChildren(...(cards.length ? cards : [node("p", advice.message, "fleet-empty")]));
  const liveList = $("fleet-live-list");
  if (!state.live) {
    liveList.replaceChildren(node("p", state.liveMode === "legacy" ? "当前为离线运行目录，没有持续采集状态。" :
      state.liveError || "正在确认持续采集状态。", "fleet-empty"));
  } else {
    liveList.replaceChildren(...summary.rows.map((row) => {
      const live = row.live;
      if (!live) return null;
      const item = node("div", "", "acquisition-row");
      item.append(node("strong", row.device.device_id), node("span", row.status.label));
      item.firstChild.dataset.level = row.status.level;
      item.append(node("small", `${number(live.captured_samples)} 样本 · ${number(live.windows_written)} 窗 · 积压 ${number(live.queue_depth)} · 提交 ${timeText(live.last_commit_at_utc)}`));
      return item;
    }).filter(Boolean));
  }
  text("live-status-detail", state.liveMode === "available" || state.liveMode === "error" ?
    liveStatus(state).label : "非持续运行");
  text("live-scope", state.live ? `${state.live.devices.length} 台 · ${number(state.live.sample_rate_hz)} Hz · 状态更新 ${timeText(state.live.updated_at_utc)}` :
    "未检测到持续会话；仅展示离线运行结果。");
}
function renderOperations(state, summary, search, cache) {
  const body = $("fleet-device-table");
  if (!body) return;
  const needle = search.trim().toLocaleLowerCase();
  const rows = summary.rows.filter((row) => [row.device.device_id, row.device.run_id]
    .some((value) => value.toLocaleLowerCase().includes(needle)));
  const signature = JSON.stringify([needle, rows.map((row) => [row.key, row.latest?.health_score,
    row.status.level, row.status.label, row.live?.captured_samples, row.live?.windows_written,
    row.live?.last_commit_at_utc])]);
  if (cache.table !== signature) {
    body.replaceChildren(...rows.map((row) => {
      const line = node("tr"), identity = node("td");
      identity.append(node("strong", row.device.device_id), node("small", row.device.run_id, "mono"));
      const score = node("td", row.latest ? number(row.latest.health_score, 2) : "—", "mono");
      const status = node("td"), badge = node("span", row.status.label, "table-status");
      badge.dataset.level = row.status.level; status.append(badge);
      const counters = node("td", row.live ? `${number(row.live.captured_samples)} / ${number(row.live.windows_written)}` : "—", "mono");
      const committed = node("td", row.live ? timeText(row.live.last_commit_at_utc) : "—");
      const actions = node("td"), links = node("div", "", "table-actions");
      const detail = node("a", "健康"); detail.href = fleetLink("/", row.device, "health");
      const diagnosis = node("a", "诊断"); diagnosis.href = fleetLink("/diagnosis", row.device);
      const advice = node("a", "维护建议"); advice.href = fleetLink("/", row.device, "maintenance");
      links.append(detail, diagnosis, advice); actions.append(links);
      line.append(identity, score, status, counters, committed, actions); return line;
    }));
    cache.table = signature;
  }
  $("fleet-table-empty").hidden = rows.length > 0;
  text("fleet-matrix-caption", `异常优先 · 当前匹配 ${rows.length} / ${summary.counts.total} 台`);
}
export function startFleetPage() {
  const mode = document.body.dataset.fleetMode;
  if (!["screen", "operations"].includes(mode)) return null;
  const store = createConsoleStore({ selection: locationSelection(location.search) });
  let search = "";
  const cache = { matrix: "", table: "" };
  function selectDevice(event) {
    const key = event.currentTarget.dataset.deviceKey;
    if (!key) return;
    const device = store.getState().devices.find((row) => deviceKey(row) === key);
    if (!device) return;
    void store.selectDevice(key);
    const query = new URLSearchParams({ device: device.device_id, run: device.run_id });
    history.pushState({}, "", `${location.pathname}?${query}`);
  }
  function render(state) {
    const summary = fleetSummary(state), selected = state.selected;
    for (const key of ["red", "yellow", "healthy", "unknown", "error"]) text(`kpi-${key}`, summary.counts[key]);
    text("kpi-total", summary.counts.total);
    text("fleet-total-caption", `${summary.counts.total} 台登记设备`);
    text("fleet-matrix-caption", `${summary.counts.total} 台 · 异常优先`);
    text("fleet-updated", state.lastRead ? `最近读取（北京时间）${timeText(state.lastRead)}` : "尚未成功读取");
    for (const [id, path, view] of [["nav-console", "/", "health"], ["nav-operations", "/operations", null],
      ["nav-screen", "/screen", null], ["nav-diagnosis", "/diagnosis", null],
      ["trend-console-link", "/", "health"], ["advice-console-link", "/", "maintenance"]]) {
      updateLink(id, path, selected, view);
    }
    const liveState = state.liveMode === "available" || state.liveMode === "error" ? liveStatus(state) : null;
    const status = liveState || (state.catalogStatus === "error" || summary.counts.error ? { level: "error", label: "部分查询失败" } :
      summary.counts.red ? { level: "red", label: `${summary.counts.red} 台红色活动告警` } :
      summary.counts.yellow ? { level: "yellow", label: `${summary.counts.yellow} 台黄色活动告警` } :
      summary.counts.unknown || state.healthStatus !== "ready" ? { level: "unknown", label: "状态读取中或未知" } :
      { level: "healthy", label: "当前无活动告警" });
    text("fleet-status", status.label); $("fleet-status").dataset.level = status.level;
    const message = state.catalogError || state.healthError || state.liveError ||
      (state.catalogStatus === "loading" ? "正在读取设备目录…" : "");
    text("fleet-message", message); $("fleet-message").hidden = !message;
    $("fleet-refresh").disabled = state.catalogStatus === "loading" || state.healthStatus === "loading";
    if (summary.liveTotals) {
      text("live-captured", number(summary.liveTotals.captured)); text("live-processed", number(summary.liveTotals.processed));
      text("live-windows", number(summary.liveTotals.windows)); text("live-queue", number(summary.liveTotals.queue));
    } else {
      for (const id of ["live-captured", "live-processed", "live-windows", "live-queue"]) text(id, "—");
    }
    renderDistribution(summary); renderTrend(state);
    if (mode === "screen") renderScreen(state, summary, selectDevice, cache);
    else renderOperations(state, summary, search, cache);
  }
  store.subscribe(render);
  $("fleet-refresh").addEventListener("click", async () => {
    if (store.getState().selected) await store.refresh();
    await store.init();
  });
  const searchInput = $("fleet-search");
  if (searchInput) searchInput.addEventListener("input", (event) => {
    search = event.target.value; renderOperations(store.getState(), fleetSummary(store.getState()), search, cache);
  });
  function restoreSelection() {
    const selection = locationSelection(location.search), state = store.getState();
    const device = state.devices.find((row) => row.device_id === selection.device &&
      (!selection.run || row.run_id === selection.run));
    if (device && (!state.selected || deviceKey(device) !== deviceKey(state.selected))) {
      void store.selectDevice(deviceKey(device));
    }
  }
  window.addEventListener("popstate", restoreSelection);
  const clock = $("fleet-clock");
  const updateClock = () => { if (clock) { clock.textContent = timeText(new Date()); clock.dateTime = new Date().toISOString(); } };
  updateClock(); const clockTimer = clock ? setInterval(updateClock, 1000) : null;
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    if (clockTimer !== null) clearInterval(clockTimer);
    window.removeEventListener("popstate", restoreSelection); store.destroy();
  });
  void store.init();
  return store;
}
if (typeof document !== "undefined") startFleetPage();
