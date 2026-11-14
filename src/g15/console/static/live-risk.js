import { activeStatus, apiUrl, chronological, deviceKey, finite, latestWindow } from "./state.js";

const FRESH_MS = 10000;
const ACTIVE_EVENTS = new Set(["scheduled", "active", "recovering"]);
const EVENT_LABELS = { scheduled: "等待注入", active: "正在注入", recovering: "等待分析完成", completed: "注入样本已分析", cancelled: "风险已清除" };
const TYPE_LABELS = { spike: "冲击", step: "阶跃", burst: "振荡" };
const ERROR_TEXT = {
  live_risk_session_changed: "仿真会话已切换，请核对当前设备。",
  live_risk_not_running: "本次仿真会话已结束。",
  live_risk_busy: "该设备已有未完成风险，请先恢复正常基线。",
  live_risk_request_conflict: "请求标识与已存操作冲突，请刷新状态后重新操作。",
  live_risk_history_full: "本次会话的风险记录已达上限。",
  live_risk_request_limit: "本次会话的控制记录已达上限。",
  live_risk_device_not_found: "当前会话中没有此设备，请重新选择。",
  invalid_live_risk_request: "风险参数未被接受，请核对数值后重试。",
  live_risk_persistence_failed: "风险配置未能保存，操作结果尚未确认。",
  live_risk_unavailable: "当前服务未启用设备风险控制。",
  live_risk_access_denied: "当前身份无权控制此设备。",
  forbidden_origin: "请求来源未通过验证，请重新打开本机控制台。",
};
const invalid = () => Object.assign(new Error("风险响应格式不可用。"), { code: "invalid_response" });
const aborted = () => Object.assign(new Error("读取已取消"), { name: "AbortError" });
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const format = (value, digits = 1) => finite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
const samePair = (left, right) => !!left && !!right && left.run_id === right.run_id && left.device_id === right.device_id;

export function normalizeRiskSnapshot(body, sessionId) {
  if (!body || body.schema_version !== "1" || typeof body.session_id !== "string" ||
      !integer(body.version) || typeof body.controls_available !== "boolean" || !Array.isArray(body.devices) ||
      !["starting", "running", "stopping", "stopped", "limit_reached", "error"].includes(body.status)) throw invalid();
  if (sessionId && body.session_id !== sessionId) throw Object.assign(new Error(ERROR_TEXT.live_risk_session_changed), { code: "live_risk_session_changed" });
  const identities = new Set();
  for (const device of body.devices) {
    if (!device || typeof device.run_id !== "string" || typeof device.device_id !== "string" ||
        !["auto", "normal"].includes(device.mode) || typeof device.capture_paused !== "boolean" || identities.has(deviceKey(device))) throw invalid();
    identities.add(deviceKey(device));
    const event = device.event;
    if (event !== null && (!event || typeof event.event_id !== "string" || !Object.hasOwn(TYPE_LABELS, event.fault_type) ||
        !Object.hasOwn(EVENT_LABELS, event.status) || !finite(event.amplitude) || event.amplitude <= 0 ||
        !finite(event.delay_seconds) || event.delay_seconds < 0 || !finite(event.duration_seconds) || event.duration_seconds <= 0 ||
        !integer(event.start_seq) || !integer(event.end_seq_exclusive) || event.end_seq_exclusive <= event.start_seq ||
        !integer(event.duration_points) || event.duration_points !== event.end_seq_exclusive - event.start_seq ||
        !integer(event.captured_points) || !integer(event.processed_points) || event.captured_points > event.duration_points ||
        event.processed_points > event.captured_points)) throw invalid();
  }
  return body;
}

export function normalizeRiskHealth(body, device) {
  if (!samePair(body, device) || !Array.isArray(body?.windows?.items) || !Array.isArray(body.states) ||
      !Array.isArray(body?.alarms?.items) || body.windows.items.some((row) => !row || !integer(row.start_seq) ||
        !finite(row.health_score) || row.health_score < 0 || row.health_score > 100)) {
    throw Object.assign(new Error("设备健康数据格式不可用。"), { code: "invalid_health_response" });
  }
  return body;
}

