/* State and same-origin data operations for the read-only G15 console. */
export const VIEWS = ["health", "alarms", "maintenance", "reports", "notifications"];
export const deviceKey = (device) => JSON.stringify([device.run_id, device.device_id]);
export const finite = (value) => typeof value === "number" && Number.isFinite(value);
export const latestWindow = (windows) => windows.reduce(
  (latest, row) => !latest || row.start_seq > latest.start_seq ? row : latest, null
);
export const chronological = (windows) => [...windows].sort((a, b) => a.start_seq - b.start_seq);
export function mergeWindows(previous, incoming) {
  const rows = new Map(previous.map((row) => [row.start_seq, row]));
  for (const row of incoming) if (!rows.has(row.start_seq)) rows.set(row.start_seq, row);
  return [...rows.values()].sort((a, b) => b.start_seq - a.start_seq);
}
export function mergeAlarms(previous, incoming) {
  const key = (row) => JSON.stringify([row.start_seq, row.rule_id]);
  const rows = new Map(previous.map((row) => [key(row), row]));
  for (const row of incoming) if (!rows.has(key(row))) rows.set(key(row), row);
  return [...rows.values()].sort((a, b) =>
    b.start_seq - a.start_seq || String(b.rule_id).localeCompare(String(a.rule_id))
  );
}
export function activeStatus(states = []) {
  const levels = states.map((row) => row.state?.active_level);
  if (levels.includes("red")) return { level: "red", label: "红色活动告警" };
  if (levels.includes("yellow")) return { level: "yellow", label: "黄色活动告警" };
  if (levels.length && levels.every((value) => value === null || value === "normal")) {
    return { level: "healthy", label: "无活动告警" };
  }
  return { level: "unknown", label: "活动状态未知" };
}
export function reportVerdict(report) {
  if (report?.passed === true) return { result: "passed", label: "当前场景通过" };
  if (report?.passed === false) return { result: "failed", label: "当前场景未通过" };
  return { result: "unverifiable", label: "未判定" };
}
export function detectionText(report) {
  if (report?.detection_rate === null) {
    return report.fault_events === 0 ? "不适用（无受控故障事件）" : "未计算";
  }
  return finite(report?.detection_rate) ? `${(report.detection_rate * 100).toFixed(1)}%` : "未知";
}
export function filterAlarms(rows, level, search) {
  const needle = search.trim().toLocaleLowerCase();
  return rows.filter((row) => (level === "all" || row.level === level) &&
    (!needle || [row.rule_id, row.reason, row.level, row.start_seq]
      .some((value) => String(value ?? "").toLocaleLowerCase().includes(needle))));
}

const FEATURE_CHECKS = Object.freeze({
  mean: "复核振动均值偏移、传感器零点与当前工况。",
  std: "复核振动波动、运行稳定性与负载变化，并安排复测。",
  rms: "复核振动 RMS、传感器安装、设备紧固与负载变化。",
  ptp: "检查峰峰值对应的冲击区段，复核紧固情况和异常工况。",
  kurtosis_excess: "结合原始波形复核冲击性成分与异常采样点。",
  dominant_freq_hz: "复核主频变化、转速与采样配置，不直接归因到具体零部件。",
  crest_factor: "复核峰值因子与冲击区段，检查传感器安装和设备紧固。",
  zero_crossing_rate: "复核过零率变化、频谱特征与采样质量。",
});
const ACTIVE_LEVELS = new Set(["yellow", "red"]);

const NUMBER_PATTERN = String.raw`[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?`;
const MEASUREMENT_PATTERN = new RegExp(
  `^\\s*([a-z][a-z0-9_]*)=(${NUMBER_PATTERN})\\s*\\(risk\\s+(${NUMBER_PATTERN})\\)`, "i"
);
export function alarmMeasurements(alarm) {
  if (typeof alarm.reason !== "string") return [];
  const match = /(?:^|;\s*)drivers:\s*([^;]+)/.exec(alarm.reason);
  if (!match) return [];
  const rows = match[1].split(",").map((part) => MEASUREMENT_PATTERN.exec(part))
    .filter(Boolean).map((item) => ({ name: item[1].toLowerCase(),
      value: Number(item[2]), risk: Number(item[3]) }))
    .filter((item) => Number.isFinite(item.value) && Number.isFinite(item.risk) &&
      item.risk >= 0 && item.risk <= 1);
  return [...new Map(rows.map((row) => [row.name, row])).values()];
}
function alarmDrivers(alarm) {
  return [...new Set(alarmMeasurements(alarm).map((row) => row.name)
    .filter((name) => Object.hasOwn(FEATURE_CHECKS, name)))];
}

