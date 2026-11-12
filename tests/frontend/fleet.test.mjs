import test from "node:test";
import assert from "node:assert/strict";
import { fleetLink, fleetRows, fleetSummary, columnBalanceMode } from "../../src/g15/console/static/fleet.js";
import { deviceKey } from "../../src/g15/console/static/state.js";

const device = (suffix) => ({ run_id: `run-${suffix}`, device_id: `dev-${suffix}` });
const preview = (score, level) => ({ status: "ready", readAt: 1000,
  latest: { start_seq: 10, health_score: score }, active: { level, label: `${level}-label` } });
const A = device("a"), B = device("b"), C = device("c"), D = device("d"), E = device("e");
const state = {
  devices: [C, D, A, E, B],
  previews: {
    [deviceKey(A)]: preview(20, "red"), [deviceKey(B)]: preview(60, "yellow"),
    [deviceKey(C)]: preview(99, "healthy"),
    [deviceKey(E)]: { status: "error", readAt: 1000, latest: null, active: { level: "error" } },
  },
  liveMode: "legacy", live: null, liveError: "", pollPaused: false, clock: 1000,
};

test("fleet summary counts only actual devices and keeps error and unknown distinct", () => {
  const result = fleetSummary(state);
  assert.deepEqual(result.counts, { total: 5, red: 1, yellow: 1, healthy: 1, unknown: 1, error: 1 });
  assert.equal(result.liveTotals, null);
  assert.deepEqual(fleetRows(state).map((row) => row.device.device_id),
    ["dev-a", "dev-e", "dev-b", "dev-d", "dev-c"]);
});

test("fleet live totals are summed from actual per-device counters", () => {
  const liveDevices = state.devices.map((row, index) => ({
    run_id: row.run_id, device_id: row.device_id, captured_samples: 100 + index,
    processed_samples: 90 + index, windows_written: 2 + index, queue_depth: index,
  }));
  const result = fleetSummary({ ...state, live: { devices: liveDevices } });
  assert.deepEqual(result.liveTotals, { captured: 510, processed: 460, windows: 20, queue: 10 });
});

test("fleet links preserve only explicit resource and view, never credentials", () => {
  const url = fleetLink("/", A, "maintenance");
  const parsed = new URL(url, "http://localhost");
  assert.equal(parsed.pathname, "/");
  assert.equal(parsed.searchParams.get("device"), A.device_id);
  assert.equal(parsed.searchParams.get("run"), A.run_id);
  assert.equal(parsed.searchParams.get("view"), "maintenance");
  assert.doesNotMatch(url, /token|Bearer|password/);
  assert.equal(fleetLink("/screen", null), "/screen");
});

test("empty catalog produces zero counts instead of borrowed demo values", () => {
  const result = fleetSummary({ devices: [], previews: {}, liveMode: "legacy", live: null });
  assert.deepEqual(result.counts, { total: 0, red: 0, yellow: 0, healthy: 0, unknown: 0, error: 0 });
  assert.deepEqual(result.rows, []);
});


test("same-row columns choose floating, stretching or natural height from measured content", () => {
  assert.equal(columnBalanceMode(300, 700), "left-float");
  assert.equal(columnBalanceMode(300, 396), "natural");
  assert.equal(columnBalanceMode(400, 200), "right-stretch");
  assert.equal(columnBalanceMode(300, 300), "natural");
  assert.equal(columnBalanceMode(0, 300), "natural");
  assert.equal(columnBalanceMode(300, Number.NaN), "natural");
});