export function riskRequest(action, identity, values = {}, requestId, sampleRate) {
  if (!["inject", "clear", "automatic"].includes(action) || !identity?.session_id || !identity.run_id || !identity.device_id || !requestId) throw new Error("设备或会话尚未确认。");
  const body = { session_id: identity.session_id, run_id: identity.run_id, device_id: identity.device_id, request_id: requestId, action };
  if (action !== "inject") return body;
  const numeric = (name) => typeof values[name] === "number" ? values[name] : typeof values[name] === "string" && values[name].trim() ? Number(values[name]) : NaN;
  const amplitude = numeric("amplitude"), delay = numeric("delay_seconds"), duration = numeric("duration_seconds");
  if (!Object.hasOwn(TYPE_LABELS, values.fault_type)) throw new Error("请选择冲击、阶跃或振荡风险。");
  if (!finite(amplitude) || amplitude <= 0 || amplitude > 10) throw new Error("强度须大于 0 且不超过 10 g。");
  if (!finite(delay) || delay < 0 || delay > 300) throw new Error("延后须为 0 至 300 秒。");
  if (!finite(duration) || duration <= 0 || duration > 300) throw new Error("持续时间须大于 0 且不超过 300 秒。");
  if (!finite(sampleRate) || sampleRate <= 0) throw new Error("采样频率尚未确认，请刷新状态。");
  if (values.fault_type === "burst" && Math.ceil(duration * sampleRate) < 3) throw new Error(`振荡风险至少需要 3 个样本，当前采样频率为 ${format(sampleRate)} Hz。`);
  return { ...body, fault_type: values.fault_type, amplitude, delay_seconds: delay, duration_seconds: duration };
}

export function riskControlState(state, now = Date.now()) {
  const device = state.live?.devices.find((row) => samePair(row, state.selected));
  const risk = state.risk?.devices.find((row) => samePair(row, state.selected));
  const heartbeat = Date.parse(state.live?.updated_at_utc);
  const stale = !state.live || state.liveFailed || !finite(state.liveAt) || now - state.liveAt > FRESH_MS ||
    (["starting", "running", "stopping"].includes(state.live.status) && (!finite(heartbeat) || now - heartbeat > FRESH_MS)) ||
    !state.risk || state.riskError || !finite(state.riskAt) || now - state.riskAt > FRESH_MS;
  const available = !state.hidden && !stale && !!device && !!risk && !device.error && !state.live.error &&
    state.live.status === "running" && state.risk.status === "running" && state.risk.controls_available === true;
  const enabled = available && !state.busy && !state.liveBusy && !state.pending;
  const active = ACTIVE_EVENTS.has(risk?.event?.status);
  return { device, risk, stale: !!stale, available, inject: enabled && !active,
    clear: enabled && (risk.mode !== "normal" || active), automatic: enabled && (risk.mode !== "auto" || active),
    retry: !state.hidden && !state.busy && !state.liveBusy && !!state.pending && !!state.live &&
      !state.liveFailed && state.pending.session_id === state.live.session_id && finite(state.liveAt) && now - state.liveAt <= FRESH_MS };
}