export function maintenanceAdvice(state) {
  const base = {
    items: [], missingActiveRules: [], partial: Boolean(state.alarmCursor),
    loadedAlarmCount: Array.isArray(state.alarms) ? state.alarms.length : 0,
  };
  if (state.healthStatus === "idle" || state.healthStatus === "loading") {
    return { ...base, status: "loading", message: "正在读取真实告警与活动状态。" };
  }
  if (state.healthStatus === "error") {
    return { ...base, status: "error", message: "告警数据读取失败，当前不能生成维护建议。" };
  }

  const alarmsByRule = new Map();
  for (const alarm of Array.isArray(state.alarms) ? state.alarms : []) {
    if (!alarm || typeof alarm.rule_id !== "string" ||
        !Number.isSafeInteger(alarm.start_seq) || !ACTIVE_LEVELS.has(alarm.level)) continue;
    const rows = alarmsByRule.get(alarm.rule_id) || [];
    rows.push(alarm); alarmsByRule.set(alarm.rule_id, rows);
  }
  const statesByRule = new Map((Array.isArray(state.states) ? state.states : [])
    .filter((row) => typeof row?.rule_id === "string")
    .map((row) => [row.rule_id, row.state?.active_level]));
  const activeLevels = new Map([...statesByRule].filter(([, level]) => ACTIVE_LEVELS.has(level)));
  const items = [], missingActiveRules = [];
  for (const [ruleId, rows] of alarmsByRule) {
    rows.sort((a, b) => b.start_seq - a.start_seq);
    const activeLevel = activeLevels.get(ruleId) || null;
    const source = activeLevel ? rows.find((row) => row.level === activeLevel) : rows[0];
    if (!source) {
      missingActiveRules.push(`${ruleId}（${activeLevel}）`);
      continue;
    }
    const drivers = alarmDrivers(source);
    const actions = drivers.map((name) => FEATURE_CHECKS[name]);
    if (!actions.length) actions.push("查看来源告警原因，复核对应特征、采样质量、传感器安装与当前工况。");
    actions.push(activeLevel === "red"
      ? "优先安排现场安全核查；是否停机及后续处置由人员按现场规程决定。"
      : activeLevel === "yellow"
        ? "安排近期点检并复测相关指标，观察异常是否持续或升级。"
        : "复核该历史告警的处置与复测记录；如再次出现，按现场流程处理。");
    const stateLevel = statesByRule.get(ruleId);
    items.push({
      key: `${ruleId}:${source.start_seq}`, ruleId, startSeq: source.start_seq,
      level: source.level, activeLevel,
      activity: activeLevel ? "active" : stateLevel === null || stateLevel === "normal" ? "inactive" : "unknown",
      urgency: activeLevel === "red" ? "优先现场核查" : activeLevel === "yellow" ? "安排近期点检" : "历史告警复核",
      drivers, basis: typeof source.reason === "string" ? source.reason : "来源未提供原因",
      actions,
    });
  }
  for (const [ruleId, level] of activeLevels) {
    if (!alarmsByRule.has(ruleId)) missingActiveRules.push(`${ruleId}（${level}）`);
  }
  const rank = { red: 2, yellow: 1 };
  items.sort((a, b) => (rank[b.activeLevel] || 0) - (rank[a.activeLevel] || 0) ||
    rank[b.level] - rank[a.level] || b.startSeq - a.startSeq ||
    a.ruleId.localeCompare(b.ruleId));
  const statesKnownInactive = statesByRule.size > 0 && [...statesByRule.values()]
    .every((level) => level === null || level === "normal");
  let message = "";
  if (!items.length && missingActiveRules.length) {
    message = "活动告警的来源记录尚未加载，请先在告警中心加载相关记录。";
  } else if (!items.length && statesKnownInactive) {
    message = "当前无活动告警，也没有已读取的历史告警；无需生成维护建议。";
  } else if (!items.length) {
    message = "暂无已读取告警，且活动状态未知；不能据此判定设备健康。";
  }
  return { ...base, status: "ready", items, missingActiveRules: missingActiveRules.sort(), message };
}
export function locationSelection(search) {
  const query = new URLSearchParams(search);
  return { device: query.get("device"), run: query.get("run"),
    view: VIEWS.includes(query.get("view")) ? query.get("view") : "health" };
}
export function apiUrl(path, device, extra = {}) {
  const query = new URLSearchParams({ run_id: device.run_id, device_id: device.device_id });
  for (const [name, value] of Object.entries(extra)) if (value !== null && value !== "") query.set(name, value);
  return `${path}?${query}`;
}
function validateHealth(body, device) {
  if (!body || body.run_id !== device.run_id || body.device_id !== device.device_id) {
    throw new Error("响应所属设备与当前选择不一致。");
  }
  for (const name of ["windows", "alarms"]) {
    const stream = body[name];
    if (!stream || !Array.isArray(stream.items) ||
        !(stream.next_cursor === null || typeof stream.next_cursor === "string")) {
      throw new Error("查询响应缺少有效的分页数据。");
    }
  }
  if (!Array.isArray(body.states)) throw new Error("查询响应缺少有效活动状态。");
  if (!body.windows.items.every((row) => Number.isInteger(row.start_seq) &&
      finite(row.health_score) && row.health_score >= 0 && row.health_score <= 100)) {
    throw new Error("健康窗口包含无法展示的数值。");
  }
  return body;
}
function message(error) {
  return error?.name === "AbortError" ? "" : (error?.message || "暂时无法读取，请重试。");
}

