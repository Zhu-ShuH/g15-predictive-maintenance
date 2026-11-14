import test from "node:test";
import assert from "node:assert/strict";
import { createLiveRiskController, normalizeRiskSnapshot, normalizeRiskHealth, riskRequest, riskControlState,
  riskEventPresentation, riskHealthPresentation, healthTrendGeometry } from "../../src/g15/console/static/live-risk.js";
import { deviceKey } from "../../src/g15/console/static/state.js";

const NOW = Date.parse("2026-11-14T15:00:00Z");
const A = { run_id: "run-a", device_id: "LIVE-001" }, B = { run_id: "run-b", device_id: "LIVE-002" };
const event = (extra = {}) => ({ event_id: "event-1", fault_type: "step", amplitude: 4, delay_seconds: 2, duration_seconds: 4,
  start_seq: 512, end_seq_exclusive: 1536, duration_points: 1024, created_at_utc: new Date(NOW).toISOString(),
  status: "scheduled", captured_points: 0, processed_points: 0, cancel_seq: null, cancelled_at_utc: null, cancel_reason: null, ...extra });
const live = (extra = {}) => ({ session_id: "session-1", status: "running", updated_at_utc: new Date(NOW).toISOString(), sample_rate_hz: 256,
  devices: [A, B].map((pair) => ({ ...pair, capture_paused: false, captured_samples: 512, processed_samples: 512,
    windows_written: 2, alarms_written: 7, last_commit_at_utc: new Date(NOW).toISOString(), error: null })), ...extra });
const risk = (extra = {}) => ({ schema_version: "1", session_id: "session-1", status: "running", version: 1, controls_available: true,
  devices: [A, B].map((pair) => ({ ...pair, mode: "auto", capture_paused: false, event: null })), history: [], history_count: 0, limits: {}, ...extra });
const health = (device = A, score = 83, active = "yellow") => ({ ...device,
  windows: { items: [{ start_seq: 256, end_seq_exclusive: 512, sample_count: 256, health_score: score }, { start_seq: 0, end_seq_exclusive: 256, sample_count: 256, health_score: 98 }], next_cursor: null },
  alarms: { items: [{ start_seq: 0, rule_id: "rule", level: "yellow" }], next_cursor: "older" }, states: [{ state: { active_level: active } }] });
const state = (extra = {}) => ({ live: live(), liveAt: NOW, liveFailed: false, liveBusy: false, risk: risk(), riskAt: NOW, riskError: "", selected: A,
  health: health(), healthAt: NOW, healthError: "", hidden: false, busy: false, pending: null, ...extra });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const tick = () => new Promise((done) => setImmediate(done));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
function fakeTimers() {
  let next = 0; const pending = new Map();
  return { setTimeout(callback, delay) { const id = ++next; pending.set(id, { callback, delay }); return id; }, clearTimeout(id) { pending.delete(id); },
    fire(delay) { for (const [id, item] of [...pending]) if (item.delay === delay) { pending.delete(id); item.callback(); } }, count: () => pending.size };
}
function visibility() {
  const listeners = new Set(); return { hidden: false, addEventListener(_name, fn) { listeners.add(fn); }, removeEventListener(_name, fn) { listeners.delete(fn); },
    setHidden(value) { this.hidden = value; for (const listener of listeners) listener(); } };
}
function harness(t, handler) {
  const timers = fakeTimers(), source = visibility(), calls = [];
  const controller = createLiveRiskController({ now: () => NOW, timers, visibilitySource: source, makeRequestId: () => "request-1",
    fetchImpl: async (url, options) => { calls.push({ url, options }); const answer = handler?.(url, options); if (answer !== undefined) return answer;
      return json(url === "/api/live/risk" ? risk() : health(new URL(url, "http://localhost").searchParams.get("device_id") === B.device_id ? B : A)); } });
  t.after(() => controller.destroy());
  return { controller, timers, source, calls };
}

