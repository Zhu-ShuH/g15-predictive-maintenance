import test from "node:test";
import assert from "node:assert/strict";
import { createConsoleStore, deviceKey, mergeWindows, mergeAlarms, latestWindow,
  chronological, activeStatus, reportVerdict, detectionText, filterAlarms, apiUrl, plotLayout, liveStatus, deviceStatus, LIVE_STALE_MS, selectedDeviceStatus, maintenanceAdvice } from "../../src/g15/console/static/state.js";

const A = { run_id: "run-a", device_id: "dev-a", scenario: "healthy" };
const B = { run_id: "run-b", device_id: "dev-b", scenario: "degradation" };
const windowRow = (seq, score = 75) => ({ start_seq: seq, end_seq_exclusive: seq + 10,
  health_score: score, sample_rate_hz: 100, sample_count: 10 });
const alarm = (seq, level = "yellow") => ({ start_seq: seq, rule_id: "rule", level, reason: "measured reason", created_at: "2026-11-12T04:00:00Z" });
const health = (device, windows, { wc = null, alarms = [], ac = null } = {}) => ({
  run_id: device.run_id, device_id: device.device_id, windows: { items: windows, next_cursor: wc },
  alarms: { items: alarms, next_cursor: ac }, states: [{ state: { active_level: null } }],
});
const report = (device, extra = {}) => ({ run_id: device.run_id, device_id: device.device_id,
  passed: null, detection_rate: null, fault_events: 0, false_alarm_count: 0, ...extra });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const tick = () => new Promise((done) => setImmediate(done));
function api(devices, handler) {
  return async (url, options) => {
    const parsed = new URL(url, "http://localhost");
    if (parsed.pathname === "/api/catalog") return json({ devices, readonly: true });
    if (parsed.pathname === "/api/live") return json({ error: "legacy" }, 404);
    const device = devices.find((item) => item.run_id === parsed.searchParams.get("run_id"));
    if (parsed.pathname === "/api/report") return handler?.(parsed, options, device) || json(report(device));
    return handler(parsed, options, device);
  };
}

test("maintenance advice keeps loading, error and unknown states distinct", () => {
  assert.equal(maintenanceAdvice({ healthStatus: "loading", alarms: [], states: [] }).status, "loading");
  assert.equal(maintenanceAdvice({ healthStatus: "error", alarms: [], states: [] }).status, "error");
  const unknown = maintenanceAdvice({ healthStatus: "ready", alarms: [], states: [] });
  assert.equal(unknown.items.length, 0);
  assert.match(unknown.message, /活动状态未知/);
  const healthy = maintenanceAdvice({ healthStatus: "ready", alarms: [],
    states: [{ rule_id: "rule", state: { active_level: null } }] });
  assert.match(healthy.message, /当前无活动告警/);
  const malformed = maintenanceAdvice({ healthStatus: "ready", alarms: [alarm(10)],
    states: [{ rule_id: "rule", state: { active_level: "unexpected" } }] });
  assert.equal(malformed.items[0].activity, "unknown");
  const malformedEmpty = maintenanceAdvice({ healthStatus: "ready", alarms: [],
    states: [{ rule_id: "rule", state: { active_level: "unexpected" } }] });
  assert.match(malformedEmpty.message, /活动状态未知/);
});

test("maintenance advice uses the active alarm source and explainable feature checks", () => {
  const red = { ...alarm(30, "red"),
    reason: "health 20; drivers: rms=2 (risk 1.00), crest_factor=4 (risk 0.50) <= red threshold 40" };
  const result = maintenanceAdvice({ healthStatus: "ready", alarms: [alarm(20), red],
    states: [{ rule_id: "rule", state: { active_level: "red" } }] });
  assert.equal(result.items.length, 1);
  assert.equal(result.items[0].startSeq, 30);
  assert.equal(result.items[0].level, "red");
  assert.equal(result.items[0].activity, "active");
  assert.deepEqual(result.items[0].drivers, ["rms", "crest_factor"]);
  assert.match(result.items[0].actions.join("\n"), /RMS/);
  assert.match(result.items[0].actions.join("\n"), /由人员按现场规程决定/);
  assert.equal(result.items[0].basis, red.reason);
});

test("maintenance advice separates historical records and incomplete pagination", () => {
  const result = maintenanceAdvice({ healthStatus: "ready", alarms: [alarm(20)], alarmCursor: "older",
    states: [{ rule_id: "rule", state: { active_level: null } }] });
  assert.equal(result.partial, true);
  assert.equal(result.items[0].activity, "inactive");
  assert.equal(result.items[0].urgency, "历史告警复核");
  assert.match(result.items[0].actions[0], /来源告警原因/);
});

test("maintenance advice never invents a missing active-alarm source", () => {
  const result = maintenanceAdvice({ healthStatus: "ready", alarms: [alarm(20)],
    states: [{ rule_id: "rule", state: { active_level: "red" } },
      { rule_id: "other", state: { active_level: "yellow" } }] });
  assert.deepEqual(result.items, []);
  assert.deepEqual(result.missingActiveRules, ["other（yellow）", "rule（red）"]);
  assert.match(result.message, /来源记录尚未加载/);
});