export const LIVE_STALE_MS = 10000;
const LIVE_STATUSES = ["starting", "running", "stopping", "stopped", "error", "limit_reached"];
const aborted = () => Object.assign(new Error("Superseded request"), { name: "AbortError" });
const timestamp = (value) => typeof value === "string" ? Date.parse(value) : NaN;
export function liveDevice(state, device) {
  return device && state.live?.devices.find((row) => deviceKey(row) === deviceKey(device));
}
export function liveStatus(state) {
  if (state.pollPaused) return { level: "unknown", label: "后台暂停更新" };
  if (state.liveError) return { level: "error", label: "持续状态读取失败" };
  if (!state.live) return { level: "unknown", label: "正在读取持续状态" };
  if (state.live.error || state.live.status === "error") return { level: "error", label: "仿真采集异常" };
  if (state.clock - timestamp(state.live.updated_at_utc) > LIVE_STALE_MS) {
    return { level: "unknown", label: "持续状态已过期" };
  }
  if (state.live.devices.some((row) => row.error)) return { level: "error", label: "部分设备采集失败" };
  if (state.live.status === "stopped") return { level: "unknown", label: "仿真采集已停止" };
  if (state.live.status === "limit_reached") return { level: "yellow", label: "已到采集上限" };
  if (state.live.status === "stopping") return { level: "yellow", label: "正在停止仿真采集" };
  if (state.live.status === "starting") return { level: "unknown", label: "正在启动仿真采集" };
  if (state.live.devices.some((row) => row.queue_depth > 0)) return { level: "yellow", label: "仿真采集中 · 分析有积压" };
  return { level: "healthy", label: "仿真采集中" };
}
export function deviceStatus(state, device, preview = state.previews[deviceKey(device)]) {
  if (!preview || preview.status !== "ready") return {
    level: preview?.status === "error" ? "error" : "unknown",
    label: preview?.status === "error" ? "读取失败" : "等待读取",
  };
  if (state.liveMode === "error") return { level: "error", label: "持续状态不可用" };
  if (state.liveMode === "legacy") return preview.active;
  const current = liveDevice(state, device);
  if (!current) return { level: "unknown", label: state.liveMode === "checking" ? "持续状态待确认" : "持续状态缺少此设备" };
  if (current.error) return { level: "error", label: "仿真采集失败" };
  const overall = liveStatus(state);
  if (overall.level === "error") return { level: "error", label: "持续状态异常" };
  if (state.pollPaused) return { level: "unknown", label: "暂停更新 · 已存数据" };
  if (state.live.status !== "running") return { level: "unknown", label: overall.label };
  if (overall.level === "unknown" || !finite(preview.readAt) || state.clock - preview.readAt > LIVE_STALE_MS ||
      (current.windows_written > 0 && (!Number.isFinite(timestamp(current.last_commit_at_utc)) ||
        state.clock - timestamp(current.last_commit_at_utc) > LIVE_STALE_MS))) {
    return { level: "unknown", label: "已存数据 · 更新过期" };
  }
  if (!preview.latest) return { level: "unknown", label: "等待首个分析窗口" };
  return preview.active;
}
export function selectedDeviceStatus(state) {
  if (!state.selected) return { level: "unknown", label: "等待读取" };
  const recent = state.previews[deviceKey(state.selected)];
  return deviceStatus(state, state.selected, {
    status: recent?.status === "error" ? "error" : state.healthStatus,
    active: activeStatus(state.states), latest: latestWindow(state.windows), readAt: state.detailReadAt,
  });
}
function validateLive(body) {
  const countNames = ["captured_samples", "processed_samples", "windows_written", "alarms_written", "queue_depth"];
  if (!body || body.schema_version !== "1" || !LIVE_STATUSES.includes(body.status) ||
      !Number.isFinite(timestamp(body.updated_at_utc)) || !Array.isArray(body.devices) ||
      !body.devices.every((row) => typeof row.run_id === "string" && typeof row.device_id === "string" &&
        countNames.every((name) => Number.isSafeInteger(row[name]) && row[name] >= 0) &&
        ["last_capture_at_utc", "last_commit_at_utc"].every((name) => row[name] === null || Number.isFinite(timestamp(row[name]))))) {
    throw new Error("持续采集状态格式不可用，请检查本机服务。");
  }
  return body;
}

