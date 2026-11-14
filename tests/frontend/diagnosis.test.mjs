import test from "node:test";
import assert from "node:assert/strict";
import { alarmMeasurements, deviceKey } from "../../src/g15/console/static/state.js";
import { diagnosisModel } from "../../src/g15/console/static/diagnosis.js";

const device = { run_id: "run-a", device_id: "dev-a", scenario: "live" };
const alarm = (level = "red") => ({ start_seq: 20, rule_id: "health_heuristic", level,
  reason: "health 12.5; drivers: mean=4.5 (risk 1.00), rms=4.75 (risk 1.00), ptp=6.8e0 (risk 0.85) <= red threshold 40",
  created_at: "2026-11-12T09:00:00Z" });
const dimensions = { level_offset: 1, vibration_strength: 1, variability: 0.4, impact: 1, spectral: 0.2 };
const base = { selected: device, devices: [device], previews: {}, healthStatus: "ready",
  windows: [{ start_seq: 20, end_seq_exclusive: 276, health_score: 12.5,
    scoring_version: "heuristic-v1", rule_id: "health_heuristic", sample_rate_hz: 256,
    features_version: "2", risk_dimensions: dimensions }],
  alarms: [alarm()], alarmCursor: null, states: [{ rule_id: "health_heuristic", state: { active_level: "red" } }],
  liveMode: "legacy", live: null, liveError: "", pollPaused: false, detailReadAt: 1000, clock: 1000 };

test("alarm measurements preserve real values, scientific notation and bounded risks", () => {
  assert.deepEqual(alarmMeasurements(alarm()), [
    { name: "mean", value: 4.5, risk: 1 }, { name: "rms", value: 4.75, risk: 1 },
    { name: "ptp", value: 6.8, risk: 0.85 },
  ]);
  assert.deepEqual(alarmMeasurements({ reason: "drivers: rms=2 (risk 1.5), mean=no (risk 0.2)" }), []);
  assert.deepEqual(alarmMeasurements({ reason: "no drivers" }), []);
});

test("diagnosis model links the current window, five stored dimensions and source evidence", () => {
  const model = diagnosisModel(base);
  assert.equal(model.latest.health_score, 12.5);
  assert.equal(model.status.level, "red");
  assert.equal(model.source.activity, "active");
  assert.equal(model.source.startSeq, 20);
  assert.deepEqual(model.dimensions.map((row) => row.name),
    ["level_offset", "vibration_strength", "variability", "impact", "spectral"]);
  assert.equal(model.dimensions[2].risk, 0.4);
  assert.equal(model.alarms.length, 1);
  assert.equal(deviceKey(device), JSON.stringify([device.run_id, device.device_id]));
});

test("historical alarm evidence never turns a recovered current result red", () => {
  const model = diagnosisModel({ ...base,
    windows: [{ ...base.windows[0], start_seq: 276, health_score: 99,
      risk_dimensions: Object.fromEntries(Object.keys(dimensions).map((key) => [key, 0])) }],
    states: [{ rule_id: "health_heuristic", state: { active_level: null } }] });
  assert.equal(model.latest.health_score, 99);
  assert.equal(model.status.level, "healthy");
  assert.equal(model.source.activity, "inactive");
  assert.ok(model.dimensions.every((row) => row.risk === 0));
});

test("legacy windows keep all five dimensions unknown instead of fabricating zero", () => {
  const model = diagnosisModel({ ...base, alarms: [], states: [],
    windows: [{ ...base.windows[0], features_version: "1", risk_dimensions: null }] });
  assert.equal(model.source, null);
  assert.ok(model.dimensions.every((row) => row.risk === null));
  assert.deepEqual(model.alarms, []);
  assert.equal(model.status.level, "unknown");
});

test("a partially unknown dimension group is not drawn as zero risk", () => {
  const model = diagnosisModel({ ...base,
    windows: [{ ...base.windows[0], risk_dimensions: { ...dimensions, impact: null } }] });
  assert.equal(model.dimensions.find((row) => row.name === "impact").risk, null);
  assert.equal(model.dimensions.find((row) => row.name === "spectral").risk, 0.2);
});