test("null report fields preserve unknown / not-applicable, never become zero or 100%", () => {
  assert.equal(reportVerdict(report(A)).result, "unverifiable");
  assert.equal(detectionText(report(A)), "不适用（无受控故障事件）");
  assert.equal(detectionText(report(A, { fault_events: 1 })), "未计算");
  assert.equal(detectionText(report(A, { detection_rate: 0 })), "0.0%");
  assert.equal(reportVerdict(report(A, { passed: false })).result, "failed");
  assert.equal(activeStatus([]).level, "unknown");
  assert.equal(activeStatus([{ state: { active_level: null } }]).label, "无活动告警");
});
test("window ordering and alarm filters do not recalculate business scores", () => {
  const rows = mergeWindows([windowRow(20, 49.5)], [windowRow(10, 0), windowRow(20, 100)]);
  assert.equal(latestWindow(rows).health_score, 49.5);
  assert.deepEqual(chronological(rows).map((row) => row.start_seq), [10, 20]);
  const alarms = mergeAlarms([alarm(20)], [alarm(20), alarm(10, "red")]);
  assert.equal(alarms.length, 2);
  assert.equal(filterAlarms(alarms, "red", "measured").length, 1);
  assert.equal(filterAlarms(alarms, "all", "missing").length, 0);
});

test("window and alarm pagination ignore the other stream's first-page replay", async () => {
  const firstAlarm = alarm(30), oldAlarm = alarm(10, "red");
  const fetchImpl = api([A], (url) => {
    if (url.pathname === "/api/report") return json(report(A));
    if (url.searchParams.get("windows_cursor")) return json(health(A, [windowRow(10)], { alarms: [firstAlarm], ac: "a1" }));
    if (url.searchParams.get("alarms_cursor")) return json(health(A, [windowRow(30)], { wc: "w1", alarms: [oldAlarm] }));
    return json(health(A, [windowRow(30)], { wc: "w1", alarms: [firstAlarm], ac: "a1" }));
  });
  const store = createConsoleStore({ fetchImpl });
  await store.init(); await store.loadWindows();
  assert.deepEqual(store.getState().windows.map((row) => row.start_seq), [30, 10]);
  assert.deepEqual(store.getState().alarms, [firstAlarm]);
  assert.equal(store.getState().alarmCursor, "a1");
  await store.loadAlarms();
  assert.deepEqual(store.getState().windows.map((row) => row.start_seq), [30, 10]);
  assert.deepEqual(store.getState().alarms.map((row) => row.start_seq), [30, 10]);
  assert.equal(store.getState().windowCursor, null);
  store.destroy();
});

test("late health/report responses cannot overwrite a different selected device, even if abort is ignored", async () => {
  const lateHealth = deferred(), lateReport = deferred();
  const fetchImpl = api([A, B], (url, _options, device) => {
    if (device === A) return url.pathname === "/api/report" ? lateReport.promise : lateHealth.promise;
    return json(url.pathname === "/api/report" ? report(B, { passed: false }) : health(B, [windowRow(80, 49)]));
  });
  const store = createConsoleStore({ fetchImpl });
  const initializing = store.init(); await tick();
  await store.selectDevice(deviceKey(B));
  lateHealth.resolve(json(health(A, [windowRow(20, 99)])));
  lateReport.resolve(json(report(A, { passed: true })));
  await initializing;
  assert.equal(store.getState().selected.device_id, B.device_id);
  assert.equal(latestWindow(store.getState().windows).health_score, 49);
  assert.equal(store.getState().report.device_id, B.device_id);
  assert.equal(store.getState().report.passed, false);
  store.destroy();
});

test("a stale report HTTP failure cannot override a newer successful retry", async () => {
  const late = deferred(); let reportCalls = 0;
  const store = createConsoleStore({ fetchImpl: api([A], (url) => {
    if (url.pathname === "/api/report") return ++reportCalls === 1 ? late.promise : json(report(A, { passed: true, false_alarm_count: 9 }));
    return json(health(A, [windowRow(20)]));
  }) });
  const initializing = store.init(); await tick(); await store.retryReport();
  late.resolve(json({ error: "unavailable" }, 503)); await initializing;
  assert.equal(store.getState().reportStatus, "ready");
  assert.equal(store.getState().report.false_alarm_count, 9);
  assert.equal(store.getState().reportError, "");
  store.destroy();
});