test("risk parameters enforce the public units and preserve exact identity without injecting clear parameters", () => {
  const identity = { session_id: "session-1", ...A }, input = { fault_type: "burst", amplitude: "4", delay_seconds: "0", duration_seconds: "0.012" };
  assert.deepEqual(riskRequest("inject", identity, input, "id-1", 256), { ...identity, request_id: "id-1", action: "inject", fault_type: "burst", amplitude: 4, delay_seconds: 0, duration_seconds: .012 });
  assert.deepEqual(riskRequest("clear", identity, input, "id-1", 256), { ...identity, request_id: "id-1", action: "clear" });
  for (const [key, value] of [["amplitude", ""], ["amplitude", 0], ["amplitude", 11], ["amplitude", Infinity], ["delay_seconds", -1], ["delay_seconds", 301], ["duration_seconds", 0], ["duration_seconds", 301], ["duration_seconds", .001], ["fault_type", "unknown"]]) assert.throws(() => riskRequest("inject", identity, { ...input, [key]: value }, "id", 256));
  assert.throws(() => riskRequest("inject", identity, input, "id", null));
});

test("risk and health responses reject cross-session or cross-device data and invalid progress", () => {
  assert.equal(normalizeRiskSnapshot(risk(), "session-1").version, 1);
  assert.throws(() => normalizeRiskSnapshot(risk(), "session-2"), { code: "live_risk_session_changed" });
  assert.throws(() => normalizeRiskSnapshot(risk({ devices: [risk().devices[0], risk().devices[0]] })), { code: "invalid_response" });
  assert.throws(() => normalizeRiskSnapshot(risk({ devices: [{ ...risk().devices[0], event: event({ captured_points: 5, processed_points: 6 }) }] })), { code: "invalid_response" });
  assert.throws(() => normalizeRiskHealth(health(B), A), { code: "invalid_health_response" });
  assert.throws(() => normalizeRiskHealth(health(A, NaN), A), { code: "invalid_health_response" });
});

test("paused devices accept a scheduled risk while terminal, stale and unavailable controls remain disabled", () => {
  const paused = live(); paused.devices[0].capture_paused = true;
  assert.equal(riskControlState(state({ live: paused }), NOW).inject, true);
  for (const extra of [{ live: live({ status: "stopped" }) }, { liveFailed: true }, { hidden: true }, { riskError: "unavailable" }, { liveBusy: true }, { riskAt: NOW - 10001 }, { live: live({ updated_at_utc: new Date(NOW - 10001).toISOString() }) }, { risk: risk({ controls_available: false }) }]) assert.equal(riskControlState(state(extra), NOW).inject, false);
  const active = risk(); active.devices[0] = { ...active.devices[0], mode: "normal", event: event({ status: "active" }) };
  const controls = riskControlState(state({ risk: active }), NOW);
  assert.equal(controls.inject, false); assert.equal(controls.clear, true); assert.equal(controls.automatic, true);
  assert.equal(riskControlState(state(), NOW).clear, true);
});

test("risk progress uses recorded samples and never reports recovery as a healthy result", () => {
  const data = { mode: "normal", event: event() };
  assert.equal(riskEventPresentation(data, { capture_paused: true, captured_samples: 256 }, 256).label, "已暂停 · 待采样");
  assert.match(riskEventPresentation(data, { capture_paused: true, captured_samples: 256 }, 256).counts, /距开始 256 点 \/ 1 秒/);
  data.event = event({ status: "recovering", captured_points: 1024, processed_points: 512 });
  assert.equal(riskEventPresentation(data, {}, 256).progress, .5); assert.equal(riskEventPresentation(data, {}, 256).label, "等待分析完成");
  data.event = event({ status: "completed", captured_points: 1024, processed_points: 1024 });
  assert.equal(riskEventPresentation(data, {}, 256).label, "注入样本已分析");
  data.event = event({ status: "cancelled", cancel_reason: "session_stopped" });
  assert.equal(riskEventPresentation(data, {}, 256).label, "会话结束 · 注入中止");
});

