import test from "node:test";
import assert from "node:assert/strict";
import { createConsoleStore, deviceKey, mergeWindows, mergeAlarms, latestWindow,
  chronological, activeStatus, reportVerdict, detectionText, filterAlarms, apiUrl, plotLayout } from "../../src/g15/console/static/state.js";

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
    const device = devices.find((item) => item.run_id === parsed.searchParams.get("run_id"));
    if (parsed.pathname === "/api/report") return handler?.(parsed, options, device) || json(report(device));
    return handler(parsed, options, device);
  };
}

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