test("full trend follows every real cursor in order without appending alarm replay", async () => {
  const requested = [], firstAlarm = alarm(15);
  const store = createConsoleStore({ fetchImpl: api([A], (url) => {
    if (url.pathname === "/api/report") return json(report(A));
    const cursor = url.searchParams.get("windows_cursor"); if (cursor) requested.push(cursor);
    return json(cursor === "p1" ? health(A, [windowRow(20)], { wc: "p2", alarms: [firstAlarm] })
      : cursor === "p2" ? health(A, [windowRow(10)], { alarms: [firstAlarm] })
      : health(A, [windowRow(30)], { wc: "p1", alarms: [firstAlarm] }));
  }) });
  await store.init(); await store.loadAllWindows();
  assert.deepEqual(requested, ["p1", "p2"]);
  assert.equal(store.getState().windowCursor, null);
  assert.deepEqual(store.getState().windows.map((row) => row.start_seq), [30, 20, 10]);
  assert.equal(store.getState().alarms.length, 1);
  store.resetWindows();
  assert.deepEqual(store.getState().windows.map((row) => row.start_seq), [30]);
  assert.equal(store.getState().windowCursor, "p1");
  store.destroy();
});

test("full trend guard keeps a partial range and does not claim completion", async () => {
  let pageCalls = 0;
  const store = createConsoleStore({ fetchImpl: api([A], (url) => {
    if (url.pathname === "/api/report") return json(report(A));
    if (url.searchParams.has("windows_cursor")) {
      pageCalls++; return json(health(A, [windowRow(20)], { wc: "p2" }));
    }
    return json(health(A, [windowRow(30)], { wc: "p1" }));
  }) });
  await store.init(); await store.loadAllWindows({ maxPages: 1 });
  assert.equal(pageCalls, 1);
  assert.equal(store.getState().windowCursor, "p2");
  assert.equal(store.getState().windows.length, 2);
  assert.match(store.getState().fullError, /部分范围/);
  assert.equal(store.getState().fullLoading, false);
  store.destroy();
});

test("reset cancels a pending full trend chain without losing the initial page", async () => {
  const late = deferred();
  const store = createConsoleStore({ fetchImpl: api([A], (url) =>
    url.pathname === "/api/report" ? json(report(A)) :
    url.searchParams.has("windows_cursor") ? late.promise : json(health(A, [windowRow(30)], { wc: "p1" }))) });
  await store.init(); const all = store.loadAllWindows(); await tick();
  store.resetWindows();
  late.resolve(json(health(A, [windowRow(20)], { wc: "p2" })));
  await all;
  assert.deepEqual(store.getState().windows.map((row) => row.start_seq), [30]);
  assert.equal(store.getState().windowCursor, "p1");
  assert.equal(store.getState().fullLoading, false);
  store.destroy();
});

test("full trend failure preserves successfully read windows and exposes an unfinished cursor", async () => {
  const store = createConsoleStore({ fetchImpl: api([A], (url) => {
    if (url.pathname === "/api/report") return json(report(A));
    const cursor = url.searchParams.get("windows_cursor");
    return cursor === "p2" ? json({}, 503) : json(cursor === "p1"
      ? health(A, [windowRow(20)], { wc: "p2" }) : health(A, [windowRow(30)], { wc: "p1" }));
  }) });
  await store.init(); await store.loadAllWindows();
  assert.deepEqual(store.getState().windows.map((row) => row.start_seq), [30, 20]);
  assert.equal(store.getState().windowCursor, "p2");
  assert.match(store.getState().fullError, /未完成/);
  assert.ok(store.getState().windowError);
  store.destroy();
});

test("first health failure leaves unknown values and no fabricated zero score", async () => {
  const store = createConsoleStore({ fetchImpl: api([A], (url) =>
    url.pathname === "/api/report" ? json(report(A)) : json({}, 503)) });
  await store.init();
  assert.equal(store.getState().healthStatus, "error");
  assert.equal(latestWindow(store.getState().windows), null);
  assert.equal(activeStatus(store.getState().states).level, "unknown");
  store.destroy();
});

test("browser requests and download URLs contain no bearer credentials", async () => {
  const optionsSeen = [];
  const base = api([A], (url) => json(url.pathname === "/api/report" ? report(A) : health(A, [])));
  const store = createConsoleStore({ fetchImpl: (url, options) => { optionsSeen.push([url, options]); return base(url, options); } });
  await store.init();
  for (const [url, options] of optionsSeen) {
    assert.equal(options.headers.Authorization, undefined);
    assert.equal(new URL(url, "http://localhost").searchParams.has("token"), false);
  }
  const url = apiUrl("/api/report/download", A, { format: "json" });
  assert.match(url, /format=json/);
  assert.doesNotMatch(url, /token|Bearer/);
  store.destroy();
});

test("refresh cancels a pending full trend and cannot append old windows afterward", async () => {
  const late = deferred(); let initialReads = 0;
  const store = createConsoleStore({ fetchImpl: api([A], (url) => {
    if (url.pathname === "/api/report") return json(report(A));
    if (url.searchParams.has("windows_cursor")) return late.promise;
    initialReads++;
    return json(initialReads === 1 ? health(A, [windowRow(30)], { wc: "p1" }) : health(A, [windowRow(99)]));
  }) });
  await store.init(); const loading = store.loadAllWindows(); await tick();
  await store.refresh();
  late.resolve(json(health(A, [windowRow(20)], { wc: "p2" }))); await loading;
  assert.deepEqual(store.getState().windows.map((row) => row.start_seq), [99]);
  assert.equal(store.getState().windowCursor, null);
  assert.equal(store.getState().fullLoading, false);
  store.destroy();
});