export function createConsoleStore({ fetchImpl = globalThis.fetch, selection = {},
  now = () => Date.now(), timers = globalThis,
  visibilitySource = globalThis.document, pollIntervalMs = 2000, requestTimeoutMs = 6000 } = {}) {
  const listeners = new Set();
  const pending = new Map();
  let revision = 0, catalogRevision = 0, fullSequence = 0, pollRevision = 0;
  let destroyed = false, initialized = false, pollTimer = null, freshnessTimer = null, pollPromise = null;
  let initialWork = null, automaticDetailRevision = null;
  const initialPreviewKeys = new Set();
  const previewSequence = new Map();
  let state = {
    view: VIEWS.includes(selection.view) ? selection.view : "health",
    catalogStatus: "idle", catalogError: "", devices: [], selected: null, previews: {},
    healthStatus: "idle", healthError: "", windows: [], initialWindows: [],
    initialWindowCursor: null, windowCursor: null, alarmCursor: null, alarms: [],
    windowLoading: false, alarmLoading: false, windowError: "", alarmError: "",
    fullLoading: false, fullError: "", fullPages: 0,
    states: [], metadata: null, report: null, reportStatus: "idle", reportError: "",
    lastRead: null, detailReadAt: null, inspectedSeq: null,
    alarmLevel: "all", alarmSearch: "", deviceSearch: "",
    liveMode: "checking", live: null, liveError: "", pollPaused: visibilitySource?.visibilityState === "hidden",
    clock: now(), historyPinned: false, newDataAvailable: false,
  };
  const emit = () => { if (!destroyed) for (const listener of listeners) listener(state); };
  const update = (patch) => { if (!destroyed) { state = { ...state, ...patch, clock: now() }; emit(); } };
  const visible = () => visibilitySource?.visibilityState !== "hidden";
  const clearTimer = (timer) => { if (timer !== null) timers.clearTimeout(timer); };
  function armFreshness() {
    clearTimer(freshnessTimer); freshnessTimer = null;
    if (destroyed || !visible() || state.liveMode !== "available") return;
    const times = [timestamp(state.live?.updated_at_utc), state.detailReadAt,
      ...Object.values(state.previews).map((preview) => preview.readAt),
      ...(state.live?.devices || []).filter((row) => row.windows_written > 0).map((row) => timestamp(row.last_commit_at_utc))];
    const future = times.filter((value) => finite(value) && value + LIVE_STALE_MS >= now());
    if (!future.length) return;
    freshnessTimer = timers.setTimeout(() => {
      freshnessTimer = null; update({}); armFreshness();
    }, Math.max(1, Math.min(...future) + LIVE_STALE_MS - now() + 1));
  }
  const nextPreview = (key) => {
    const next = (previewSequence.get(key) || 0) + 1;
    previewSequence.set(key, next); return next;
  };
  function savePreview(device, body, sequence, error = "") {
    const key = deviceKey(device);
    if (destroyed || previewSequence.get(key) !== sequence) return;
    initialPreviewKeys.delete(key);
    update({ previews: { ...state.previews, [key]: {
      status: error ? "error" : "ready", error, readAt: now(),
      latest: body ? latestWindow(body.windows.items) : null,
      active: body ? activeStatus(body.states) : { level: "error", label: "读取失败" },
    } } });
    armFreshness();
  }
  async function request(channel, url, { optional404 = false } = {}) {
    if (destroyed) throw aborted();
    pending.get(channel)?.abort();
    const controller = new AbortController();
    pending.set(channel, controller);
    let timeout = null, abortListener;
    const cancellation = new Promise((_, reject) => {
      abortListener = () => {
        clearTimer(timeout);
        reject(controller.signal.reason?.name === "TimeoutError" ? controller.signal.reason : aborted());
      };
      controller.signal.addEventListener("abort", abortListener, { once: true });
      timeout = timers.setTimeout(() => controller.abort(Object.assign(
        new Error("读取超时，请刷新重试。"), { name: "TimeoutError" })), requestTimeoutMs);
    });
    try {
      const operation = (async () => {
        const response = await fetchImpl(url, { signal: controller.signal,
          credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
        if (controller.signal.aborted || pending.get(channel) !== controller) throw aborted();
        if (optional404 && response.status === 404) return null;
        if (!response.ok) {
          const suffix = response.status === 401 ? "认证暂不可用，请刷新或联系本机服务。"
            : response.status === 404 ? "该设备或报告暂不可读取。"
            : response.status === 503 ? "查询服务暂不可用，请稍后重试。" : "请求失败，请重试。";
          throw new Error(suffix);
        }
        const body = await response.json();
        if (controller.signal.aborted || pending.get(channel) !== controller) throw aborted();
        return body;
      })();
      return await Promise.race([operation, cancellation]);
    } finally {
      clearTimer(timeout);
      controller.signal.removeEventListener("abort", abortListener);
      if (pending.get(channel) === controller) pending.delete(channel);
    }
  }
  function abortDetail() {
    revision += 1; fullSequence += 1;
    for (const channel of ["health", "report", "windows", "alarms"]) {
      pending.get(channel)?.abort(); pending.delete(channel);
    }
  }
  function applyHealth(body) {
    update({ healthStatus: "ready", healthError: "", metadata: body,
      windows: [...body.windows.items], initialWindows: [...body.windows.items],
      initialWindowCursor: body.windows.next_cursor, windowCursor: body.windows.next_cursor,
      alarms: [...body.alarms.items], alarmCursor: body.alarms.next_cursor,
      states: body.states || [], inspectedSeq: null, lastRead: new Date(now()), detailReadAt: now(),
      newDataAvailable: false });
    armFreshness();
  }
  async function readHealth(device, epoch) {
    const key = deviceKey(device), sequence = nextPreview(key);
    try {
      const body = validateHealth(await request("health", apiUrl("/api/health", device, { page_size: 100 })), device);
      if (destroyed || epoch !== revision) return;
      savePreview(device, body, sequence); applyHealth(body);
    } catch (error) {
      if (destroyed || epoch !== revision || error.name === "AbortError") return;
      savePreview(device, null, sequence, message(error));
      update({ healthStatus: "error", healthError: message(error) });
    }
  }
  async function readReport(device, epoch) {
    update({ reportStatus: "loading", reportError: "" });
    try {
      const report = await request("report", apiUrl("/api/report", device));
      if (destroyed || epoch !== revision) return;
      if (!report || report.run_id !== device.run_id || report.device_id !== device.device_id) {
        throw new Error("报告所属运行与当前选择不一致。");
      }
      update({ report, reportStatus: "ready", reportError: "" });
    } catch (error) {
      if (destroyed || epoch !== revision || error.name === "AbortError") return;
      update({ reportStatus: "error", reportError: message(error), report: null });
    }
  }
  function select(device, { automatic = false } = {}) {
    if (destroyed) return Promise.resolve();
    abortDetail();
    const epoch = revision;
    automaticDetailRevision = automatic ? epoch : null;
    update({ selected: device, healthStatus: "loading", healthError: "", windows: [],
      initialWindows: [], initialWindowCursor: null, windowCursor: null, alarmCursor: null,
      alarms: [], states: [], metadata: null, report: null, reportStatus: "loading",
      reportError: "", windowLoading: false, alarmLoading: false, windowError: "", alarmError: "",
      inspectedSeq: null, lastRead: null, detailReadAt: null, fullLoading: false, fullError: "", fullPages: 0,
      historyPinned: false, newDataAvailable: false });
    return Promise.allSettled([readHealth(device, epoch), readReport(device, epoch)]).finally(() => {
      if (automaticDetailRevision === epoch) automaticDetailRevision = null;
    });
  }
  async function page(stream) {
    const device = state.selected, epoch = revision;
    const cursorName = stream === "windows" ? "windowCursor" : "alarmCursor";
    const loading = stream === "windows" ? "windowLoading" : "alarmLoading";
    const errorName = stream === "windows" ? "windowError" : "alarmError";
    const cursor = state[cursorName];
    if (!device || !cursor || state[loading] || state.healthStatus !== "ready" || destroyed) return;
    update({ [loading]: true, [errorName]: "", historyPinned: true });
    try {
      const body = validateHealth(await request(stream, apiUrl("/api/health", device,
        { page_size: 100, [stream === "windows" ? "windows_cursor" : "alarms_cursor"]: cursor })), device);
      if (destroyed || epoch !== revision || state[cursorName] !== cursor) return;
      const incoming = body[stream];
      if (incoming.next_cursor === cursor) throw new Error("分页未推进，已停止继续加载。请刷新后重试。");
      update({ [stream]: stream === "windows" ? mergeWindows(state.windows, incoming.items)
        : mergeAlarms(state.alarms, incoming.items),
      [cursorName]: incoming.next_cursor, [loading]: false, [errorName]: "", lastRead: new Date(now()) });
    } catch (error) {
      if (destroyed || epoch !== revision || error.name === "AbortError") return;
      update({ [loading]: false, [errorName]: message(error) });
    }
  }
  const canApplyLive = () => state.view === "health" && !state.historyPinned &&
    !state.fullLoading && !state.windowLoading && !state.alarmLoading && state.inspectedSeq === null &&
    state.healthStatus !== "loading";
  function hasNewData(body) {
    const incoming = latestWindow(body.windows.items), shown = latestWindow(state.windows);
    const knownAlarms = new Set(state.alarms.map((row) => JSON.stringify([row.start_seq, row.rule_id])));
    return Boolean(incoming && (!shown || incoming.start_seq > shown.start_seq)) ||
      body.alarms.items.some((row) => !knownAlarms.has(JSON.stringify([row.start_seq, row.rule_id])));
  }
  async function prefetch(device, generation, { polling = false, epoch = revision, cycle = pollRevision } = {}) {
    const key = deviceKey(device), sequence = nextPreview(key);
    try {
      const body = validateHealth(await request(`${polling ? "poll-preview" : "preview"}:${key}`,
        apiUrl("/api/health", device, { page_size: 100 })), device);
      if (destroyed || generation !== catalogRevision || cycle !== pollRevision) return;
      savePreview(device, body, sequence);
      if (polling && epoch === revision && state.selected && deviceKey(state.selected) === key) {
        if (canApplyLive()) applyHealth(body);
        else if (hasNewData(body)) update({ newDataAvailable: true });
      }
    } catch (error) {
      if (destroyed || generation !== catalogRevision || error.name === "AbortError" ||
          cycle !== pollRevision) return;
      savePreview(device, null, sequence, message(error));
      if (polling && epoch === revision && state.selected && deviceKey(state.selected) === key && canApplyLive()) {
        update({ healthStatus: "error", healthError: message(error) });
      }
    }
  }
  async function readPreviews(devices, generation, options = {}) {
    let index = 0;
    const cycle = options.cycle ?? pollRevision;
    const workers = Array.from({ length: Math.min(3, devices.length) }, async () => {
      while (!destroyed && generation === catalogRevision && cycle === pollRevision && visible() && index < devices.length) {
        const device = devices[index++];
        await prefetch(device, generation, { ...options, cycle });
      }
    });
    await Promise.allSettled(workers);
  }
  async function readLive(cycle) {
    try {
      const body = await request("live", "/api/live", { optional404: true });
      if (destroyed || cycle !== pollRevision) return false;
      if (body === null) {
        if (state.liveMode !== "checking") throw new Error("持续状态端点暂不可用，将继续重试。");
        update({ liveMode: "legacy", live: null, liveError: "" }); return false;
      }
      const live = validateLive(body);
      update({ liveMode: "available", live, liveError: "" }); armFreshness();
      return true;
    } catch (error) {
      if (destroyed || cycle !== pollRevision || error.name === "AbortError") return false;
      update({ liveMode: state.live ? "available" : "error", liveError: message(error) });
      return false;
    }
  }
  function needsInitialWork() {
    return initialPreviewKeys.size > 0 || state.liveMode === "checking" ||
      Boolean(state.selected && (state.healthStatus === "idle" || state.reportStatus === "idle"));
  }
  function startInitialWork() {
    if (initialWork || destroyed || !visible() || state.catalogStatus !== "ready" || !needsInitialWork()) {
      return initialWork || Promise.resolve();
    }
    const cycle = pollRevision, generation = catalogRevision;
    const work = (async () => {
      if (state.liveMode === "checking") await readLive(cycle);
      if (destroyed || !visible() || cycle !== pollRevision || generation !== catalogRevision) return;
      const device = (state.selected && state.devices.find((row) => deviceKey(row) === deviceKey(state.selected))) ||
        state.devices.find((row) => row.device_id === selection.device &&
          (!selection.run || row.run_id === selection.run)) || state.devices[0];
      if (!device) return;
      const selected = !state.selected || state.healthStatus === "idle" || state.reportStatus === "idle"
        ? select(device, { automatic: true }) : Promise.resolve();
      const previews = readPreviews(state.devices.filter((row) => initialPreviewKeys.has(deviceKey(row)) &&
        deviceKey(row) !== deviceKey(device)), generation, { cycle });
      await Promise.allSettled([selected, previews]);
    })();
    initialWork = work;
    void work.finally(() => {
      if (initialWork === work) initialWork = null;
      if (cycle !== pollRevision && visible() && needsInitialWork()) void startInitialWork();
      else schedulePoll();
    });
    return work;
  }
  function schedulePoll(delay = pollIntervalMs) {
    clearTimer(pollTimer); pollTimer = null;
    if (destroyed || !initialized || initialWork || !visible() || state.liveMode === "legacy") return;
    pollTimer = timers.setTimeout(() => { pollTimer = null; void poll(); }, delay);
  }
  async function poll() {
    if (pollPromise || initialWork || destroyed || !visible() || state.liveMode === "legacy") return pollPromise;
    const cycle = pollRevision, generation = catalogRevision, epoch = revision;
    pollPromise = (async () => {
      if (await readLive(cycle)) await readPreviews(state.devices, generation, { polling: true, epoch, cycle });
    })();
    try { await pollPromise; }
    finally { pollPromise = null; schedulePoll(); }
  }
  function visibilityChanged() {
    clearTimer(pollTimer); pollTimer = null;
    clearTimer(freshnessTimer); freshnessTimer = null;
    if (!visible()) {
      pollRevision += 1;
      for (const [channel, controller] of pending) if (channel === "live" || channel.startsWith("poll-preview:") || channel.startsWith("preview:")) {
        controller.abort(); pending.delete(channel);
      }
      if (automaticDetailRevision === revision) {
        automaticDetailRevision = null; abortDetail();
        update({ healthStatus: state.healthStatus === "loading" ? "idle" : state.healthStatus,
          reportStatus: state.reportStatus === "loading" ? "idle" : state.reportStatus });
      }
      update({ pollPaused: true });
    } else {
      update({ pollPaused: false }); armFreshness(); void startInitialWork(); schedulePoll(0);
    }
  }
  visibilitySource?.addEventListener?.("visibilitychange", visibilityChanged);
  const store = {
    getState: () => state,
    subscribe(listener) { listeners.add(listener); listener(state); return () => listeners.delete(listener); },
    async init() {
      const generation = ++catalogRevision;
      update({ catalogStatus: "loading", catalogError: "" });
      try {
        const catalog = await request("catalog", "/api/catalog");
        if (destroyed || generation !== catalogRevision) return;
        if (!catalog || catalog.readonly !== true || !Array.isArray(catalog.devices) ||
            !catalog.devices.every((row) => typeof row.run_id === "string" && typeof row.device_id === "string")) {
          throw new Error("设备目录格式不可用，请检查本机服务。");
        }
        initialPreviewKeys.clear();
        for (const device of catalog.devices) initialPreviewKeys.add(deviceKey(device));
        update({ devices: catalog.devices, catalogStatus: "ready" });
        await startInitialWork();
      } catch (error) {
        if (!destroyed && generation === catalogRevision && error.name !== "AbortError") {
          update({ catalogStatus: "error", catalogError: message(error) });
        }
      } finally { initialized = true; schedulePoll(); }
    },
    selectDevice(key) { const device = state.devices.find((row) => deviceKey(row) === key); return device ? select(device) : Promise.resolve(); },
    refresh() { return state.selected ? select(state.selected) : store.init(); },
    returnLatest() { update({ view: "health" }); return state.selected ? select(state.selected) : Promise.resolve(); },
    retryReport() { return state.selected ? readReport(state.selected, revision) : Promise.resolve(); },
    loadWindows: () => state.fullLoading ? Promise.resolve() : page("windows"),
    async loadAllWindows({ maxPages = 100, maxWindows = 10000 } = {}) {
      if (!Number.isInteger(maxPages) || maxPages < 1 || maxPages > 100 ||
          !Number.isInteger(maxWindows) || maxWindows < 100 || maxWindows > 10000) throw new Error("Invalid full-history guard");
      if (state.fullLoading || state.windowLoading || state.healthStatus !== "ready" || destroyed) return;
      const epoch = revision, sequence = ++fullSequence;
      update({ fullLoading: true, fullError: "", fullPages: 0, historyPinned: true });
      let pages = 0;
      while (epoch === revision && sequence === fullSequence && state.windowCursor && !destroyed) {
        if (pages >= maxPages || state.windows.length >= maxWindows) {
          update({ fullLoading: false, fullError: "已达到本次读取保护上限；当前仅为部分范围，未标记为全程。可重置或分段查看。" }); return;
        }
        await page("windows");
        if (destroyed || epoch !== revision || sequence !== fullSequence) return;
        if (state.windowError) { update({ fullLoading: false, fullError: "全程读取未完成，已保留成功读取的范围。" }); return; }
        pages += 1; update({ fullPages: pages });
      }
      if (!destroyed && epoch === revision && sequence === fullSequence) update({ fullLoading: false });
    },
    loadAlarms: () => page("alarms"),
    resetWindows() {
      if (state.newDataAvailable && liveDevice(state, state.selected)) return store.returnLatest();
      fullSequence += 1;
      pending.get("windows")?.abort(); pending.delete("windows");
      update({ windows: [...state.initialWindows], windowCursor: state.initialWindowCursor,
        inspectedSeq: null, windowLoading: false, windowError: "", fullLoading: false, fullError: "", fullPages: 0,
        historyPinned: false });
    },
    inspect(seq) { if (seq === null || state.windows.some((row) => row.start_seq === seq)) update({ inspectedSeq: seq }); },
    setView(view) { if (VIEWS.includes(view)) update({ view }); },
    setAlarmFilter(level, search = state.alarmSearch) { update({ alarmLevel: ["all", "red", "yellow"].includes(level) ? level : "all", alarmSearch: search }); },
    setDeviceSearch(value) { update({ deviceSearch: value }); },
    destroy() {
      destroyed = true; initialized = false; pollRevision += 1;
      clearTimer(pollTimer); clearTimer(freshnessTimer);
      visibilitySource?.removeEventListener?.("visibilitychange", visibilityChanged);
      abortDetail(); catalogRevision += 1;
      for (const controller of pending.values()) controller.abort();
      pending.clear(); listeners.clear();
    },
  };
  return store;
}

// SVG units match the measured CSS box, so axis labels remain actual 10px text.
export function plotLayout(containerWidth, containerHeight) {
  const width = Math.max(280, Math.round(finite(containerWidth) ? containerWidth : 280));
  const height = Math.max(180, Math.round(finite(containerHeight) ? containerHeight : 180));
  return { width, height, left: 38, right: width - 16, top: 20, bottom: height - 36,
    plotWidth: width - 54, plotHeight: height - 56 };
}
