const POLL_MS = 2000;
const MAX_HISTORY = 60;
const STALE_MS = 10000;
const number = (value) => Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 1 }) : "—";
const clock = (value) => {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";
};

export function sumCounter(devices, field) {
  if (!devices.length || devices.some((device) => !Number.isFinite(device[field]) || device[field] < 0)) return null;
  return devices.reduce((sum, device) => sum + device[field], 0);
}

export function sampleRates(previous, current, elapsedSeconds) {
  if (!previous || previous.session_id !== current.session_id || !Number.isFinite(elapsedSeconds) || elapsedSeconds <= 0 || elapsedSeconds > STALE_MS / 1000) return null;
  const identities = (snapshot) => snapshot.devices.map((device) => JSON.stringify([device.run_id, device.device_id])).sort().join("|");
  if (identities(previous) !== identities(current)) return null;
  const result = {};
  for (const [key, field] of [["capture", "captured_samples"], ["process", "processed_samples"]]) {
    const before = sumCounter(previous.devices, field), after = sumCounter(current.devices, field);
    if (before === null || after === null || after < before) return null;
    result[key] = (after - before) / elapsedSeconds;
  }
  return result;
}

export function deviceState(device, snapshot, stale = false) {
  if (stale) return { key: "stale", label: "状态已过期" };
  if (device.error) return { key: "error", label: "采集异常" };
  if (snapshot.error || snapshot.status === "error") return { key: "error", label: "会话异常" };
  if (snapshot.status !== "running") return { key: "stopped", label: snapshot.status === "starting" ? "正在启动" : snapshot.status === "stopping" ? "正在结束" : "会话已结束" };
  if (device.capture_paused === true) return { key: "paused", label: device.queue_depth > 0 ? "已停止 · 排空中" : "已停止采集" };
  if (device.capture_paused !== false) return { key: "stale", label: "控制状态未知" };
  return { key: "running", label: "正在采集" };
}

export function snapshotIsStale(snapshot, receivedAt, now = Date.now(), readFailed = false) {
  if (!snapshot || readFailed || !Number.isFinite(receivedAt) || now - receivedAt > STALE_MS) return true;
  // The runtime heartbeat continues while every device is paused.
  const heartbeat = Date.parse(snapshot.updated_at_utc);
  return ["starting", "running", "stopping"].includes(snapshot.status) && (!Number.isFinite(heartbeat) || now - heartbeat > STALE_MS);
}

export function simulationStatus(snapshot, stale = false) {
  if (stale) return { label: "状态已过期", level: "unknown" };
  if (snapshot.error || snapshot.status === "error") return { label: "仿真异常", level: "error" };
  const failed = snapshot.devices.filter((device) => device.error).length;
  if (failed) return { label: `${failed} 台设备采集异常`, level: "error" };
  if (snapshot.status === "running") {
    const running = snapshot.devices.filter((device) => device.capture_paused === false).length;
    if (running) return { label: `${running} 台正在采集`, level: "healthy" };
    if (snapshot.devices.length && snapshot.devices.every((device) => device.capture_paused === true)) return { label: "全部已停止采集", level: "unknown" };
    return { label: "控制状态未知", level: "unknown" };
  }
  return { label: snapshot.status === "starting" ? "仿真正在启动" : snapshot.status === "stopping" ? "仿真正在结束" : "仿真会话已结束", level: "unknown" };
}

const ERROR_TEXT = {
  live_not_running: "本次仿真会话已结束，无法继续启停。请从本机启动新的仿真会话。",
  live_session_changed: "仿真会话已切换，已重新读取当前状态。请核对设备后再次操作。",
  live_control_unavailable: "当前服务未启用仿真控制，可以继续查看已保存的数据。",
  live_unavailable: "当前服务未启用持续仿真，请启动本机仿真会话后刷新。",
  forbidden_origin: "服务拒绝了此次操作，请从本机控制台地址重新打开页面。",
  invalid_request: "启停请求未被接受，请刷新状态后重试。",
};