test("full trend window-count protection never requests another page at the cap", async () => {
  let more = 0;
  const windows = Array.from({ length: 100 }, (_, index) => windowRow(1000 - index));
  const store = createConsoleStore({ fetchImpl: api([A], (url) => {
    if (url.pathname === "/api/report") return json(report(A));
    if (url.searchParams.has("windows_cursor")) more++;
    return json(health(A, windows, { wc: "p1" }));
  }) });
  await store.init(); await store.loadAllWindows({ maxWindows: 100 });
  assert.equal(more, 0);
  assert.equal(store.getState().windows.length, 100);
  assert.equal(store.getState().windowCursor, "p1");
  assert.ok(store.getState().fullError);
  store.destroy();
});

test("plot coordinates track the measured mobile and desktop boxes without a fixed 960-unit scale", () => {
  for (const [width, height] of [[320, 205], [780, 234]]) {
    const layout = plotLayout(width, height);
    assert.equal(layout.width, width);
    assert.equal(layout.height, height);
    assert.ok(layout.left > 0 && layout.right < width);
    assert.ok(layout.top > 0 && layout.bottom < height);
    assert.equal(layout.right - layout.left, layout.plotWidth);
    assert.equal(layout.bottom - layout.top, layout.plotHeight);
    assert.ok(layout.plotWidth >= 200);
    assert.ok(layout.height - layout.bottom >= 30);
  }
  const narrow = plotLayout(0, 0);
  assert.ok(narrow.width >= 280 && narrow.height >= 180);
});


function fakeTime() {
  let current = Date.parse("2026-11-12T08:00:00Z"), nextId = 0;
  const jobs = new Map();
  const visibility = new EventTarget(); visibility.visibilityState = "visible";
  return {
    now: () => current, visibility,
    timers: {
      setTimeout(callback, delay) { const id = ++nextId; jobs.set(id, { at: current + delay, callback }); return id; },
      clearTimeout(id) { jobs.delete(id); },
    },
    async advance(amount) {
      const end = current + amount;
      for (;;) {
        const entry = [...jobs].filter(([, job]) => job.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!entry) break;
        current = entry[1].at; jobs.delete(entry[0]); entry[1].callback(); await tick();
      }
      current = end; await tick();
    },
    setVisibility(value) { visibility.visibilityState = value; visibility.dispatchEvent(new Event("visibilitychange")); },
    get pendingCount() { return jobs.size; },
  };
}
function liveSnapshot(devices, clock, extra = {}) {
  const utc = new Date(clock.now()).toISOString();
  return { schema_version: "1", status: "running", started_at_utc: utc, updated_at_utc: utc,
    sample_rate_hz: 100, window_points: 10, error: null,
    devices: devices.map((device) => ({ ...device, captured_samples: 30, processed_samples: 30,
      windows_written: 3, alarms_written: 0, queue_depth: 0,
      last_capture_at_utc: utc, last_commit_at_utc: utc, error: null })), ...extra };
}
function liveApi(devices, clock, handler) {
  return (url, options) => {
    const parsed = new URL(url, "http://localhost");
    const device = devices.find((row) => row.run_id === parsed.searchParams.get("run_id"));
    return handler?.(parsed, options, device) || (parsed.pathname === "/api/catalog"
      ? json({ devices, readonly: true }) : parsed.pathname === "/api/live"
      ? json(liveSnapshot(devices, clock)) : parsed.pathname === "/api/report"
      ? json(report(device)) : json(health(device, [windowRow(20)])));
  };
}
function liveStore(clock, fetchImpl, extra = {}) {
  return createConsoleStore({ fetchImpl, now: clock.now, timers: clock.timers,
    visibilitySource: clock.visibility, ...extra });
}

test("legacy live endpoint 404 remains a quiet static console without a poll timer", async () => {
  const clock = fakeTime(); let reads = 0;
  const store = liveStore(clock, api([A], (url) => {
    reads++; return json(url.pathname === "/api/report" ? report(A) : health(A, [windowRow(10)]));
  }));
  await store.init(); const initialReads = reads;
  assert.equal(store.getState().liveMode, "legacy");
  assert.equal(store.getState().liveError, "");
  assert.equal(clock.pendingCount, 0);
  await clock.advance(20000);
  assert.equal(reads, initialReads);
  store.destroy();
});

