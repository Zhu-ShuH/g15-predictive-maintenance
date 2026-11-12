/* State and same-origin data operations for the read-only G15 console. */
export const VIEWS = ["health", "alarms", "reports"];
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

export function createConsoleStore({ fetchImpl = globalThis.fetch, selection = {} } = {}) {
  const listeners = new Set();
  const pending = new Map();
  let revision = 0;
  let catalogRevision = 0;
  let fullSequence = 0;
  const previewSequence = new Map();
  let state = {
    view: VIEWS.includes(selection.view) ? selection.view : "health",
    catalogStatus: "idle", catalogError: "", devices: [], selected: null, previews: {},
    healthStatus: "idle", healthError: "", windows: [], initialWindows: [],
    initialWindowCursor: null, windowCursor: null, alarmCursor: null, alarms: [],
    windowLoading: false, alarmLoading: false, windowError: "", alarmError: "",
    fullLoading: false, fullError: "", fullPages: 0,
    states: [], metadata: null, report: null, reportStatus: "idle", reportError: "",
    lastRead: null, inspectedSeq: null, alarmLevel: "all", alarmSearch: "", deviceSearch: "",
  };
  const emit = () => { for (const listener of listeners) listener(state); };
  const update = (patch) => { state = { ...state, ...patch }; emit(); };
  const nextPreview = (key) => {
    const next = (previewSequence.get(key) || 0) + 1;
    previewSequence.set(key, next); return next;
  };
  function savePreview(device, body, sequence, error = "") {
    const key = deviceKey(device);
    if (previewSequence.get(key) !== sequence) return;
    update({ previews: { ...state.previews, [key]: {
      status: error ? "error" : "ready", error,
      latest: body ? latestWindow(body.windows.items) : null,
      active: body ? activeStatus(body.states) : { level: "error", label: "读取失败" },
    } } });
  }
  async function request(channel, url) {
    pending.get(channel)?.abort();
    const controller = new AbortController();
    pending.set(channel, controller);
    try {
      const response = await fetchImpl(url, { signal: controller.signal,
        credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
      if (controller.signal.aborted || pending.get(channel) !== controller) {
        throw Object.assign(new Error("Superseded request"), { name: "AbortError" });
      }
      if (!response.ok) {
        const suffix = response.status === 401 ? "认证暂不可用，请刷新或联系本机服务。"
          : response.status === 404 ? "该设备或报告暂不可读取。"
          : response.status === 503 ? "查询服务暂不可用，请稍后重试。" : "请求失败，请重试。";
        throw new Error(suffix);
      }
      const body = await response.json();
      if (controller.signal.aborted || pending.get(channel) !== controller) {
        throw Object.assign(new Error("Superseded request"), { name: "AbortError" });
      }
      return body;
    } catch (error) {
      if (controller.signal.aborted || pending.get(channel) !== controller) {
        throw Object.assign(new Error("Superseded request"), { name: "AbortError" });
      }
      throw error;
    } finally {
      if (pending.get(channel) === controller) pending.delete(channel);
    }
  }
  function abortDetail() {
    revision += 1;
    fullSequence += 1;
    for (const channel of ["health", "report", "windows", "alarms"]) {
      pending.get(channel)?.abort(); pending.delete(channel);
    }
  }
  async function readHealth(device, epoch) {
    const key = deviceKey(device), sequence = nextPreview(key);
    try {
      const body = validateHealth(await request("health",
        apiUrl("/api/health", device, { page_size: 100 })), device);
      if (epoch !== revision) return;
      savePreview(device, body, sequence);
      update({ healthStatus: "ready", healthError: "", metadata: body,
        windows: [...body.windows.items], initialWindows: [...body.windows.items],
        initialWindowCursor: body.windows.next_cursor, windowCursor: body.windows.next_cursor,
        alarms: [...body.alarms.items], alarmCursor: body.alarms.next_cursor,
        states: body.states || [], inspectedSeq: null, lastRead: new Date() });
    } catch (error) {
      if (epoch !== revision || error.name === "AbortError") return;
      savePreview(device, null, sequence, message(error));
      update({ healthStatus: "error", healthError: message(error) });
    }
  }
  async function readReport(device, epoch) {
    update({ reportStatus: "loading", reportError: "" });
    try {
      const report = await request("report", apiUrl("/api/report", device));
      if (epoch !== revision) return;
      if (!report || report.run_id !== device.run_id || report.device_id !== device.device_id) {
        throw new Error("报告所属运行与当前选择不一致。");
      }
      update({ report, reportStatus: "ready", reportError: "" });
    } catch (error) {
      if (epoch !== revision || error.name === "AbortError") return;
      update({ reportStatus: "error", reportError: message(error), report: null });
    }
  }
  function select(device) {
    abortDetail();
    const epoch = revision;
    update({ selected: device, healthStatus: "loading", healthError: "", windows: [],
      initialWindows: [], initialWindowCursor: null, windowCursor: null, alarmCursor: null,
      alarms: [], states: [], metadata: null, report: null, reportStatus: "loading",
      reportError: "", windowLoading: false, alarmLoading: false, windowError: "", alarmError: "",
      inspectedSeq: null, lastRead: null, fullLoading: false, fullError: "", fullPages: 0 });
    return Promise.allSettled([readHealth(device, epoch), readReport(device, epoch)]);
  }
  async function page(stream) {
    const device = state.selected, epoch = revision;
    const cursorName = stream === "windows" ? "windowCursor" : "alarmCursor";
    const loading = stream === "windows" ? "windowLoading" : "alarmLoading";
    const errorName = stream === "windows" ? "windowError" : "alarmError";
    const cursor = state[cursorName];
    if (!device || !cursor || state[loading] || state.healthStatus !== "ready") return;
    update({ [loading]: true, [errorName]: "" });
    try {
      const body = validateHealth(await request(stream, apiUrl("/api/health", device,
        { page_size: 100, [stream === "windows" ? "windows_cursor" : "alarms_cursor"]: cursor })), device);
      if (epoch !== revision || state[cursorName] !== cursor) return;
      const incoming = body[stream];
      if (incoming.next_cursor === cursor) throw new Error("分页未推进，已停止继续加载。请刷新后重试。");
      // The other stream may replay page one. It is deliberately untouched.
      update({ [stream]: stream === "windows" ? mergeWindows(state.windows, incoming.items)
        : mergeAlarms(state.alarms, incoming.items),
      [cursorName]: incoming.next_cursor, [loading]: false, [errorName]: "",
      lastRead: new Date() });
    } catch (error) {
      if (epoch !== revision || error.name === "AbortError") return;
      update({ [loading]: false, [errorName]: message(error) });
    }
  }
  async function prefetch(device, generation) {
    const key = deviceKey(device), sequence = nextPreview(key);
    try {
      const body = validateHealth(await request(`preview:${key}`,
        apiUrl("/api/health", device, { page_size: 100 })), device);
      if (generation === catalogRevision) savePreview(device, body, sequence);
    } catch (error) {
      if (generation === catalogRevision && error.name !== "AbortError") savePreview(device, null, sequence, message(error));
    }
  }
  return {
    getState: () => state,
    subscribe(listener) { listeners.add(listener); listener(state); return () => listeners.delete(listener); },
    async init() {
      const generation = ++catalogRevision;
      update({ catalogStatus: "loading", catalogError: "" });
      try {
        const catalog = await request("catalog", "/api/catalog");
        if (generation !== catalogRevision) return;
        if (!catalog || catalog.readonly !== true || !Array.isArray(catalog.devices) ||
            !catalog.devices.every((row) => typeof row.run_id === "string" && typeof row.device_id === "string")) {
          throw new Error("设备目录格式不可用，请检查本机服务。");
        }
        update({ devices: catalog.devices, catalogStatus: "ready" });
        const device = catalog.devices.find((row) => row.device_id === selection.device &&
          (!selection.run || row.run_id === selection.run)) || catalog.devices[0];
        if (device) {
          const selected = select(device);
          for (const other of catalog.devices) if (deviceKey(other) !== deviceKey(device)) void prefetch(other, generation);
          await selected;
        }
      } catch (error) {
        if (generation === catalogRevision && error.name !== "AbortError") update({ catalogStatus: "error", catalogError: message(error) });
      }
    },
    selectDevice(key) { const device = state.devices.find((row) => deviceKey(row) === key); return device ? select(device) : Promise.resolve(); },
    refresh() { return state.selected ? select(state.selected) : this.init(); },
    retryReport() { return state.selected ? readReport(state.selected, revision) : Promise.resolve(); },
    loadWindows: () => state.fullLoading ? Promise.resolve() : page("windows"),
    async loadAllWindows({ maxPages = 100, maxWindows = 10000 } = {}) {
      if (!Number.isInteger(maxPages) || maxPages < 1 || maxPages > 100 ||
          !Number.isInteger(maxWindows) || maxWindows < 100 || maxWindows > 10000) {
        throw new Error("Invalid full-history guard");
      }
      if (state.fullLoading || state.windowLoading || state.healthStatus !== "ready") return;
      const epoch = revision, sequence = ++fullSequence;
      update({ fullLoading: true, fullError: "", fullPages: 0 });
      let pages = 0;
      while (epoch === revision && sequence === fullSequence && state.windowCursor) {
        if (pages >= maxPages || state.windows.length >= maxWindows) {
          update({ fullLoading: false, fullError: "已达到本次读取保护上限；当前仅为部分范围，未标记为全程。可重置或分段查看。" });
          return;
        }
        await page("windows");
        if (epoch !== revision || sequence !== fullSequence) return;
        if (state.windowError) {
          update({ fullLoading: false, fullError: "全程读取未完成，已保留成功读取的范围。" });
          return;
        }
        pages += 1;
        update({ fullPages: pages });
      }
      if (epoch === revision && sequence === fullSequence) update({ fullLoading: false });
    },
    loadAlarms: () => page("alarms"),
    resetWindows() {
      fullSequence += 1;
      pending.get("windows")?.abort(); pending.delete("windows");
      update({ windows: [...state.initialWindows], windowCursor: state.initialWindowCursor,
        inspectedSeq: null, windowLoading: false, windowError: "", fullLoading: false, fullError: "", fullPages: 0 });
    },
    inspect(seq) { if (seq === null || state.windows.some((row) => row.start_seq === seq)) update({ inspectedSeq: seq }); },
    setView(view) { if (VIEWS.includes(view)) update({ view }); },
    setAlarmFilter(level, search = state.alarmSearch) { update({ alarmLevel: ["all", "red", "yellow"].includes(level) ? level : "all", alarmSearch: search }); },
    setDeviceSearch(value) { update({ deviceSearch: value }); },
    destroy() { abortDetail(); catalogRevision += 1; for (const controller of pending.values()) controller.abort(); pending.clear(); listeners.clear(); },
  };
}

// SVG units match the measured CSS box, so axis labels remain actual 10px text.
export function plotLayout(containerWidth, containerHeight) {
  const width = Math.max(280, Math.round(finite(containerWidth) ? containerWidth : 280));
  const height = Math.max(180, Math.round(finite(containerHeight) ? containerHeight : 180));
  return { width, height, left: 38, right: width - 16, top: 20, bottom: height - 36,
    plotWidth: width - 54, plotHeight: height - 56 };
}
