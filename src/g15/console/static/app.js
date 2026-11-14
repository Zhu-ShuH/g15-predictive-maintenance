import { createConsoleStore, deviceKey, latestWindow, chronological, activeStatus,
  reportVerdict, detectionText, filterAlarms, locationSelection, apiUrl, finite, plotLayout, liveDevice, liveStatus, deviceStatus, selectedDeviceStatus, VIEWS, maintenanceAdvice } from "./state.js";
import { mountNotifications } from "./notifications.js";

const $ = (id) => document.getElementById(id);
const text = (id, value) => {
  const element = $(id), next = String(value ?? "");
  if (element && element.textContent !== next) element.textContent = next;
};
const number = (value, digits = 0) => finite(value)
  ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
const scenarios = { healthy: "正常工况", degradation: "缓慢退化", sudden_fault: "突发故障" };
const scenario = (device) => scenarios[device?.scenario] || device?.scenario || "场景未标明";
const timeFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai",
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
  minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const timeText = (value) => {
  if (value === null || value === undefined || value === "") return "未知";
  const parsed = value instanceof Date ? value : new Date(value);
  return Number.isFinite(parsed.getTime()) ? timeFormat.format(parsed) : "未知";
};
function node(tag, content = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = content;
  if (className) element.className = className;
  return element;
}
function badge(level, label) {
  const element = node("span", label, "status-badge");
  element.dataset.level = level; return element;
}
function levelLabel(level) { return level === "red" ? "红色告警" : level === "yellow" ? "黄色告警" : "级别未知"; }
function svgNode(tag, attributes = {}, content) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (content !== undefined) element.textContent = content;
  return element;
}
function positionText(row, rate) {
  return finite(rate) && rate > 0 ? `${number(row.start_seq / rate, 2)} s · seq ${row.start_seq}`
    : `seq ${row.start_seq}`;
}