test("ten-device previews are real health requests with at most four initial and three polling workers", async () => {
  const clock = fakeTime(); let active = 0, maximum = 0, pollMaximum = 0, polling = false;
  const devices = Array.from({ length: 10 }, (_, index) => ({ run_id: `run-${index}`, device_id: `DEV-${index}` }));
  const observed = new Set();
  const fetchImpl = liveApi(devices, clock, (url, _options, device) => {
    if (url.pathname !== "/api/health") return;
    return (async () => {
      active++; maximum = Math.max(maximum, active);
      if (polling) pollMaximum = Math.max(pollMaximum, active);
      observed.add(device.device_id); await tick(); active--;
      return json(health(device, [windowRow(20 + devices.indexOf(device))]));
    })();
  });
  const actual = liveStore(clock, fetchImpl);
  await actual.init();
  assert.equal(observed.size, 10);
  assert.equal(Object.values(actual.getState().previews).filter((preview) => preview.status === "ready").length, 10);
  assert.ok(maximum <= 4 && maximum >= 2);
  polling = true; observed.clear(); await clock.advance(2000);
  for (let turn = 0; turn < 10 && observed.size < 10; turn++) await tick();
  await tick();
  assert.equal(observed.size, 10); assert.ok(pollMaximum <= 3);
  actual.destroy(); assert.equal(clock.pendingCount, 0);
});

test("slow live requests do not overlap polling rounds or create an interval backlog", async () => {
  const clock = fakeTime(), late = deferred(); let liveReads = 0;
  const store = liveStore(clock, liveApi([A], clock, (url) => {
    if (url.pathname === "/api/live" && ++liveReads > 1) return late.promise;
  }));
  await store.init(); await clock.advance(2000);
  assert.equal(liveReads, 2);
  await clock.advance(3000); assert.equal(liveReads, 2);
  late.resolve(json(liveSnapshot([A], clock))); await tick();
  await clock.advance(1999); assert.equal(liveReads, 2);
  store.destroy();
});

test("latest view follows real windows while report view stays fixed and offers explicit return to latest", async () => {
  const clock = fakeTime(); let seq = 10, reports = 0;
  const store = liveStore(clock, liveApi([A], clock, (url) => {
    if (url.pathname === "/api/health") return json(health(A, [windowRow(seq)]));
    if (url.pathname === "/api/report") { reports++; return json(report(A)); }
  }));
  await store.init(); seq = 20; await clock.advance(2000);
  assert.equal(latestWindow(store.getState().windows).start_seq, 20);
  assert.equal(reports, 1);
  store.setView("reports"); const shown = store.getState().report;
  seq = 30; await clock.advance(2000);
  assert.equal(latestWindow(store.getState().windows).start_seq, 20);
  assert.equal(store.getState().report, shown); assert.equal(reports, 1);
  assert.equal(store.getState().newDataAvailable, true);
  await store.returnLatest();
  assert.equal(store.getState().view, "health");
  assert.equal(latestWindow(store.getState().windows).start_seq, 30);
  assert.equal(store.getState().newDataAvailable, false);
  store.destroy();
});

test("history pagination survives live polling without changing windows, cursors or report", async () => {
  const clock = fakeTime(); let seq = 20;
  const store = liveStore(clock, liveApi([A], clock, (url) => url.pathname === "/api/health"
    ? json(url.searchParams.has("windows_cursor") ? health(A, [windowRow(10)], { wc: "older-2" })
      : health(A, [windowRow(seq)], { wc: "older-1" })) : undefined));
  await store.init(); await store.loadWindows();
  const state = store.getState(), windows = state.windows, shown = state.report;
  seq = 30; await clock.advance(2000);
  assert.equal(store.getState().windows, windows);
  assert.equal(store.getState().windowCursor, "older-2");
  assert.equal(store.getState().report, shown);
  assert.equal(store.getState().historyPinned, true);
  assert.equal(store.getState().newDataAvailable, true);
  assert.equal(store.getState().previews[deviceKey(A)].latest.start_seq, 30);
  store.destroy();
});

test("manual window inspection is preserved until the user returns to the latest snapshot", async () => {
  const clock = fakeTime(); let seq = 20;
  const store = liveStore(clock, liveApi([A], clock, (url) => url.pathname === "/api/health"
    ? json(health(A, [windowRow(seq)])) : undefined));
  await store.init(); store.inspect(20); seq = 30; await clock.advance(2000);
  assert.equal(store.getState().inspectedSeq, 20);
  assert.equal(latestWindow(store.getState().windows).start_seq, 20);
  assert.equal(store.getState().newDataAvailable, true);
  await store.resetWindows();
  assert.equal(latestWindow(store.getState().windows).start_seq, 30);
  assert.equal(store.getState().inspectedSeq, null);
  store.destroy();
});