export function riskEventPresentation(risk, liveDevice, sampleRate) {
  const event = risk?.event;
  if (!event) return { label: risk ? "无计划风险" : "尚未读取风险状态", parameters: "", counts: "", progress: null };
  let label = EVENT_LABELS[event.status] || "风险状态未知";
  if (event.status === "cancelled") label = event.cancel_reason === "session_stopped" ? "会话结束 · 注入中止" : event.cancel_reason === "automatic" ? "风险已清除 · 自动场景" : "风险已清除";
  if (liveDevice?.capture_paused && ["scheduled", "active"].includes(event.status)) label = event.status === "scheduled" ? "已暂停 · 待采样" : "已暂停 · 注入未完成";
  const remaining = finite(liveDevice?.captured_samples) ? Math.max(0, event.start_seq - liveDevice.captured_samples) : null;
  const delay = event.status === "scheduled" && remaining !== null ? `距开始 ${format(remaining, 0)} 点${finite(sampleRate) && sampleRate > 0 ? ` / ${format(remaining / sampleRate, 3)} 秒` : ""} · ` : "";
  return { label, parameters: `${TYPE_LABELS[event.fault_type]} · ${format(event.amplitude, 3)} g · ${format(event.duration_seconds, 3)} 秒`,
    counts: `${delay}已采集 ${format(event.captured_points, 0)} / ${format(event.duration_points, 0)} 点 · 已分析 ${format(event.processed_points, 0)} 点`,
    progress: event.processed_points / event.duration_points };
}

export function riskHealthPresentation(state, now = Date.now()) {
  const control = riskControlState(state, now), windows = state.health?.windows.items || [], latest = latestWindow(windows);
  const liveStale = state.hidden || state.liveFailed ||
    !finite(state.liveAt) || now - state.liveAt > FRESH_MS ||
    (state.live?.status === "running" && (!finite(Date.parse(state.live.updated_at_utc)) || now - Date.parse(state.live.updated_at_utc) > FRESH_MS));
  const commitStale = state.live?.status === "running" && control.device?.capture_paused === false && control.device.windows_written > 0 &&
    (!finite(Date.parse(control.device.last_commit_at_utc)) || now - Date.parse(control.device.last_commit_at_utc) > FRESH_MS);
  const stale = liveStale || commitStale || !!state.healthError || !finite(state.healthAt) || now - state.healthAt > FRESH_MS;
  const active = activeStatus(state.health?.states || []);
  const captured = !state.live ? "等待采集状态" : liveStale ? "采集状态已过期" : control.device?.capture_paused ? "采集已暂停" : state.live.status === "running" ? "正在采集" : "会话已结束";
  const frozen = control.device?.capture_paused || state.live?.status !== "running";
  const rows = chronological(windows).slice(-60);
  const range = rows.length ? `seq ${format(rows[0].start_seq, 0)}–${format(rows.at(-1).start_seq, 0)} · ${rows.length} 窗` : "尚无已提交窗口";
  return { latest, rows, stale, captured, score: latest ? format(latest.health_score, 2) : "—",
    alarm: !state.health ? { level: "unknown", label: state.healthError ? "读取失败" : "尚未读取" } :
      stale ? { level: "unknown", label: `已存：${active.label}` } : active,
    alarmCount: integer(control.device?.alarms_written) ? format(control.device.alarms_written, 0) : "—",
    range: `${range}${stale ? " · 更新过期" : frozen ? " · 已存数据" : ""}` };
}