export function startConsole() {
  const store = createConsoleStore({ selection: locationSelection(location.search) });
  const notifications = mountNotifications({ onSelect: (device) => { void store.selectDevice(deviceKey(device)); } });
  let previousLocation = null, restoring = false, chartPoints = [];
  let chartLayout = plotLayout(280, 180), lastChartSize = "", resizeFrame = 0;
  let revealedRosterKey = null, lastRosterSize = "", renderedState = null;
  function syncLocation(state) {
    if (!state.selected) return;
    const next = `${deviceKey(state.selected)}|${state.view}`;
    if (next === previousLocation) return;
    const query = new URLSearchParams(location.search);
    query.set("device", state.selected.device_id); query.set("run", state.selected.run_id);
    query.set("view", state.view);
    const url = `${location.pathname}?${query}`;
    history[previousLocation === null || restoring ? "replaceState" : "pushState"]({}, "", url);
    previousLocation = next;
  }
  function ensureSelectedInRoster(force = false) {
    const container = $("device-list");
    const overflow = container.scrollWidth > container.clientWidth;
    const verticalOverflow = container.scrollHeight > container.clientHeight;
    const selected = container.querySelector('[data-device-key][data-selected="true"]');
    if (!selected) { revealedRosterKey = null; return; }
    const key = selected.dataset.deviceKey;
    // Selection/resize reveals the current item. Ordinary chart inspection or
    // data re-rendering must not undo the user's deliberate horizontal pan.
    const reveal = force || key !== revealedRosterKey;
    revealedRosterKey = key;
    if ((!overflow && !verticalOverflow) || !reveal || container.clientWidth <= 0) return;
    const bounds = container.getBoundingClientRect(), item = selected.getBoundingClientRect();
    const left = bounds.left + container.clientLeft, right = left + container.clientWidth;
    const delta = item.left < left ? item.left - left : item.right > right ? item.right - right : 0;
    if (delta) container.scrollLeft = Math.max(0, Math.min(
      container.scrollWidth - container.clientWidth, container.scrollLeft + delta
    ));
    if (verticalOverflow) {
      const top = bounds.top + container.clientTop, bottom = top + container.clientHeight;
      const verticalDelta = item.top < top ? item.top - top : item.bottom > bottom ? item.bottom - bottom : 0;
      if (verticalDelta) container.scrollTop = Math.max(0, Math.min(
        container.scrollHeight - container.clientHeight, container.scrollTop + verticalDelta));
    }
  }
  function renderDevices(state) {
    const container = $("device-list");
    const focused = document.activeElement?.closest("[data-device-key]")?.dataset.deviceKey;
    const search = state.deviceSearch.trim().toLocaleLowerCase();
    const devices = state.devices.filter((device) =>
      [device.device_id, device.run_id, device.scenario, scenario(device), device.label]
        .some((value) => String(value || "").toLocaleLowerCase().includes(search)));
    text("catalog-count", state.catalogStatus === "loading" ? "正在读取" : `${devices.length} / ${state.devices.length} 台设备`);
    const rows = devices.map((device) => {
      const key = deviceKey(device), preview = state.previews[key];
      const selected = state.selected && deviceKey(state.selected) === key;
      const button = node("button", "", "device-row");
      button.type = "button"; button.dataset.deviceKey = key;
      button.dataset.selected = String(Boolean(selected));
      button.setAttribute("aria-pressed", String(Boolean(selected)));
      const main = node("span", "", "device-main");
      main.append(node("strong", device.device_id, "device-label"), node("span", scenario(device), "muted"));
      const score = node("span", preview?.latest ? number(preview.latest.health_score, 1) : "—", "device-score");
      const scoreLabel = preview?.latest ? `最新窗口健康分：${number(preview.latest.health_score, 2)} / 100` : "最新窗口健康分尚不可用";
      score.title = scoreLabel; score.setAttribute("aria-label", scoreLabel);
      const status = deviceStatus(state, device);
      const detail = node("span", "", "device-state"); detail.append(badge(status.level, status.label));
      button.append(main, score, detail);
      const acquisition = liveDevice(state, device);
      if (acquisition) {
        button.append(node("span", `${number(acquisition.captured_samples)} 样本 · ${number(acquisition.windows_written)} 窗`, "device-live-count"));
        button.append(node("small", acquisition.error ? "采集异常" : acquisition.queue_depth > 0
          ? `积压 ${number(acquisition.queue_depth)} 个批次` : `最近提交 ${timeText(acquisition.last_commit_at_utc)}`, "device-live-commit"));
      }
      button.addEventListener("click", () => { void store.selectDevice(key); });
      const item = node("li"); item.append(button); return item;
    });
    container.replaceChildren(...rows);
    if (!rows.length) container.append(node("li", state.devices.length ? "没有匹配的设备" :
      state.catalogStatus === "loading" ? "正在读取设备目录…" :
      state.catalogStatus === "error" ? state.catalogError : "设备目录暂无记录。", "empty-state"));
    if (focused) [...container.querySelectorAll("[data-device-key]")].find((item) =>
      item.dataset.deviceKey === focused)?.focus({ preventScroll: true });
    ensureSelectedInRoster();
  }
  function renderChart(state) {
    const container = $("health-chart");
    if ($("health-view").hidden) return;
    const width = container.clientWidth, height = container.clientHeight;
    if (width <= 0 || height <= 0) return;
    chartLayout = plotLayout(width, height);
    lastChartSize = `${width}:${height}`;
    const layout = chartLayout, points = chronological(state.windows);
    chartPoints = points;
    container.setAttribute("aria-busy", String(state.healthStatus === "loading" || state.windowLoading || state.fullLoading));
    const slider = $("health-window-slider");
    slider.disabled = !points.length;
    slider.min = "0"; slider.max = String(Math.max(0, points.length - 1));
    const chosen = points.find((row) => row.start_seq === state.inspectedSeq) || points.at(-1);
    const chosenIndex = chosen ? points.indexOf(chosen) : 0;
    slider.value = String(chosenIndex);
    if (!points.length) {
      $("chart-legend").hidden = true;
      container.replaceChildren(node("p", !state.selected ? "选择设备后读取真实窗口。" : state.healthStatus === "loading" ? "正在读取真实健康窗口…" :
        state.healthStatus === "error" ? state.healthError : "当前运行暂无已提交的健康窗口。", "empty-state"));
      text("chart-range", "—"); text("chart-inspector", "暂无窗口");
      return;
    }
    const rate = points[0].sample_rate_hz;
    const seconds = finite(rate) && rate > 0 && points.every((row) => row.sample_rate_hz === rate);
    const rawX = (row) => seconds ? row.start_seq / rate : row.start_seq;
    const first = rawX(points[0]), last = rawX(points.at(-1)), span = Math.max(last - first, 1);
    const x = (row) => layout.left + (rawX(row) - first) / span * layout.plotWidth;
    const y = (score) => layout.bottom - score / 100 * layout.plotHeight;
    const svg = svgNode("svg", { viewBox: `0 0 ${layout.width} ${layout.height}`, role: "img",
      "aria-labelledby": "plot-title plot-description", preserveAspectRatio: "xMidYMid meet" });
    svg.dataset.level = activeStatus(state.states).level;
    svg.append(svgNode("title", { id: "plot-title" }, `${state.selected?.device_id || ""}的真实健康窗口`));
    svg.append(svgNode("desc", { id: "plot-description" }, `已读取${points.length}个窗口，健康分范围${number(Math.min(...points.map((row) => row.health_score)), 2)}至${number(Math.max(...points.map((row) => row.health_score)), 2)}。`));
    for (const tick of [0, 50, 100]) {
      svg.append(svgNode("line", { x1: layout.left, x2: layout.right, y1: y(tick), y2: y(tick), class: "chart-grid" }));
      svg.append(svgNode("text", { x: layout.left - 10, y: y(tick) + 4, "text-anchor": "end", "font-size": 10, class: "chart-label" }, tick));
    }
    svg.append(svgNode("line", { x1: layout.left, x2: layout.right, y1: layout.bottom, y2: layout.bottom, class: "chart-axis" }));
    for (const [ratio, value] of [[0, first], [0.5, (first + last) / 2], [1, last]]) {
      svg.append(svgNode("text", { x: layout.left + layout.plotWidth * ratio, y: layout.height - 11,
        "text-anchor": ratio === 0 ? "start" : ratio === 1 ? "end" : "middle", "font-size": 10, class: "chart-label" },
      `${number(value, seconds ? 1 : 0)}${seconds ? " s" : ""}`));
    }
    const path = points.map((row, index) => `${index ? "L" : "M"}${x(row).toFixed(2)},${y(row.health_score).toFixed(2)}`).join(" ");
    svg.append(svgNode("path", { d: path, class: "chart-line", fill: "none" }));
    if (points.length === 1) svg.append(svgNode("circle", { cx: x(points[0]), cy: y(points[0].health_score), r: 4, class: "chart-point" }));
    for (const alarm of state.alarms) {
      const alarmX = seconds ? alarm.start_seq / rate : alarm.start_seq;
      if (alarmX < first || alarmX > last) continue;
      svg.append(svgNode("line", { x1: layout.left + (alarmX - first) / span * layout.plotWidth,
        x2: layout.left + (alarmX - first) / span * layout.plotWidth,
        y1: layout.top, y2: layout.bottom, class: "chart-alarm",
        "data-level": alarm.level }));
    }
    svg.append(svgNode("line", { x1: x(chosen), x2: x(chosen), y1: layout.top - 5, y2: layout.bottom + 4, class: "chart-focus" }));
    svg.append(svgNode("circle", { cx: x(chosen), cy: y(chosen.health_score), r: 5, class: "chart-point" }));
    container.replaceChildren(svg);
    text("chart-range", `已加载 ${points.length} 窗 · ${number(first, 1)}–${number(last, 1)} ${seconds ? "s" : "seq"} · ${state.windowCursor ? "部分范围" : "全部已提交窗口"}`);
    const visibleLevel = level => state.alarms.some(row => row.level === level && rawX(row) >= first && rawX(row) <= last);
    const hasRed = visibleLevel("red"), hasYellow = visibleLevel("yellow");
    $("chart-legend").hidden = !hasRed && !hasYellow;
    $("legend-red").hidden = !hasRed; $("legend-yellow").hidden = !hasYellow;
    const inspector = `选中窗口 [${chosen.start_seq}, ${chosen.end_seq_exclusive}) · 健康分 ${number(chosen.health_score, 2)} · ${positionText(chosen, seconds ? rate : null)}`;
    text("chart-inspector", inspector); slider.setAttribute("aria-valuetext", inspector);
  }
  function labelledAlarmCell(label, value, valueClass = "") {
    const cell = node("td");
    const caption = node("span", label, "mobile-cell-label");
    caption.setAttribute("aria-hidden", "true");
    cell.append(caption, node("span", value, `cell-value ${valueClass}`.trim()));
    return cell;
  }
  function renderAlarms(state) {
    const filtered = filterAlarms(state.alarms, state.alarmLevel, state.alarmSearch);
    const rate = latestWindow(state.windows)?.sample_rate_hz;
    text("alarm-count", `${filtered.length} 条匹配 / 已加载 ${state.alarms.length} 条${state.alarmCursor ? "（仍有更早记录）" : ""}`);
    $("alarm-table-body").replaceChildren(...filtered.map((alarm) => {
      const row = node("tr"); const level = node("td"); level.append(badge(alarm.level, levelLabel(alarm.level)));
      const reason = node("td"), details = node("details", "", "alarm-reason");
      details.append(node("summary", "查看原因"), node("p", alarm.reason ?? "来源未提供原因"));
      reason.append(details);
      row.append(level,
        labelledAlarmCell("仿真位置", positionText(alarm, rate), "mono"),
        labelledAlarmCell("规则", alarm.rule_id || "未知", "mono"),
        labelledAlarmCell("记录时间（北京时间）", timeText(alarm.created_at)), reason);
      return row;
    }));
    $("alarm-empty").hidden = filtered.length > 0;
    text("alarm-empty", state.healthStatus === "loading" ? "正在读取告警…" :
      state.healthStatus === "error" ? "暂时无法读取告警。请刷新重试。" :
      state.alarms.length ? "没有符合筛选的已加载告警" : "该运行暂无已读取的告警记录");
    const recent = state.alarms.slice(0, 3).map((alarm) => {
      const item = node("article", "", "recent-alarm");
      item.append(badge(alarm.level, levelLabel(alarm.level)), node("span", positionText(alarm, rate), "mono"),
        node("p", alarm.reason || "来源未提供原因", "muted"));
      return item;
    });
    $("recent-alarms").replaceChildren(...(recent.length ? recent : [node("p",
      state.healthStatus === "loading" ? "正在读取告警…" : state.healthStatus === "error" ? "告警暂不可读取。" :
        "暂无已读取的历史告警", "empty-state")]));
    const button = $("load-more-alarms");
    button.disabled = !state.alarmCursor || state.alarmLoading || state.healthStatus !== "ready";
    button.textContent = state.alarmLoading ? "正在读取…" : state.alarmCursor ? "加载更早告警" : "已加载全部告警";
  }
  function renderMaintenance(state) {
    const advice = maintenanceAdvice(state);
    const activeCount = advice.items.filter((item) => item.activity === "active").length;
    text("maintenance-summary", advice.status === "ready"
      ? `${advice.items.length} 条建议 · ${activeCount} 条对应当前活动告警`
      : "等待可信的告警数据");
    const warnings = [];
    if (advice.missingActiveRules.length) {
      warnings.push(`以下活动规则尚未加载对应级别的来源告警：${advice.missingActiveRules.join("、")}。`);
    }
    if (advice.partial) warnings.push("仍有更早告警未加载。");
    text("maintenance-warning", warnings.join(""));
    $("maintenance-warning").hidden = !warnings.length;
    text("maintenance-scope", `已加载 ${advice.loadedAlarmCount} 条告警`);
    const rate = latestWindow(state.windows)?.sample_rate_hz;
    const cards = advice.items.map((item) => {
      const card = node("article", "", "maintenance-card");
      card.dataset.level = item.level;
      const heading = node("div", "", "maintenance-card-heading");
      const labels = node("div", "", "maintenance-labels");
      labels.append(badge(item.level, levelLabel(item.level)),
        node("span", item.activity === "active" ? "当前活动告警" : item.activity === "inactive" ? "当前无活动告警" : "活动状态未知", "maintenance-activity"));
      heading.append(labels, node("strong", item.urgency, "maintenance-urgency"));
      const source = node("p", `${item.ruleId} · ${positionText({ start_seq: item.startSeq }, rate)}`, "maintenance-source mono");
      const basis = node("p", item.basis, "maintenance-basis");
      const drivers = node("p", item.drivers.length ? `扣分特征：${item.drivers.join("、")}` : "扣分特征未知", "maintenance-drivers");
      const actions = node("ul", "", "maintenance-actions");
      actions.append(...item.actions.map((action) => node("li", action)));
      const footer = node("div", "", "maintenance-card-footer");
      const sourceButton = node("button", "查看来源告警", "text-button");
      sourceButton.type = "button";
      sourceButton.addEventListener("click", () => {
        $("alarm-level-filter").value = "all"; $("alarm-search").value = item.ruleId;
        store.setAlarmFilter("all", item.ruleId); store.setView("alarms");
      });
      footer.append(sourceButton);
      card.append(heading, source, basis, drivers, actions, footer);
      return card;
    });
    $("maintenance-list").replaceChildren(...(cards.length ? cards : [node("p",
      advice.message || "暂无可生成的维护建议。", "empty-state")]));
  }
  function renderReport(state) {
    const ready = state.reportStatus === "ready", report = state.report;
    const continuous = Boolean(liveDevice(state, state.selected));
    text("report-title", continuous ? "持续运行摘要" : "运行分析报告");
    const verdict = reportVerdict(report);
    const status = $("report-state");
    status.dataset.result = verdict.result;
    status.textContent = ready ? (continuous && verdict.result === "unverifiable" ? "持续运行摘要 · 未判定" : verdict.label) : state.reportStatus === "loading" ? "正在读取报告…" : state.reportStatus === "error" ? "报告读取失败" : "报告未读取";
    text("report-error", state.reportError);
    $("report-error").hidden = !state.reportError;
    $("retry-report-button").hidden = state.reportStatus !== "error";
    const metrics = ready ? [
      ["受控故障事件", number(report.fault_events)], ["已检出事件", number(report.detected_events)],
      ["未检出事件", number(report.missed_events)], ["检出率", continuous && report.detection_rate === null ? "未评估（持续仿真）" : detectionText(report)],
      ["误报告警", number(report.false_alarm_count)], ["冗余告警", number(report.redundant_alarm_count)],
      ["未匹配告警", number(report.unmatched_alarm_count)], ["告警总数", number(report.alarm_count)],
      ["健康分最小值（全程）", number(report.health_score_min, 2)],
      ["健康分最大值（全程）", number(report.health_score_max, 2)],
      ["已分析窗口", number(report.feature_windows)], ["片内未成窗采样点", number(report.dropped_points)],
    ] : [];
    $("report-metrics").replaceChildren(...metrics.map(([label, value]) => {
      const pair = node("div"); pair.append(node("dt", label), node("dd", value)); return pair;
    }));
    $("report-reasons").replaceChildren(...(ready && Array.isArray(report.verdict_reasons)
      ? report.verdict_reasons.map((reason) => node("li", reason)) : []));
    const events = ready && Array.isArray(report.events) ? report.events : [];
    $("report-events-body").replaceChildren(...events.map((event) => {
      const row = node("tr");
      row.append(node("td", event.event_id ?? "未知", "mono"), node("td", event.type ?? "未知"),
        node("td", `[${event.start_seq ?? "?"}, ${event.end_seq_exclusive ?? "?"})`, "mono"),
        node("td", event.detected === true ? "已检出" : event.detected === false ? "未检出" : "未知"),
        node("td", number(event.lag_samples), "mono")); return row;
    }));
    if (ready && !events.length) {
      const row = node("tr"), cell = node("td", report.fault_events === 0 ? "本报告无受控故障事件，检出率不适用。" : "暂无逐事件记录。", "muted");
      cell.colSpan = 5; row.append(cell); $("report-events-body").append(row);
    }
    $("report-events-section").hidden = !ready;
    for (const format of ["json", "markdown"]) {
      const link = $(`report-download-${format}`);
      if (state.selected && ready) {
        link.href = apiUrl("/api/report/download", state.selected, { format });
        link.removeAttribute("aria-disabled"); link.removeAttribute("tabindex");
      } else { link.removeAttribute("href"); link.setAttribute("aria-disabled", "true"); link.tabIndex = -1; }
    }
  }
  function renderLive(state) {
    const shown = state.liveMode === "available" || state.liveMode === "error";
    $("live-monitor").hidden = !shown;
    $("device-list").dataset.live = String(shown);
    if (!shown) return;
    const status = liveStatus(state);
    text("live-status", status.label); $("live-status").dataset.level = status.level;
    const devices = state.live?.devices || [];
    const sum = (field) => devices.reduce((total, device) => total + device[field], 0);
    text("live-source", state.live ? `${devices.length} 台仿真设备 · ${number(state.live.sample_rate_hz)} Hz · ${number(state.live.window_points)} 点 / 窗`
      : "持续状态暂不可读取");
    const totals = state.live ? [["已采集样本", number(sum("captured_samples"))], ["已处理样本", number(sum("processed_samples"))],
      ["已写入窗口", number(sum("windows_written"))], ["待分析批次", number(sum("queue_depth"))]] : [];
    if (!renderedState || state.live !== renderedState.live) $("live-totals").replaceChildren(...totals.map(([label, value]) => {
      const pair = node("div"); pair.append(node("dt", label), node("dd", value)); return pair;
    }));
    text("live-note", state.liveError || state.live?.error || (state.pollPaused
      ? "自动读取已暂停"
      : `状态更新（北京时间）${timeText(state.live?.updated_at_utc)}`));
  }
  function render(state) {
    syncLocation(state);
    const titles = {
      health: "设备健康", alarms: "告警记录", maintenance: "维护建议",
      reports: "运行报告", notifications: "手机通知",
    };
    text("view-title", titles[state.view]);
    for (const view of VIEWS) {
      $(`${view}-view`).hidden = state.view !== view;
      const button = $(`nav-${view}`);
      if (state.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    }
    $("analysis-workspace").hidden = state.view === "notifications";
    notifications.sync(state);
    const error = state.catalogError || state.healthError || state.windowError || state.alarmError || state.fullError;
    text("global-message", error || (state.catalogStatus === "loading" ? "正在连接本机只读服务…" :
      state.healthStatus === "loading" ? "正在读取所选设备…" :
      state.fullLoading ? `正在读取全程趋势，已读取${state.windows.length}个真实窗口…` :
      state.windowLoading ? "正在读取更早窗口…" : state.alarmLoading ? "正在读取更早告警…" : ""));
    $("global-message").hidden = !error && state.catalogStatus !== "loading" && state.healthStatus !== "loading" && !state.windowLoading && !state.alarmLoading && !state.fullLoading;
    const continuousStatus = state.liveMode === "available" || state.liveMode === "error" ? liveStatus(state) : null;
    text("connection-status", continuousStatus ? continuousStatus.label : state.catalogStatus === "error" || state.healthStatus === "error" ? "读取失败" :
      error ? "部分读取失败" : state.healthStatus === "ready" ? "只读连接已就绪" : "正在读取");
    $("connection-status").dataset.level = error ? "error" : continuousStatus ? continuousStatus.level : state.healthStatus === "ready" ? "healthy" : "unknown";
    $("refresh-button").disabled = state.catalogStatus === "loading" || state.healthStatus === "loading";
    text("last-updated", state.lastRead ? `最近读取（北京时间）${timeText(state.lastRead)}` : "尚未成功读取");
    text("selected-title", state.selected?.device_id || "选择设备");
    text("selected-scenario", state.selected ? scenario(state.selected) : "尚未选择设备");
    text("selected-run", state.selected?.run_id || "—");
    const active = selectedDeviceStatus(state);
    text("selected-status", active.label); $("selected-status").dataset.level = active.level;
    const current = liveDevice(state, state.selected);
    $("selected-live-meta").hidden = !current;
    if (current) text("selected-live-meta", `仿真采集 ${number(current.captured_samples)} 样本 · 已处理 ${number(current.processed_samples)} · ${number(current.windows_written)} 窗 · 积压 ${number(current.queue_depth)} 个批次 · 最近提交（北京时间）${timeText(current.last_commit_at_utc)}${current.error ? ` · ${current.error}` : ""}`);
    $("live-update").hidden = !state.newDataAvailable;
    text("health-score-label", state.historyPinned || state.newDataAvailable ? "当前快照最新窗口健康分" : "最新窗口健康分");
    const latest = latestWindow(state.windows);
    text("health-score", latest ? number(latest.health_score, 2) : "—");
    text("health-window", latest ? `最新已存窗口 [${latest.start_seq}, ${latest.end_seq_exclusive})` : "暂无可用窗口");
    $("health-view").setAttribute("aria-busy", String(state.healthStatus === "loading"));
    const older = $("load-older-button");
    older.disabled = !state.windowCursor || state.windowLoading || state.fullLoading || state.healthStatus !== "ready";
    older.textContent = state.windowLoading ? "正在读取…" : state.windowCursor ? "加载更早窗口" : "已加载全部窗口";
    const allWindows = $("load-all-windows-button");
    allWindows.disabled = !state.windowCursor || state.fullLoading || state.windowLoading || state.healthStatus !== "ready";
    allWindows.textContent = state.fullLoading ? `读取全程…${state.windows.length}窗` : state.windowCursor ? "全程趋势" : "全程已读取";
    $("reset-chart-button").disabled = !state.historyPinned && !state.newDataAvailable && state.windows.length === state.initialWindows.length && state.inspectedSeq === null;
    const changed = (...fields) => !renderedState || fields.some((field) => state[field] !== renderedState[field]);
    renderLive(state);
    if (state.view === "notifications") $("live-monitor").hidden = true;
    if (changed("previews", "devices", "selected", "deviceSearch", "catalogStatus", "live", "liveError", "liveMode", "pollPaused", "clock")) renderDevices(state);
    if (changed("windows", "alarms", "inspectedSeq", "healthStatus", "windowLoading", "fullLoading", "view")) renderChart(state);
    if (changed("alarms", "alarmLevel", "alarmSearch", "alarmCursor", "alarmLoading", "healthStatus")) renderAlarms(state);
    if (changed("alarms", "states", "windows", "alarmCursor", "healthStatus", "selected")) renderMaintenance(state);
    if (changed("report", "reportStatus", "reportError", "selected", "liveMode")) renderReport(state);
    renderedState = state;
  }
  $("device-search").addEventListener("input", (event) => store.setDeviceSearch(event.target.value));
  $("alarm-search").addEventListener("input", (event) => store.setAlarmFilter(store.getState().alarmLevel, event.target.value));
  $("alarm-level-filter").addEventListener("change", (event) => store.setAlarmFilter(event.target.value));
  $("refresh-button").addEventListener("click", () => {
    if (store.getState().view === "notifications") void notifications.refresh();
    else void store.refresh();
  });
  $("show-latest-button").addEventListener("click", () => { void store.returnLatest(); });
  $("load-older-button").addEventListener("click", () => { void store.loadWindows(); });
  $("load-all-windows-button").addEventListener("click", () => { void store.loadAllWindows(); });
  $("reset-chart-button").addEventListener("click", () => store.resetWindows());
  $("load-more-alarms").addEventListener("click", () => { void store.loadAlarms(); });
  $("retry-report-button").addEventListener("click", () => { void store.retryReport(); });
  $("health-window-slider").addEventListener("input", (event) => store.inspect(chartPoints[Number(event.target.value)]?.start_seq ?? null));
  $("show-all-alarms").addEventListener("click", () => store.setView("alarms"));
  for (const view of VIEWS) $(`nav-${view}`).addEventListener("click", () => store.setView(view));
  $("health-chart").addEventListener("pointermove", (event) => {
    const svg = $("health-chart").querySelector("svg");
    if (!svg || !chartPoints.length) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width <= 0) return;
    const ratio = ((event.clientX - rect.left) / rect.width * chartLayout.width - chartLayout.left) / chartLayout.plotWidth;
    const first = chartPoints[0].start_seq, last = chartPoints.at(-1).start_seq;
    const target = first + Math.max(0, Math.min(1, ratio)) * (last - first);
    const nearest = chartPoints.reduce((best, row) => Math.abs(row.start_seq - target) < Math.abs(best.start_seq - target) ? row : best);
    if (nearest.start_seq !== store.getState().inspectedSeq) store.inspect(nearest.start_seq);
  });
  function restoreLocation() {
    restoring = true;
    const desired = locationSelection(location.search), state = store.getState();
    const device = state.devices.find((item) => item.device_id === desired.device && (!desired.run || item.run_id === desired.run));
    store.setView(desired.view);
    if (device && (!state.selected || deviceKey(device) !== deviceKey(state.selected))) void store.selectDevice(deviceKey(device));
    restoring = false;
  }
  window.addEventListener("popstate", restoreLocation);
  function scheduleChartResize() {
    const container = $("health-chart");
    if ($("health-view").hidden || container.clientWidth <= 0 || container.clientHeight <= 0) return;
    const size = `${container.clientWidth}:${container.clientHeight}`;
    if (size === lastChartSize || resizeFrame) return;
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      if (!$("health-view").hidden &&
          `${container.clientWidth}:${container.clientHeight}` !== lastChartSize) renderChart(store.getState());
    });
  }
  function scheduleLayoutResize() {
    const roster = $("device-list");
    const size = `${roster.clientWidth}:${roster.clientHeight}`;
    if (size !== lastRosterSize) {
      lastRosterSize = size;
      ensureSelectedInRoster(true);
    }
    // Roster visibility is independent of the selected health/alarms/report view.
    scheduleChartResize();
  }
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(scheduleLayoutResize) : null;
  if (observer) {
    observer.observe($("health-chart"));
    observer.observe($("device-list"));
  } else window.addEventListener("resize", scheduleLayoutResize);
  const destroy = store.destroy.bind(store);
  store.destroy = () => {
    notifications.destroy();
    observer?.disconnect();
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    window.removeEventListener("resize", scheduleLayoutResize);
    window.removeEventListener("popstate", restoreLocation);
    window.removeEventListener("pagehide", leavePage);
    destroy();
  };
  function leavePage(event) { if (!event.persisted) store.destroy(); }
  window.addEventListener("pagehide", leavePage);
  store.subscribe(render);
  void store.init();
  return store;
}
if (typeof document !== "undefined") startConsole();