test("hiding the page aborts background health requests and an ignored abort cannot repaint selected data", async () => {
  const clock = fakeTime(), late = deferred(); let reads = 0, activeSignal;
  const store = liveStore(clock, liveApi([A], clock, (url, options) => {
    if (url.pathname === "/api/health" && ++reads === 2) { activeSignal = options.signal; return late.promise; }
    if (url.pathname === "/api/health") return json(health(A, [windowRow(reads === 1 ? 10 : 30)]));
  }));
  await store.init(); await clock.advance(2000);
  clock.setVisibility("hidden"); await tick();
  assert.equal(activeSignal.aborted, true);
  assert.equal(store.getState().pollPaused, true);
  assert.equal(deviceStatus(store.getState(), A).level, "unknown");
  late.resolve(json(health(A, [windowRow(20)]))); await tick();
  assert.equal(latestWindow(store.getState().windows).start_seq, 10);
  const count = reads; await clock.advance(10000); assert.equal(reads, count);
  clock.setVisibility("visible"); await clock.advance(0);
  assert.equal(latestWindow(store.getState().windows).start_seq, 30);
  assert.equal(store.getState().pollPaused, false);
  store.destroy();
});

test("destroy removes visibility hooks and timers and ignores late in-flight live responses", async () => {
  const clock = fakeTime(), late = deferred(); let liveReads = 0, signal, emissions = 0;
  const store = liveStore(clock, liveApi([A], clock, (url, options) => {
    if (url.pathname === "/api/live" && ++liveReads === 2) { signal = options.signal; return late.promise; }
  }));
  store.subscribe(() => emissions++);
  await store.init(); await clock.advance(2000);
  store.destroy(); const count = emissions;
  assert.equal(signal.aborted, true); assert.equal(clock.pendingCount, 0);
  late.resolve(json(liveSnapshot([A], clock))); await tick();
  clock.setVisibility("hidden"); clock.setVisibility("visible"); await clock.advance(10000);
  assert.equal(emissions, count); assert.equal(liveReads, 2);
});

test("a failed live preview removes green active-state presentation and a later round recovers", async () => {
  const clock = fakeTime(); let fail = false;
  const store = liveStore(clock, liveApi([A], clock, (url) => url.pathname === "/api/health" && fail ? json({}, 503) : undefined));
  await store.init(); assert.equal(deviceStatus(store.getState(), A).level, "healthy");
  fail = true; await clock.advance(2000);
  assert.equal(deviceStatus(store.getState(), A).level, "error");
  assert.equal(store.getState().healthStatus, "error");
  fail = false; await clock.advance(2000);
  assert.equal(deviceStatus(store.getState(), A).level, "healthy");
  assert.equal(store.getState().healthStatus, "ready");
  store.destroy();
});

test("freshness expires independently of slow polling and stopped or errored acquisition cannot remain green", async () => {
  const clock = fakeTime();
  const store = liveStore(clock, liveApi([A], clock), { pollIntervalMs: 20000 });
  await store.init(); await clock.advance(LIVE_STALE_MS + 1);
  assert.equal(liveStatus(store.getState()).level, "unknown");
  assert.equal(deviceStatus(store.getState(), A).level, "unknown");
  const current = store.getState();
  const stopped = { ...current, clock: clock.now(), live: liveSnapshot([A], clock, { status: "stopped" }) };
  assert.equal(deviceStatus(stopped, A).level, "unknown");
  const bad = { ...current, live: liveSnapshot([A], clock, { status: "error", error: "capture failed" }) };
  assert.equal(liveStatus(bad).level, "error");
  assert.equal(deviceStatus(bad, A).level, "error");
  store.destroy();
});

test("fresh heartbeat does not conceal an expired device commit or queue backlog", async () => {
  const clock = fakeTime();
  const store = liveStore(clock, liveApi([A], clock)); await store.init();
  const current = store.getState(), live = liveSnapshot([A], clock);
  live.devices[0].last_commit_at_utc = new Date(clock.now() - LIVE_STALE_MS - 1).toISOString();
  live.devices[0].queue_depth = 4;
  const delayed = { ...current, live };
  assert.equal(liveStatus(delayed).level, "yellow");
  assert.equal(deviceStatus(delayed, A).level, "unknown");
  live.devices[0].error = "analysis failed";
  assert.equal(deviceStatus(delayed, A).level, "error");
  store.destroy();
});

test("bounded request timeout clears stale green presentation and later polling can retry", async () => {
  const clock = fakeTime(), never = deferred(); let fail = true, signal;
  const store = liveStore(clock, liveApi([A], clock, (url, options) => {
    if (url.pathname === "/api/health" && fail) { signal = options.signal; return never.promise; }
  }));
  const initial = store.init(); await tick(); await clock.advance(6000); await initial;
  assert.equal(signal.aborted, true);
  assert.equal(store.getState().healthStatus, "error");
  assert.match(store.getState().healthError, /超时/);
  fail = false; await clock.advance(2000);
  assert.equal(store.getState().healthStatus, "ready");
  assert.equal(deviceStatus(store.getState(), A).level, "healthy");
  never.resolve(json(health(A, [windowRow(1)]))); await tick();
  assert.equal(latestWindow(store.getState().windows).start_seq, 20);
  store.destroy();
});