export function createLiveRiskController({ fetchImpl = globalThis.fetch, now = () => Date.now(), timers = globalThis,
  visibilitySource = globalThis.document, requestTimeoutMs = 6000, pollIntervalMs = 2000,
  makeRequestId = () => globalThis.crypto.randomUUID() } = {}) {
  const listeners = new Set(), requests = new Map();
  let destroyed = false, timer = null, epoch = 0;
  let state = { live: null, liveAt: null, liveFailed: false, liveBusy: false, selected: null,
    risk: null, riskAt: null, riskError: "", health: null, healthAt: null, healthError: "",
    hidden: !!visibilitySource?.hidden, reading: false, busy: false, pending: null, error: "", feedback: "" };
  const emit = () => { if (!destroyed) for (const listener of listeners) listener(state); };
  const update = (patch) => { if (!destroyed) { state = { ...state, ...patch }; emit(); } };
  const clearTimer = () => { if (timer !== null) timers.clearTimeout(timer); timer = null; };
  const schedule = () => { clearTimer(); if (!destroyed && !state.hidden && state.live) timer = timers.setTimeout(() => void refresh(), pollIntervalMs); };
  function cancelReads() { epoch += 1; for (const name of ["risk", "health"]) requests.get(name)?.abort(); clearTimer(); state = { ...state, reading: false }; }
  async function request(name, url, body) {
    requests.get(name)?.abort();
    const controller = new AbortController(); requests.set(name, controller);
    let timeout, onAbort;
    const cancellation = new Promise((_, reject) => {
      onAbort = () => { timers.clearTimeout(timeout); reject(controller.signal.reason?.name === "TimeoutError" ? controller.signal.reason : aborted()); };
      controller.signal.addEventListener("abort", onAbort, { once: true });
      timeout = timers.setTimeout(() => controller.abort(Object.assign(new Error("请求超时"), { name: "TimeoutError" })), requestTimeoutMs);
    });
    try {
      const operation = (async () => {
        const response = await fetchImpl(url, { method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store", signal: controller.signal,
          headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json", "X-G15-Console-Action": "live-risk" } : {}) },
          ...(body ? { body: JSON.stringify(body) } : {}) });
        let result; try { result = await response.json(); } catch { result = null; }
        if (controller.signal.aborted || requests.get(name) !== controller) throw aborted();
        if (!response.ok) throw Object.assign(new Error(ERROR_TEXT[result?.error] || (response.status === 404 ? "当前服务未启用设备风险控制，或设备已不在当前会话。" : "请求失败，请刷新后重试。")), { code: result?.error, status: response.status });
        return result;
      })();
      return await Promise.race([operation, cancellation]);
    } finally {
      timers.clearTimeout(timeout); controller.signal.removeEventListener("abort", onAbort);
      if (requests.get(name) === controller) requests.delete(name);
    }
  }
  function acceptRisk(body) {
    if (state.risk && state.risk.session_id === body.session_id && body.version < state.risk.version) return false;
    state = { ...state, risk: body, riskAt: now(), riskError: "" }; return true;
  }
  async function refresh() {
    if (destroyed || state.hidden || state.busy || state.reading || !state.live || !state.selected) return;
    clearTimer(); const current = ++epoch, selected = { ...state.selected }, sessionId = state.live.session_id;
    update({ reading: true });
    const results = await Promise.allSettled([
      request("risk", "/api/live/risk").then((body) => normalizeRiskSnapshot(body, sessionId)),
      request("health", apiUrl("/api/health", selected, { page_size: 60 })).then((body) => normalizeRiskHealth(body, selected)),
    ]);
    if (destroyed || current !== epoch || state.hidden) return;
    const [riskResult, healthResult] = results;
    if (riskResult.status === "fulfilled") acceptRisk(riskResult.value);
    else state = { ...state, riskError: riskResult.reason.message || "风险配置读取失败。" };
    if (healthResult.status === "fulfilled") state = { ...state, health: healthResult.value, healthAt: now(), healthError: "" };
    else state = { ...state, healthError: "设备健康数据读取失败，已保留最近快照。" };
    update({ reading: false }); schedule();
  }
  function updateLive(live, { receivedAt = now(), failed = false, busy = false } = {}) {
    if (destroyed) return;
    const changed = state.live?.session_id !== live?.session_id;
    const selected = live?.devices.find((row) => samePair(row, state.selected)) || live?.devices[0] || null;
    const targetChanged = changed || !samePair(state.selected, selected);
    if (targetChanged) {
      cancelReads();
      const pending = state.pending;
      if (changed) requests.get("write")?.abort();
      state = { ...state, selected: selected ? { run_id: selected.run_id, device_id: selected.device_id } : null,
        health: null, healthAt: null, healthError: "", ...(changed ? { risk: null, riskAt: null, riskError: "", pending: null, busy: false } : {}),
        error: pending && changed ? "会话已切换，上一请求仅属于旧会话。" : "", feedback: "" };
    }
    update({ live, liveAt: receivedAt, liveFailed: failed, liveBusy: busy });
    if (targetChanged && live) void refresh();
  }
  function select(key) {
    if (destroyed || state.busy || state.pending) return;
    const device = state.live?.devices.find((row) => deviceKey(row) === key);
    if (!device || samePair(device, state.selected)) return;
    cancelReads(); update({ selected: { run_id: device.run_id, device_id: device.device_id }, health: null, healthAt: null, healthError: "", error: "", feedback: "" });
    void refresh();
  }
  async function send(body) {
    cancelReads(); const sessionId = body.session_id;
    update({ busy: true, pending: body, error: "", feedback: `正在${body.action === "inject" ? "施加风险" : body.action === "clear" ? "恢复正常基线" : "恢复自动场景"}…` });
    try {
      const result = normalizeRiskSnapshot(await request("write", "/api/live/risk", body), sessionId);
      if (destroyed || state.live?.session_id !== sessionId) return;
      if (result.request?.request_id !== body.request_id || result.request?.action !== body.action) throw invalid();
      acceptRisk(result);
      update({ pending: null, feedback: `${body.device_id} · ${body.action === "inject" ? "风险计划已接受" : body.action === "clear" ? "已切换正常基线" : "已切换自动场景"}${result.request.replayed ? " · 已确认原请求" : ""}` });
    } catch (error) {
      if (destroyed || state.live?.session_id !== sessionId) return;
      const definitive = integer(error.status) && error.status >= 400 && error.status < 500;
      update({ pending: definitive ? null : body, feedback: "", error: definitive ? error.message :
        `${body.device_id} 的操作结果尚未确认。可使用“重试原请求”核对同一操作。` });
    } finally {
      if (!destroyed && state.live?.session_id === sessionId) { update({ busy: false }); void refresh(); schedule(); }
    }
  }
  async function submit(action, values) {
    const allowed = riskControlState(state, now());
    if (!(action === "inject" ? allowed.inject : action === "clear" ? allowed.clear : action === "automatic" ? allowed.automatic : false)) return;
    let body;
    try { body = riskRequest(action, { session_id: state.live.session_id, ...state.selected }, values, makeRequestId(), state.live.sample_rate_hz); }
    catch (error) { update({ error: error.message, feedback: "" }); return; }
    await send(body);
  }
  async function retry() { if (riskControlState(state, now()).retry) await send(state.pending); }
  function visibility() {
    cancelReads(); update({ hidden: !!visibilitySource.hidden });
    if (!state.hidden) void refresh();
  }
  visibilitySource?.addEventListener("visibilitychange", visibility);
  function destroy() {
    if (destroyed) return;
    destroyed = true; cancelReads(); for (const controller of requests.values()) controller.abort();
    requests.clear(); listeners.clear(); visibilitySource?.removeEventListener("visibilitychange", visibility);
  }
  return { updateLive, select, refresh, submit, retry, destroy, getState: () => state,
    subscribe(listener) { listeners.add(listener); listener(state); return () => listeners.delete(listener); } };
}