test("health uses committed query scores and activity, and historical counts are never a page length", () => {
  const data = risk(); data.devices[0] = { ...data.devices[0], event: event({ status: "completed", captured_points: 1024, processed_points: 1024 }) };
  let model = riskHealthPresentation(state({ risk: data, health: health(A, 12.5, "red") }), NOW);
  assert.equal(model.score, "12.5"); assert.equal(model.alarm.level, "red"); assert.equal(model.alarmCount, "7");
  model = riskHealthPresentation(state({ health: health(A, 99, null) }), NOW);
  assert.equal(model.score, "99"); assert.equal(model.alarm.level, "healthy");
  const paused = live(); paused.devices[0].capture_paused = true; paused.devices[0].last_commit_at_utc = new Date(NOW - 60000).toISOString();
  model = riskHealthPresentation(state({ live: paused }), NOW);
  assert.equal(model.stale, false); assert.equal(model.captured, "采集已暂停"); assert.match(model.range, /已存数据/);
  paused.devices[0].capture_paused = false;
  assert.equal(riskHealthPresentation(state({ live: paused }), NOW).stale, true);
  model = riskHealthPresentation(state({ liveFailed: true }), NOW);
  assert.equal(model.alarm.level, "unknown"); assert.match(model.alarm.label, /已存/); assert.match(model.captured, /过期/);
});

test("trend geometry preserves sequence gaps, zero scores and a lone committed window", () => {
  const rows = [{ start_seq: 0, end_seq_exclusive: 10, health_score: 0 }, { start_seq: 20, end_seq_exclusive: 30, health_score: 50 }];
  const geometry = healthTrendGeometry(rows, 280, 190);
  assert.equal((geometry.path.match(/M/g) || []).length, 2); assert.equal(geometry.ticks[0].value, 0);
  assert.equal(geometry.start, 0); assert.equal(geometry.end, 20); assert.ok(healthTrendGeometry(rows.slice(0, 1)).point);
  assert.equal(healthTrendGeometry([]), null);
  assert.deepEqual(healthTrendGeometry(rows, 280, 190, { start_seq: 10, end_seq_exclusive: 30, captured_points: 10 }).markers.map((marker) => [marker.label, marker.seq]), [["注入", 10]]);
  assert.deepEqual(healthTrendGeometry(rows, 280, 190, { start_seq: 0, end_seq_exclusive: 30, status: "cancelled", cancel_seq: 20, captured_points: 20 }).markers.map((marker) => [marker.label, marker.seq]), [["注入", 0], ["清除", 20]]);
  assert.deepEqual(healthTrendGeometry(rows, 280, 190, { start_seq: 10, end_seq_exclusive: 30, status: "cancelled", cancel_seq: 0, captured_points: 0 }).markers, []);
});

test("controller requests actual health with selected pair and sends the precise same-origin risk contract", async (t) => {
  const { controller, calls } = harness(t, (_url, options) => {
    if (options.method === "POST") { const body = JSON.parse(options.body); return json({ ...risk({ version: 2 }), request: { request_id: body.request_id, action: body.action, event_id: "event-1", replayed: false } }); }
  });
  controller.updateLive(live(), { receivedAt: NOW }); await tick();
  assert.equal(controller.getState().health.device_id, A.device_id);
  const query = new URL(calls.find((call) => call.url.startsWith("/api/health")).url, "http://localhost");
  assert.equal(query.searchParams.get("page_size"), "60"); assert.equal(query.searchParams.get("run_id"), A.run_id);
  await controller.submit("inject", { fault_type: "step", amplitude: 4, delay_seconds: 2, duration_seconds: 8 }); await tick();
  const call = calls.find((entry) => entry.options.method === "POST"), body = JSON.parse(call.options.body);
  assert.equal(call.url, "/api/live/risk"); assert.equal(call.options.headers["X-G15-Console-Action"], "live-risk"); assert.equal(call.options.credentials, "same-origin");
  assert.deepEqual(body, { session_id: "session-1", ...A, request_id: "request-1", action: "inject", fault_type: "step", amplitude: 4, delay_seconds: 2, duration_seconds: 8 });
  assert.equal(controller.getState().pending, null); assert.equal(controller.getState().risk.version, 2);
});

test("late responses from a previous selected device cannot replace the new device", async (t) => {
  const old = deferred();
  const { controller } = harness(t, (url) => url.includes("run_id=run-a") ? old.promise : undefined);
  controller.updateLive(live(), { receivedAt: NOW }); controller.select(deviceKey(B)); await tick();
  assert.equal(controller.getState().health.device_id, B.device_id);
  old.resolve(json(health(A, 1, "red"))); await tick();
  assert.equal(controller.getState().health.device_id, B.device_id); assert.equal(controller.getState().health.windows.items[0].health_score, 83);
});