test("invalid live metrics are visible failures rather than fabricated counters or healthy status", async () => {
  const clock = fakeTime(); const invalid = liveSnapshot([A], clock);
  invalid.devices[0].captured_samples = -1;
  const store = liveStore(clock, liveApi([A], clock, (url) => url.pathname === "/api/live" ? json(invalid) : undefined));
  await store.init();
  assert.equal(store.getState().liveMode, "error");
  assert.match(store.getState().liveError, /格式/);
  assert.equal(deviceStatus(store.getState(), A).level, "error");
  store.destroy();
});


test("a manual device selection during a slow initial live check remains authoritative", async () => {
  const clock = fakeTime(), late = deferred();
  const store = liveStore(clock, liveApi([A, B], clock, (url) => url.pathname === "/api/live" ? late.promise : undefined));
  const initialization = store.init(); await tick();
  await store.selectDevice(deviceKey(B));
  late.resolve(json(liveSnapshot([A, B], clock))); await initialization;
  assert.equal(store.getState().selected.device_id, B.device_id);
  assert.equal(store.getState().report.device_id, B.device_id);
  store.destroy();
});

test("a missing commit timestamp with written windows is never presented as fresh healthy data", async () => {
  const clock = fakeTime(); const snapshot = liveSnapshot([A], clock);
  snapshot.devices[0].last_commit_at_utc = null;
  const store = liveStore(clock, liveApi([A], clock, (url) => url.pathname === "/api/live" ? json(snapshot) : undefined));
  await store.init();
  assert.equal(deviceStatus(store.getState(), A).level, "unknown");
  assert.match(deviceStatus(store.getState(), A).label, /更新过期/);
  store.destroy();
});


test("a live snapshot missing a catalog device cannot inherit that device's healthy preview", async () => {
  const clock = fakeTime();
  const store = liveStore(clock, liveApi([A, B], clock, (url) =>
    url.pathname === "/api/live" ? json(liveSnapshot([A], clock)) : undefined));
  await store.init();
  assert.equal(store.getState().previews[deviceKey(B)].active.level, "healthy");
  assert.equal(deviceStatus(store.getState(), B).level, "unknown");
  assert.match(deviceStatus(store.getState(), B).label, /缺少此设备/);
  await store.selectDevice(deviceKey(B));
  assert.equal(selectedDeviceStatus(store.getState()).level, "unknown");
  const checking = { ...store.getState(), liveMode: "checking", live: null };
  assert.equal(deviceStatus(checking, B).level, "unknown");
  store.destroy();
});

test("a live endpoint disappearing after confirmation remains an error and is retried instead of becoming legacy", async () => {
  const clock = fakeTime(); let liveReads = 0;
  const store = liveStore(clock, liveApi([A], clock, (url) => {
    if (url.pathname === "/api/live" && ++liveReads === 2) return json({}, 404);
  }));
  await store.init(); await clock.advance(2000);
  assert.equal(store.getState().liveMode, "available");
  assert.ok(store.getState().liveError);
  assert.equal(deviceStatus(store.getState(), A).level, "error");
  assert.equal(selectedDeviceStatus(store.getState()).level, "error");
  await clock.advance(2000);
  assert.equal(liveReads, 3);
  assert.equal(store.getState().liveError, "");
  assert.equal(deviceStatus(store.getState(), A).level, "healthy");
  store.destroy();
});

test("only a first completed live check may use legacy 404 fallback", async () => {
  const clock = fakeTime(); let liveReads = 0;
  const store = liveStore(clock, liveApi([A], clock, (url) => url.pathname === "/api/live"
    ? json({}, ++liveReads === 1 ? 503 : 404) : undefined));
  await store.init(); await clock.advance(2000);
  assert.equal(store.getState().liveMode, "error");
  assert.ok(store.getState().liveError);
  assert.equal(selectedDeviceStatus(store.getState()).level, "error");
  await clock.advance(2000); assert.equal(liveReads, 3);
  store.destroy();
});

test("initial loading in a hidden page defers live, selected data and all device previews until foreground", async () => {
  const clock = fakeTime(); clock.setVisibility("hidden");
  const devices = Array.from({ length: 10 }, (_, index) => ({ run_id: `hidden-${index}`, device_id: `DEV-${index}` }));
  const urls = [];
  const base = liveApi(devices, clock);
  const store = liveStore(clock, (url, options) => { urls.push(url); return base(url, options); });
  await store.init(); await clock.advance(10000);
  assert.deepEqual(urls, ["/api/catalog"]);
  assert.equal(store.getState().pollPaused, true);
  clock.setVisibility("visible"); await clock.advance(0);
  for (let turn = 0; turn < 15 && Object.keys(store.getState().previews).length < 10; turn++) await tick();
  assert.equal(Object.values(store.getState().previews).filter((row) => row.status === "ready").length, 10);
  assert.equal(store.getState().reportStatus, "ready");
  assert.equal(urls.filter((url) => url === "/api/live").length, 1);
  store.destroy();
});