export function healthTrendGeometry(windows, width = 600, height = 190, event = null) {
  const rows = chronological(windows).slice(-60);
  if (!rows.length) return null;
  const left = 34, right = Math.max(left + 1, width - 10), top = 10, bottom = height - 29;
  const start = rows[0].start_seq, end = rows.at(-1).start_seq;
  const x = (seq) => start === end ? (left + right) / 2 : left + (seq - start) / (end - start) * (right - left);
  const y = (score) => bottom - score / 100 * (bottom - top);
  const path = rows.map((row, index) => {
    const previous = rows[index - 1], priorEnd = previous?.end_seq_exclusive ?? (integer(previous?.sample_count) ? previous.start_seq + previous.sample_count : null);
    const move = !index || priorEnd !== null && row.start_seq > priorEnd;
    return `${move ? "M" : "L"}${x(row.start_seq).toFixed(2)} ${y(row.health_score).toFixed(2)}`;
  }).join(" ");
  const clearedEarly = event?.status === "cancelled" && integer(event.cancel_seq) && event.cancel_seq < event.end_seq_exclusive;
  const markers = event?.captured_points > 0 ? [["注入", event.start_seq], [clearedEarly ? "清除" : "结束", clearedEarly ? event.cancel_seq : event.end_seq_exclusive]]
    .filter(([, seq]) => integer(seq) && seq >= start && seq <= end).map(([label, seq]) => ({ label, seq, x: x(seq) })) : [];
  return { path, markers, point: rows.length === 1 ? [x(start), y(rows[0].health_score)] : null, left, right, top, bottom, start, end,
    ticks: [0, 50, 100].map((value) => ({ value, y: y(value) })) };
}

