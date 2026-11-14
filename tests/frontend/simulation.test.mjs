import test from "node:test";
import assert from "node:assert/strict";
import { sumCounter, sampleRates, deviceState, snapshotIsStale, simulationStatus } from "../../src/g15/console/static/simulation.js";
import { liveStatus, deviceStatus, deviceKey } from "../../src/g15/console/static/state.js";

const device = {run_id: "run-1", device_id: "LIVE-001", captured_samples: 100, processed_samples: 80, queue_depth: 1, capture_paused: false};
const snapshot = {session_id: "s1", status: "running", devices: [device]};
test("flow rates use actual counter deltas and preserve unknown data", () => {
  assert.deepEqual(sampleRates(snapshot, {...snapshot, devices: [{...device, captured_samples: 140, processed_samples: 120}]}, 2), {capture: 20, process: 20});
  assert.equal(sumCounter([], "captured_samples"), null);
  assert.equal(sumCounter([{...device, captured_samples: null}], "captured_samples"), null);
  assert.equal(sampleRates(snapshot, {...snapshot, session_id: "s2"}, 2), null);
  assert.equal(sampleRates(snapshot, {...snapshot, devices: [{...device, captured_samples: 0}]}, 2), null);
  assert.equal(sampleRates(snapshot, snapshot, 11), null);
});
test("paused capture, queue draining, errors and terminal sessions remain distinct", () => {
  assert.match(deviceState({...device, capture_paused: true}, snapshot).label, /排空/);
  assert.match(deviceState({...device, capture_paused: true, queue_depth: 0}, snapshot).label, /已停止采集/);
  assert.equal(deviceState(device, snapshot, true).key, "stale");
  assert.equal(deviceState({...device, error: "failed"}, snapshot).key, "error");
  assert.equal(deviceState(device, {...snapshot, status: "stopped"}).key, "stopped");
});
test("health views identify intentional pauses without hiding stale service state", () => {
  const now = Date.now();
  const paused = {...device, capture_paused: true, windows_written: 1, last_commit_at_utc: new Date(now - 20000).toISOString()};
  const state = {clock: now, liveMode: "live", live: {...snapshot, updated_at_utc: new Date(now).toISOString(), devices: [paused]}, previews: {[deviceKey(device)]: {status: "ready", active: {level:"healthy",label:"无活动告警"}, readAt: now, latest: {start_seq:0}}}};
  assert.match(liveStatus(state).label, /暂停/);
  assert.match(deviceStatus(state, device).label, /暂停/);
  const stale = {...state, clock: now + 11000};
  assert.match(deviceStatus(stale, device).label, /过期/);
});

test("fresh HTTP reads cannot refresh a stalled heartbeat when every device is paused", () => {
  const now = Date.now();
  const paused = {...snapshot, devices: [{...device, capture_paused: true}], updated_at_utc: new Date(now - 11000).toISOString()};
  assert.equal(snapshotIsStale(paused, now, now), true);
  assert.equal(snapshotIsStale(paused, now + 2000, now + 2000), true);
  assert.equal(snapshotIsStale({...paused, updated_at_utc: new Date(now).toISOString()}, now, now), false);
  assert.equal(snapshotIsStale({...paused, updated_at_utc: null}, now, now), true);
  assert.equal(snapshotIsStale({...paused, updated_at_utc: new Date(now).toISOString()}, now - 11000, now), true);
});

test("device and session errors remain errors even when all capture is paused", () => {
  const paused = {...snapshot, devices: [{...device, capture_paused: true, queue_depth: 0}]};
  assert.equal(simulationStatus(paused).level, "unknown");
  const deviceFailure = {...paused, devices: [{...paused.devices[0], error: "capture failed"}]};
  assert.equal(simulationStatus(deviceFailure).level, "error");
  assert.match(simulationStatus(deviceFailure).label, /异常/);
  assert.equal(deviceState(deviceFailure.devices[0], deviceFailure).key, "error");
  for (const failed of [{...paused, error: "session failed"}, {...paused, status: "error"}]) {
    assert.equal(simulationStatus(failed).level, "error");
    assert.equal(deviceState(failed.devices[0], failed).key, "error");
  }
});

test("finished sessions keep their terminal state but failed reads still expire it", () => {
  const now = Date.now();
  const stopped = {...snapshot, status: "stopped", updated_at_utc: new Date(now - 60000).toISOString()};
  assert.equal(snapshotIsStale(stopped, now, now), false);
  assert.equal(snapshotIsStale(stopped, now, now, true), true);
  assert.equal(simulationStatus(stopped).level, "unknown");
  assert.match(simulationStatus(stopped).label, /已结束/);
});