test("hiding during catalog loading prevents the following automatic requests until foreground", async () => {
  const clock = fakeTime(), catalog = deferred(); let reads = 0;
  const base = liveApi([A, B], clock);
  const store = liveStore(clock, (url, options) => {
    if (url === "/api/catalog") return catalog.promise;
    reads++; return base(url, options);
  });
  const initial = store.init(); clock.setVisibility("hidden");
  catalog.resolve(json({ devices: [A, B], readonly: true })); await initial;
  assert.equal(reads, 0);
  clock.setVisibility("visible"); await clock.advance(0);
  assert.equal(Object.keys(store.getState().previews).length, 2);
  assert.equal(store.getState().healthStatus, "ready");
  store.destroy();
});

test("initial preview workers abort on hide, do not launch the remaining devices, and resume without blocking a manual selection", async () => {
  const clock = fakeTime(); const deferredPreviews = [], requested = [];
  const devices = Array.from({ length: 10 }, (_, index) => ({ run_id: `resume-${index}`, device_id: `DEV-${index}` }));
  let hold = true;
  const store = liveStore(clock, liveApi(devices, clock, (url, options, device) => {
    if (url.pathname !== "/api/health") return;
    requested.push(device.device_id);
    if (hold && device !== devices[0]) {
      const pending = deferred(); deferredPreviews.push({ ...pending, signal: options.signal, device }); return pending.promise;
    }
  }));
  const initial = store.init(); await tick();
  assert.equal(deferredPreviews.length, 3);
  clock.setVisibility("hidden"); await initial;
  assert.ok(deferredPreviews.every((row) => row.signal.aborted));
  assert.equal(requested.length, 4);
  hold = false;
  // Explicit selection is allowed while automatic work is paused.
  await store.selectDevice(deviceKey(devices[9]));
  assert.equal(store.getState().selected.device_id, devices[9].device_id);
  const count = requested.length; await clock.advance(5000);
  assert.equal(requested.length, count);
  for (const row of deferredPreviews) row.resolve(json(health(row.device, [windowRow(1)])));
  await tick();
  assert.equal(Object.keys(store.getState().previews).length, 2);
  clock.setVisibility("visible"); await clock.advance(0);
  for (let turn = 0; turn < 15 && Object.keys(store.getState().previews).length < 10; turn++) await tick();
  assert.equal(Object.values(store.getState().previews).filter((row) => row.status === "ready").length, 10);
  assert.equal(store.getState().selected.device_id, devices[9].device_id);
  store.destroy();
});

test("hiding during initial selected reads cancels automatic details and foreground reattempts them", async () => {
  const clock = fakeTime(), lateHealth = deferred(), lateReport = deferred(); const signals = [];
  let hold = true;
  const store = liveStore(clock, liveApi([A], clock, (url, options) => {
    if (hold && ["/api/health", "/api/report"].includes(url.pathname)) {
      signals.push(options.signal); return url.pathname === "/api/health" ? lateHealth.promise : lateReport.promise;
    }
  }));
  const initial = store.init(); await tick(); clock.setVisibility("hidden"); await initial;
  assert.ok(signals.every((signal) => signal.aborted));
  assert.equal(store.getState().healthStatus, "idle");
  hold = false; clock.setVisibility("visible"); await clock.advance(0);
  assert.equal(store.getState().healthStatus, "ready");
  assert.equal(store.getState().reportStatus, "ready");
  lateHealth.resolve(json(health(A, [windowRow(1)]))); lateReport.resolve(json(report(A, { passed: true })));
  await tick();
  assert.equal(latestWindow(store.getState().windows).start_seq, 20);
  assert.equal(store.getState().report.passed, null);
  store.destroy();
});

for (const frozen of ["history", "full", "reports"]) {
  test(`failed polling immediately removes the detail badge's green state while preserving frozen ${frozen}`, async () => {
    const clock = fakeTime(); let fail = false;
    const store = liveStore(clock, liveApi([A], clock, (url) => {
      if (url.pathname !== "/api/health") return;
      if (fail) return json({}, 503);
      return json(url.searchParams.has("windows_cursor") ? health(A, [windowRow(10)])
        : health(A, [windowRow(20)], { wc: "older" }));
    }));
    await store.init();
    if (frozen === "history") await store.loadWindows();
    else if (frozen === "full") await store.loadAllWindows();
    else store.setView("reports");
    const held = store.getState(), windows = held.windows, reportSnapshot = held.report;
    assert.equal(selectedDeviceStatus(held).level, "healthy");
    fail = true; await clock.advance(2000);
    assert.equal(store.getState().healthStatus, "ready");
    assert.equal(store.getState().windows, windows);
    assert.equal(store.getState().report, reportSnapshot);
    assert.equal(store.getState().windowCursor, held.windowCursor);
    assert.equal(store.getState().view, held.view);
    assert.equal(deviceStatus(store.getState(), A).level, "error");
    assert.equal(selectedDeviceStatus(store.getState()).level, "error");
    assert.equal(clock.now() - held.detailReadAt, 2000);
    store.destroy();
  });
}