export function mountLiveRiskPanel(documentRef = document, options = {}) {
  const $ = (id) => documentRef.getElementById(id);
  const text = (id, value) => { const node = $(id); if (node.textContent !== String(value)) node.textContent = value; };
  const controller = createLiveRiskController({ visibilitySource: documentRef, ...options });
  let chartKey = "", chartWidth = 0, lastState = controller.getState();
  const node = (tag, value) => { const result = documentRef.createElement(tag); if (value !== undefined) result.textContent = value; return result; };
  function message(id, value) { text(id, value); $(id).hidden = !value; }
  function renderChart(model) {
    const host = $("risk-health-chart"), width = Math.max(200, host.clientWidth || 600), height = host.clientHeight || 190;
    const event = lastState.risk?.devices.find((device) => samePair(device, lastState.selected))?.event;
    const key = `${deviceKey(lastState.selected || {})}|${width}|${height}|${model.range}|${event?.event_id}:${event?.status}:${event?.cancel_seq}|${model.rows.map((row) => `${row.start_seq}:${row.health_score}`).join(",")}`;
    if (key === chartKey) return;
    chartKey = key; chartWidth = width;
    const geometry = healthTrendGeometry(model.rows, width, height, event);
    if (!geometry) { const empty = node("p", "尚无已提交窗口"); empty.className = "stream-empty"; host.replaceChildren(empty); host.setAttribute("aria-label", "尚无健康趋势"); return; }
    const svgNode = (tag, attributes, value) => {
      const result = documentRef.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [name, content] of Object.entries(attributes)) result.setAttribute(name, String(content));
      if (value !== undefined) result.textContent = value; return result;
    };
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
    for (const tick of geometry.ticks) svg.append(svgNode("path", { d: `M${geometry.left} ${tick.y}H${geometry.right}`, class: "risk-grid" }), svgNode("text", { x: geometry.left - 7, y: tick.y + 4, "text-anchor": "end" }, String(tick.value)));
    geometry.markers.forEach((marker, index) => {
      const close = index && marker.x - geometry.markers[index - 1].x < 45;
      svg.append(svgNode("path", { d: `M${marker.x} ${geometry.top}V${geometry.bottom}`, class: "risk-event-marker" }),
        svgNode("text", { x: Math.min(geometry.right - 4, Math.max(geometry.left + 4, marker.x + 5)), y: geometry.top + (close ? 29 : 13),
          "text-anchor": marker.x > geometry.right - 38 ? "end" : "start", class: "risk-marker-label" }, marker.label));
    });
    svg.append(svgNode("path", { d: geometry.path, class: "risk-health-line" }));
    if (geometry.point) svg.append(svgNode("circle", { cx: geometry.point[0], cy: geometry.point[1], r: 3, class: "risk-health-point" }));
    svg.append(svgNode("text", { x: geometry.left, y: height - 7 }, `seq ${format(geometry.start, 0)}`));
    if (geometry.start !== geometry.end) svg.append(svgNode("text", { x: geometry.right, y: height - 7, "text-anchor": "end" }, format(geometry.end, 0)));
    host.replaceChildren(svg); host.setAttribute("aria-label", `最近 ${model.rows.length} 个已提交窗口，健康分范围 0 至 100，最新 ${model.score}，样本序号 ${geometry.start} 至 ${geometry.end}${model.stale ? "，更新过期" : ""}`);
  }
  function render(state) {
    lastState = state;
    const controls = riskControlState(state), model = riskHealthPresentation(state), devices = state.live?.devices || [];
    const select = $("risk-device"), signature = devices.map(deviceKey).join("|");
    if (select.dataset.signature !== signature) {
      select.replaceChildren(...devices.map((device) => { const option = node("option", device.device_id); option.value = deviceKey(device); return option; }));
      if (!devices.length) { const option = node("option", state.live ? "当前会话没有设备" : "正在读取设备"); option.value = ""; select.append(option); }
      select.dataset.signature = signature;
    }
    select.value = state.selected ? deviceKey(state.selected) : "";
    select.disabled = !devices.length || state.busy || !!state.pending;
    for (const id of ["risk-type", "risk-amplitude", "risk-delay", "risk-duration"]) $(id).disabled = !controls.available || state.busy || !!state.pending;
    $("risk-inject").disabled = !controls.inject; $("risk-clear").disabled = !controls.clear; $("risk-automatic").disabled = !controls.automatic;
    $("risk-retry").hidden = !state.pending || state.busy; $("risk-retry").disabled = !controls.retry;
    text("risk-connection", state.hidden ? "后台暂停更新" : state.busy ? "正在提交" : state.riskError ? "风险配置读取失败" : !state.risk ? "正在读取风险配置" : controls.stale ? "风险配置已过期" : state.risk.status !== "running" ? "会话已结束" : state.risk.controls_available ? "风险控制已连接" : "风险控制未启用");
    text("risk-mode", controls.risk?.mode === "auto" ? "自动场景" : controls.risk?.mode === "normal" ? "正常基线" : "场景未知");
    text("risk-capture", model.captured); text("risk-health", model.score); text("risk-alarm", model.alarm.label); $("risk-alarm").dataset.level = model.alarm.level;
    text("risk-alarm-count", model.alarmCount); text("risk-health-range", model.range);
    const event = riskEventPresentation(controls.risk, controls.device, state.live?.sample_rate_hz);
    text("risk-event-state", event.label); text("risk-event-parameters", event.parameters); text("risk-event-counts", event.counts);
    const progress = $("risk-progress"); progress.hidden = event.progress === null;
    if (event.progress !== null) { progress.value = event.progress; progress.setAttribute("aria-label", `${event.label}，${event.counts}`); }
    message("risk-error", [...new Set([state.error, state.riskError, state.healthError].filter(Boolean))].join(" "));
    message("risk-feedback", state.feedback);
    renderChart(model);
  }
  const unsubscribe = controller.subscribe(render);
  const values = () => ({ fault_type: $("risk-type").value, amplitude: $("risk-amplitude").value, delay_seconds: $("risk-delay").value, duration_seconds: $("risk-duration").value });
  const submit = (event) => { event.preventDefault(); if ($("risk-form").reportValidity()) void controller.submit("inject", values()); };
  const clear = () => void controller.submit("clear"), automatic = () => void controller.submit("automatic"), retry = () => void controller.retry(), select = () => controller.select($("risk-device").value);
  $("risk-form").addEventListener("submit", submit); $("risk-device").addEventListener("change", select);
  $("risk-clear").addEventListener("click", clear); $("risk-automatic").addEventListener("click", automatic); $("risk-retry").addEventListener("click", retry);
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(() => { if (Math.abs($("risk-health-chart").clientWidth - chartWidth) > 1) { chartKey = ""; renderChart(riskHealthPresentation(lastState)); } }) : null;
  observer?.observe($("risk-health-chart"));
  return { updateLive: controller.updateLive, refresh: controller.refresh, destroy() {
    unsubscribe(); controller.destroy(); observer?.disconnect(); $("risk-form").removeEventListener("submit", submit); $("risk-device").removeEventListener("change", select);
    $("risk-clear").removeEventListener("click", clear); $("risk-automatic").removeEventListener("click", automatic); $("risk-retry").removeEventListener("click", retry);
  } };
}