test("session changes discard old commands and reject late results", async (t) => {
  const old = deferred(); let secondSession = false;
  const { controller } = harness(t, (url, options) => {
    if (options.method === "POST") return old.promise;
    if (url === "/api/live/risk") return json(risk({ session_id: secondSession ? "session-2" : "session-1" }));
  });
  controller.updateLive(live(), { receivedAt: NOW }); await tick();
  const saving = controller.submit("clear"); await tick();
  secondSession = true; controller.updateLive(live({ session_id: "session-2" }), { receivedAt: NOW }); await tick();
  old.resolve(json({ ...risk({ version: 10 }), request: { request_id: "request-1", action: "clear" } })); await saving; await tick();
  assert.equal(controller.getState().risk.session_id, "session-2"); assert.equal(controller.getState().pending, null); assert.equal(controller.getState().busy, false);
  assert.match(controller.getState().error, /上一请求/);
});

test("a timed-out write is never resubmitted automatically and manual retry preserves its complete body", async (t) => {
  const pending = deferred(), bodies = []; let fail = true;
  const { controller, timers } = harness(t, (_url, options) => {
    if (options.method === "POST") {
      const body = JSON.parse(options.body); bodies.push(body);
      return fail ? pending.promise : json({ ...risk({ version: 2 }), request: { request_id: body.request_id, action: body.action, replayed: true, event_id: null } });
    }
  });
  controller.updateLive(live(), { receivedAt: NOW }); await tick();
  const saving = controller.submit("clear"); await tick(); timers.fire(6000); await saving; await tick();
  assert.equal(bodies.length, 1); assert.equal(controller.getState().pending.request_id, "request-1"); assert.match(controller.getState().error, /尚未确认/);
  await controller.refresh(); assert.equal(bodies.length, 1);
  controller.select(deviceKey(B)); assert.equal(controller.getState().selected.device_id, A.device_id);
  fail = false; await controller.retry(); await tick();
  assert.equal(bodies.length, 2); assert.deepEqual(bodies[1], bodies[0]); assert.equal(controller.getState().pending, null); assert.match(controller.getState().feedback, /已确认原请求/);
  pending.resolve(json(risk({ version: 99 }))); await tick(); assert.equal(controller.getState().risk.version, 2);
});

test("explicit request-ID conflicts do not leave an ambiguous write pending", async (t) => {
  const { controller } = harness(t, (_url, options) => options.method === "POST" ? json({ error: "live_risk_request_conflict" }, 409) : undefined);
  controller.updateLive(live(), { receivedAt: NOW }); await tick(); await controller.submit("clear"); await tick();
  assert.equal(controller.getState().pending, null); assert.match(controller.getState().error, /标识.*冲突/);
});

test("old or unavailable risk services preserve independently read health and disable risk mutations", async (t) => {
  for (const status of [404, 503]) {
    const { controller, calls } = harness(t, (url) => url === "/api/live/risk" ? json({ error: status === 503 ? "live_risk_unavailable" : "not_found" }, status) : undefined);
    controller.updateLive(live(), { receivedAt: NOW }); await tick();
    assert.equal(controller.getState().health.windows.items[0].health_score, 83); assert.ok(controller.getState().riskError);
    assert.equal(riskControlState(controller.getState(), NOW).inject, false);
    await controller.submit("clear"); assert.equal(calls.filter((call) => call.options.method === "POST").length, 0);
    controller.destroy();
  }
});

test("hidden pages stop polling and destroy cancels reads without late updates", async (t) => {
  const pending = deferred(); let hold = false;
  const { controller, timers, source, calls } = harness(t, () => hold ? pending.promise : undefined);
  controller.updateLive(live(), { receivedAt: NOW }); await tick();
  source.setHidden(true); const count = calls.length; timers.fire(2000); await tick(); assert.equal(calls.length, count);
  hold = true; source.setHidden(false); await tick();
  const requests = calls.slice(count); assert.equal(requests.length, 2);
  let observed = 0; controller.subscribe(() => { observed += 1; }); controller.destroy(); const afterDestroy = observed;
  assert.ok(requests.every((request) => request.options.signal.aborted)); assert.equal(timers.count(), 0);
  pending.resolve(json(risk())); await tick(); assert.equal(observed, afterDestroy);
});