export function startSimulationPage(documentRef = document) {
  const $ = (id) => documentRef.getElementById(id);
  const text = (id, value) => { $(id).textContent = value; };
  const element = (tag, value, className = "") => {
    const node = documentRef.createElement(tag);
    if (value !== undefined) node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  let snapshot = null, previousSnapshot = null, previousAt = 0, receivedAt = 0;
  let busy = false, reading = false, readFailed = false, destroyed = false, timer = null, request = null;
  let rates = null, history = [], rows = new Map();
  let chartWidth = 0;
  const isStale = () => snapshotIsStale(snapshot, receivedAt, Date.now(), readFailed);
  const canControl = () => snapshot?.controls_available === true && snapshot.status === "running" && !snapshot.error && !busy && !reading && !isStale();

  function message(id, value) {
    text(id, value);
    $(id).hidden = !value;
  }

  function renderChart() {
    const host = $("stream-rate-chart"), width = Math.max(290, host.clientWidth || 600), height = 230;
    chartWidth = width;
    if (history.length < 2) {
      host.replaceChildren(element("p", history.length ? "已记录 1 次速率" : "等待速率数据", "stream-empty"));
      host.setAttribute("aria-label", "速率数据不足");
      return;
    }
    const svgNS = "http://www.w3.org/2000/svg";
    const svgNode = (tag, attrs, value) => {
      const node = documentRef.createElementNS(svgNS, tag);
      for (const [name, val] of Object.entries(attrs)) node.setAttribute(name, String(val));
      if (value !== undefined) node.textContent = value;
      return node;
    };
    const left = 56, right = width - 12, top = 14, bottom = height - 32;
    const ceiling = Math.max(1, ...history.flatMap((sample) => [sample.capture, sample.process]));
    const max = Math.ceil(ceiling / Math.pow(10, Math.floor(Math.log10(ceiling)))) * Math.pow(10, Math.floor(Math.log10(ceiling)));
    const start = history[0].at, end = history[history.length - 1].at;
    const x = (at) => left + (at - start) / Math.max(1, end - start) * (right - left);
    const y = (value) => bottom - value / max * (bottom - top);
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
    for (let index = 0; index < 4; index += 1) {
      const value = max * index / 3, py = y(value);
      svg.append(svgNode("path", { d: `M${left} ${py}H${right}`, class: "rate-grid" }), svgNode("text", { x: left - 9, y: py + 4, "text-anchor": "end" }, number(value)));
    }
    for (const key of ["capture", "process"]) {
      const path = history.map((sample, index) => `${index ? "L" : "M"}${x(sample.at).toFixed(1)} ${y(sample[key]).toFixed(1)}`).join(" ");
      svg.append(svgNode("path", { d: path, class: `rate-${key}` }));
    }
    svg.append(svgNode("text", { x: left, y: height - 8 }, clock(start)), svgNode("text", { x: right, y: height - 8, "text-anchor": "end" }, clock(end)));
    host.replaceChildren(svg);
    host.setAttribute("aria-label", `${history.length} 次实测读取速率，最新采集 ${number(rates?.capture)} 样本每秒，处理 ${number(rates?.process)} 样本每秒。时间范围 ${clock(start)} 至 ${clock(end)}。`);
  }

  function renderDevices(stale) {
    const present = new Set();
    for (const device of snapshot.devices) {
      const key = JSON.stringify([device.run_id, device.device_id]);
      present.add(key);
      let row = rows.get(key);
      if (!row) {
        const tr = element("tr"), identity = element("td"), link = element("a", device.device_id, "stream-device-name");
        link.href = `/?view=health&run=${encodeURIComponent(device.run_id)}&device=${encodeURIComponent(device.device_id)}`;
        const run = element("span", device.run_id, "stream-run"); run.title = device.run_id;
        identity.append(link, run);
        const stateCell = element("td"), badge = element("span", "", "stream-device-status"); stateCell.append(badge);
        const counts = element("td"), queue = element("td"), results = element("td"), commit = element("td"), action = element("td");
        const button = element("button", "", "button button-secondary"); button.type = "button";
        button.addEventListener("click", () => {
          const current = snapshot.devices.find((item) => item.run_id === device.run_id && item.device_id === device.device_id);
          if (current) void control(current.capture_paused ? "resume" : "pause", current);
        });
        action.append(button); tr.append(identity, stateCell, counts, queue, results, commit, action);
        row = { tr, badge, counts, queue, results, commit, button };
        rows.set(key, row); $("stream-device-rows").append(tr);
      }
      const status = deviceState(device, snapshot, stale);
      row.badge.textContent = status.label; row.badge.dataset.state = status.key;
      row.counts.textContent = `${number(device.captured_samples)} / ${number(device.processed_samples)}`;
      row.queue.textContent = number(device.queue_depth);
      row.results.textContent = `${number(device.windows_written)} / ${number(device.alarms_written)}`;
      row.commit.textContent = device.last_commit_at_utc ? clock(device.last_commit_at_utc) : "尚未提交";
      row.commit.title = device.last_commit_at_utc || "尚未提交";
      const resume = device.capture_paused === true;
      row.button.textContent = resume ? "启动采集" : "停止采集";
      row.button.setAttribute("aria-label", `${resume ? "启动" : "停止"} ${device.device_id} 的仿真采集`);
      row.button.classList.toggle("stream-start", resume);
      row.button.classList.toggle("stream-stop", !resume);
      row.button.disabled = !canControl() || Boolean(device.error) || typeof device.capture_paused !== "boolean";
    }
    for (const [key, row] of rows) if (!present.has(key)) { row.tr.remove(); rows.delete(key); }
    $("stream-device-empty").hidden = snapshot.devices.length > 0;
    text("stream-device-empty", "当前仿真会话没有设备。");
  }

  function render() {
    $("stream-refresh").disabled = reading || busy;
    if (!snapshot) {
      text("stream-connection", readFailed ? "连接失败" : "正在连接");
      text("stream-state", readFailed ? "状态不可用" : "等待状态");
      text("stream-device-empty", readFailed ? "无法读取设备，请刷新重试。" : "正在读取设备…");
      $("stream-start-all").disabled = true; $("stream-stop-all").disabled = true;
      return;
    }
    const stale = isStale(), devices = snapshot.devices;
    const paused = devices.filter((device) => device.capture_paused === true).length;
    const running = devices.filter((device) => deviceState(device, snapshot, stale).key === "running").length;
    const total = (field) => sumCounter(devices, field);
    const status = simulationStatus(snapshot, stale);
    text("stream-state", status.label); text("stream-connection", status.label);
    $("stream-state").dataset.level = status.level;
    text("stream-updated", `读取 ${clock(receivedAt)}`);
    text("stream-config", `${devices.length} 台仿真设备 · ${number(snapshot.sample_rate_hz)} Hz · ${number(snapshot.window_points)} 点 / 窗`);
    text("stream-device-count", `${devices.length} 台设备`);
    text("stream-pipeline-label", stale ? "显示最近快照 · 已过期" : `状态更新 ${clock(snapshot.updated_at_utc)}`);
    text("flow-captured", number(total("captured_samples")));
    text("flow-pending", number(total("pending_samples")));
    text("flow-processed", number(total("processed_samples")));
    text("flow-windows", number(total("windows_written")));
    text("flow-queue", `${number(total("queue_depth"))} 批待分析`);
    text("flow-alarms", `${number(total("alarms_written"))} 条历史告警`);
    text("flow-capture-rate", stale ? "速率已过期" : rates ? `${number(rates.capture)} 样本 / 秒` : "等待速率采样");
    text("flow-process-rate", stale ? "速率已过期" : rates ? `${number(rates.process)} 样本 / 秒` : "等待速率采样");
    $("pipeline-stations").dataset.active = String(!stale && !snapshot.error && snapshot.status === "running" && Boolean(rates && (rates.capture > 0 || rates.process > 0)));
    text("observe-running", stale ? "—" : `${running} 台`);
    text("observe-paused", stale ? "—" : `${paused} 台`);
    const latency = snapshot.latency?.recent_p95_seconds;
    text("observe-latency", Number.isFinite(latency) ? `${number(latency * 1000)} ms` : "尚无提交");
    text("observe-backpressure", number(snapshot.backpressure_events));
    text("observe-late", number(snapshot.late_cycles));
    $("stream-start-all").disabled = !canControl() || paused === 0 || devices.some((device) => device.error);
    $("stream-stop-all").disabled = !canControl() || running === 0;
    renderDevices(stale);
  }

  async function requestJSON(path, body) {
    const controller = new AbortController(); request = controller;
    const timeout = setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch(path, { method: body ? "POST" : "GET", cache: "no-store", credentials: "same-origin", signal: controller.signal,
        headers: body ? { "Content-Type": "application/json", "X-G15-Console-Action": "live-control", Accept: "application/json" } : { Accept: "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}) });
      const result = await response.json();
      if (!response.ok) throw Object.assign(new Error(result.error || "request_failed"), { code: result.error, status: response.status });
      if (!result || !Array.isArray(result.devices) || typeof result.session_id !== "string" || result.devices.some((device) => !device || typeof device.run_id !== "string" || typeof device.device_id !== "string")) throw new Error("invalid_live_state");
      return result;
    } finally { clearTimeout(timeout); if (request === controller) request = null; }
  }

  function accept(result) {
    const now = Date.now();
    if (snapshot && snapshot.session_id !== result.session_id) { history = []; previousSnapshot = null; rates = null; }
    rates = sampleRates(previousSnapshot, result, (now - previousAt) / 1000);
    if (rates) { history.push({ ...rates, at: now }); history = history.slice(-MAX_HISTORY); }
    else if (previousAt && now - previousAt > STALE_MS) history = [];
    previousSnapshot = result; previousAt = now; snapshot = result; receivedAt = now; readFailed = false;
    renderChart();
  }

  function schedule() {
    clearTimeout(timer);
    if (!destroyed && !documentRef.hidden) timer = setTimeout(() => void refresh(), POLL_MS);
  }

  async function refresh() {
    if (reading || busy || destroyed || documentRef.hidden) return;
    clearTimeout(timer); reading = true; render();
    try {
      const result = await requestJSON("/api/live");
      if (destroyed) return;
      accept(result);
      const limitLabel = ({ duration_limit: "已达到运行时长上限", disk_limit: "已达到磁盘使用上限" })[result.limit_reason] || "已达到运行限制";
      message("stream-error", result.error ? "仿真服务报告异常，请检查本机运行状态。" : result.limit_reason ? `本次仿真${limitLabel}` : result.controls_available !== true ? "当前会话设备启停不可用" : "");
    } catch (error) {
      if (destroyed || documentRef.hidden) return;
      readFailed = true;
      message("stream-error", ERROR_TEXT[error.code] || (error.status === 404 ? "当前服务未启用持续仿真，请启动本机仿真会话后刷新。" : "读取仿真状态失败，已保留最近快照。请检查本机服务并刷新。"));
    } finally { reading = false; if (!destroyed) { render(); schedule(); } }
  }

  async function control(action, device) {
    if (!canControl()) return;
    clearTimeout(timer); busy = true; message("stream-error", "");
    message("stream-feedback", `正在${action === "pause" ? "停止" : "启动"}${device ? ` ${device.device_id} ` : "全部设备"}采集…`); render();
    try {
      const result = await requestJSON("/api/live/control", { session_id: snapshot.session_id, action, ...(device ? { run_id: device.run_id, device_id: device.device_id } : {}) });
      if (destroyed) return;
      accept(result);
      message("stream-feedback", `${device ? device.device_id : "全部设备"}已${action === "pause" ? "停止采集" : "启动采集"}`);
    } catch (error) {
      if (destroyed) return;
      readFailed = true;
      message("stream-feedback", "");
      message("stream-error", ERROR_TEXT[error.code] || "启停结果尚未确认，正在重新读取状态。请核对后再操作。");
    } finally { busy = false; if (!destroyed) { render(); schedule(); } }
  }

  $("stream-refresh").addEventListener("click", () => void refresh());
  $("stream-start-all").addEventListener("click", () => void control("resume"));
  $("stream-stop-all").addEventListener("click", () => void control("pause"));
  const visibility = () => { clearTimeout(timer); if (documentRef.hidden) { if (!busy) request?.abort(); $("pipeline-stations").dataset.active = "false"; } else void refresh(); };
  documentRef.addEventListener("visibilitychange", visibility);
  const resize = typeof ResizeObserver === "function" ? new ResizeObserver(() => { if (Math.abs($("stream-rate-chart").clientWidth - chartWidth) > 1) renderChart(); }) : null;
  resize?.observe($("stream-rate-chart"));
  const destroy = () => { destroyed = true; clearTimeout(timer); request?.abort(); resize?.disconnect(); documentRef.removeEventListener("visibilitychange", visibility); };
  window.addEventListener("pagehide", destroy, { once: true });
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
  void refresh();
  return { refresh, destroy };
}

if (typeof document !== "undefined" && document.getElementById("stream-device-rows")) startSimulationPage();
