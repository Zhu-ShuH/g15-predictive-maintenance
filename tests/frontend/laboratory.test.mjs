import test from "node:test";
import assert from "node:assert/strict";
import { experimentParameters, secondsToSamples, injectionDraft, normalizeWaveform, waveformStatistics, plotGeometry, experimentStatus, experimentDraft, experimentParameterRows, connectionPresentation, reportSummary, initialExperimentStart, createLaboratoryClient } from "../../src/g15/console/static/laboratory.js";

const parameters = { profile: "normal", seed: 7, total_points: 8192, noise_std: 0.05, degradation_rate_per_sample: 0, fault_amplitude: 0 };
const raw = () => ({ run_id: "run-1", device_id: "DEV-1", source_kind: "manifest", sample_rate_hz: 4, available_points: 100,
  start_seq: 8, end_seq_exclusive: 12, count: 4, seq: [8, 9, 10, 11], timestamp_ns: ["1767225600000000000", "1767225600250000000", "1767225600500000000", "1767225600750000000"], time_seconds: [0, 0.25, 0.5, 0.75],
  vibration: [0, 1, 0, -1], temperature: [25, 25.1, 25.2, 25.3], units: { vibration: "g", temperature: "degC" }, integrity: { status: "sha256_verified", source: "manifest" },
  spectrum: { frequency_hz: [0, 1, 2], amplitude_g: [0, 1, 0], sample_count: 4, fft_points: 4, window: "hann", detrended: true, scaling: "single_sided_peak_amplitude", units: "g", start_seq: 8, end_seq_exclusive: 12 }, temperature_role: "context_only" });

test("fault injection preserves legacy requests and exact non-aligned integer sample positions", () => {
  const old = { ...parameters, profile: "sudden_fault", fault_amplitude: 3 };
  assert.deepEqual(experimentParameters(old), old);
  const next = { ...old, fault_type: "burst", fault_start_seq: 771, fault_duration_points: 1031 };
  assert.deepEqual(experimentParameters(next), next);
  assert.deepEqual(experimentDraft({ id: "lab-1", parameters: next }), next);
  for (const change of [{ fault_type: "unknown" }, { fault_duration_points: 2 }, { fault_start_seq: -1 }, { fault_start_seq: 0.5 }, { fault_duration_points: 8192 }, { profile: "normal" }]) assert.throws(() => experimentParameters({ ...next, ...change }));
  assert.throws(() => experimentParameters({ ...old, fault_type: "step" }));
});

test("injection time never silently rounds and old recorded plans remain reproducible", () => {
  assert.equal(secondsToSamples("3.01171875"), 771);
  assert.equal(secondsToSamples("0.00390625"), 1);
  for (const value of ["", " ", "0.1", -1, Infinity, false]) assert.throws(() => secondsToSamples(value));
  const old = { ...parameters, profile: "sudden_fault", fault_amplitude: 6 };
  const config = { fault_type: "step", fault_start_seq: 17, fault_duration_points: 31 };
  assert.deepEqual(injectionDraft(old, config), { ...old, ...config });
  assert.equal(injectionDraft(old).fault_start_seq, 4096);
  assert.equal(injectionDraft(parameters).fault_type, undefined);
  assert.equal(experimentParameterRows(old, config).length, 9);
});

test("experiment inputs preserve explicit seeds and clear parameters outside the selected profile", () => {
  assert.deepEqual(experimentParameters({ ...parameters, seed: "0", total_points: "8192", noise_std: "0", fault_amplitude: 9, degradation_rate_per_sample: 0.0009 }), { ...parameters, seed: 0, noise_std: 0 });
  assert.equal(experimentParameters({ ...parameters, profile: "degradation", degradation_rate_per_sample: "0.00015", fault_amplitude: 8 }).degradation_rate_per_sample, 0.00015);
  assert.equal(experimentParameters({ ...parameters, profile: "sudden_fault", fault_amplitude: "6" }).fault_amplitude, 6);
});

test("out-of-range, missing and nonfinite experiment parameters are not submitted", () => {
  for (const change of [{ seed: "" }, { seed: true }, { seed: 1.5 }, { seed: -1 }, { seed: 2147483648 }, { noise_std: Infinity }, { noise_std: -0.01 }, { noise_std: 1.01 }, { total_points: 8193 }, { total_points: 65537 }, { total_points: 0 }, { profile: "unregistered" }, { profile: "degradation", degradation_rate_per_sample: 0.01 }, { profile: "sudden_fault", fault_amplitude: 11 }]) assert.throws(() => experimentParameters({ ...parameters, ...change }));
});

test("waveform validation preserves the exact raw samples and large nanosecond strings", () => {
  const value = raw();
  assert.equal(normalizeWaveform(value), value);
  assert.equal(value.timestamp_ns[0], "1767225600000000000");
  assert.deepEqual(value.vibration, [0, 1, 0, -1]);
});

test("malformed arrays, inconsistent sample positions and invalid spectrum data cannot become curves", () => {
  const corruptions = [(v) => { v.vibration[2] = NaN; }, (v) => { v.temperature.pop(); }, (v) => { v.seq[2] = 99; }, (v) => { v.time_seconds[1] = 0.5; }, (v) => { v.timestamp_ns[0] = 1767225600000000000; }, (v) => { v.end_seq_exclusive = 13; }, (v) => { v.available_points = 10; }, (v) => { v.units.vibration = "m/s"; }, (v) => { v.spectrum.amplitude_g[1] = -1; }, (v) => { v.spectrum.frequency_hz[2] = 3; }, (v) => { v.spectrum.start_seq = 7; }];
  for (const corrupt of corruptions) { const value = raw(); corrupt(value); assert.throws(() => normalizeWaveform(value), /invalid_response/); }
});

test("legitimate empty committed data remains empty rather than a generated signal", () => {
  const value = { ...raw(), count: 0, available_points: 0, start_seq: 0, end_seq_exclusive: 0, seq: [], timestamp_ns: [], time_seconds: [], vibration: [], temperature: [], spectrum: null };
  assert.equal(normalizeWaveform(value).count, 0);
  assert.deepEqual(waveformStatistics(value), { rms: null, peakToPeak: null, peakFrequency: null });
  assert.equal(plotGeometry([], []), null);
});

test("snapshot measurements derive from actual vibration and supplied spectral amplitude", () => {
  const stats = waveformStatistics(raw());
  assert.ok(Math.abs(stats.rms - Math.SQRT1_2) < 1e-12);
  assert.equal(stats.peakToPeak, 2);
  assert.equal(stats.peakFrequency, 1);
  assert.deepEqual(waveformStatistics({ ...raw(), vibration: [0, 0, 0, 0], spectrum: null }), { rms: 0, peakToPeak: 0, peakFrequency: null });
});

test("plot geometry retains irregular sample positions without synthesizing extra points", () => {
  const model = plotGeometry([0, 1, 4], [3, 2, 5], { width: 600, height: 200 });
  assert.equal(model.points.length, 3);
  assert.equal((model.path.match(/[ML]/g) || []).length, 3);
  assert.ok(Math.abs((model.points[1].x - model.points[0].x) / (model.points[2].x - model.points[0].x) - 0.25) < 1e-12);
  assert.equal(plotGeometry([0, 1], [1, Infinity]), null);
  assert.equal(plotGeometry([1, 0], [1, 2]), null);
  assert.equal(plotGeometry([0], [null]), null);
});

test("constant and zero signals retain finite axes and the health scale stays at zero to one hundred", () => {
  for (const ys of [[0, 0], [25, 25], [-2, -2]]) assert.doesNotMatch(plotGeometry([0, 1], ys).path, /NaN|Infinity/);
  const health = plotGeometry([0, 1], [20, 90], { fixedY: [0, 100] });
  assert.equal(health.ymin, 0); assert.equal(health.ymax, 100);
});

test("initial sudden-fault window includes the public planned injection for every supported window size", () => {
  const detail = { experiment: { parameters: { ...parameters, profile: "sudden_fault", total_points: 16384 } }, configuration: { fault_start_seq: 8192, fault_duration_points: 512 } };
  for (const count of [256, 512, 1024, 2048]) { const start = initialExperimentStart(detail, count); assert.ok(start <= 8192 && start + count > 8192); assert.equal(start % 256, 0); }
  assert.equal(initialExperimentStart(detail, 2048), 7680);
  assert.equal(initialExperimentStart({ experiment: { parameters: { ...parameters, profile: "degradation" } } }, 2048), 6144);
  assert.equal(initialExperimentStart({ experiment: { parameters } }, 2048), 0);
});

test("completion, report verdict, missing results and fault-free detection stay distinct", () => {
  assert.equal(experimentStatus({ status: "completed" }).label, "执行完成");
  assert.equal(experimentStatus({ status: "failed" }).state, "failed");
  assert.equal(experimentStatus({ status: "other" }).state, "waiting");
  const unassessed = reportSummary({ verifiable: false, passed: true, detection_rate: null, fault_events: 0, false_alarm_count: null });
  assert.equal(unassessed.state, "waiting"); assert.match(unassessed.verdict, /未判定/);
  assert.equal(unassessed.items.find(([name]) => name === "检出率")[1], "不适用");
  assert.equal(unassessed.items.find(([name]) => name === "误报事件")[1], "—");
  assert.equal(reportSummary({ verifiable: true, passed: false }).state, "failed");
  assert.equal(reportSummary(null).items.length, 0);
});

test("client uses same-origin explicit laboratory actions and safely encodes resource identifiers", async () => {
  const requests = [], client = createLaboratoryClient({ fetchImpl: async (path, options) => { requests.push({ path, options }); return { ok: true, json: async () => ({ experiment: { id: "exp-1" } }) }; } });
  await client.create(parameters);
  assert.equal(requests[0].path, "/api/laboratory/experiments");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[0].options.headers["X-G15-Console-Action"], "laboratory");
  assert.equal(requests[0].options.credentials, "same-origin");
  assert.deepEqual(JSON.parse(requests[0].options.body), parameters);
  await client.waveform({ run_id: "run&other=1", device_id: "设备 A", count: "256" });
  const query = new URL(requests[1].path, "http://localhost").searchParams;
  assert.equal(query.get("run_id"), "run&other=1"); assert.equal(query.get("device_id"), "设备 A"); assert.equal(query.has("other"), false);
  assert.equal(requests[1].options.method, "GET"); assert.equal(requests[1].options.headers["X-G15-Console-Action"], undefined);
  assert.throws(() => client.create({ ...parameters, seed: -1 })); assert.equal(requests.length, 2);
});

test("a failed creation keeps its error and is never automatically retried", async () => {
  let calls = 0;
  const client = createLaboratoryClient({ fetchImpl: async () => { calls += 1; return { ok: false, status: 409, json: async () => ({ error: "laboratory_busy" }) }; } });
  await assert.rejects(client.create(parameters), (error) => error.code === "laboratory_busy" && error.status === 409);
  assert.equal(calls, 1);
});

test("timeouts abort a read without retrying or producing substitute data", async () => {
  let calls = 0;
  const client = createLaboratoryClient({ timeoutMs: 5, fetchImpl: (_path, options) => new Promise((_resolve, reject) => { calls += 1; options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true }); }) });
  await assert.rejects(client.list(), { name: "AbortError" }); assert.equal(calls, 1);
});

test("loading recorded parameters creates a detached reproducible draft without changing the run", () => {
  const experiment = { id: "exp-original", run_id: "run-original", status: "completed", parameters: { ...parameters, profile: "sudden_fault", seed: 42, total_points: 8448, noise_std: 0.123, fault_amplitude: 6.53 } };
  const original = structuredClone(experiment), draft = experimentDraft(experiment);
  assert.deepEqual(experimentParameters(draft), original.parameters);
  assert.notEqual(draft, experiment.parameters);
  draft.seed = 123; draft.fault_amplitude = 2;
  assert.deepEqual(experiment, original);
  assert.equal(Object.hasOwn(draft, "status"), false);
  assert.equal(Object.hasOwn(draft, "run_id"), false);
  assert.throws(() => experimentDraft({ id: "incomplete" }));
});

test("recorded parameter display keeps all six fields, units and nonapplicable values", () => {
  const normal = experimentParameterRows(parameters);
  assert.equal(normal.length, 6);
  assert.match(normal.find(([name]) => name === "样本点数")[1], /8,192 点/);
  assert.equal(normal.find(([name]) => name === "噪声标准差")[1], "0.05 g");
  assert.match(normal.find(([name]) => name === "故障振幅")[1], /不适用.*0/);
  const degradation = experimentParameterRows({ ...parameters, profile: "degradation", degradation_rate_per_sample: 0.00015 });
  assert.equal(degradation.find(([name]) => name === "每样本退化增量")[1], "0.00015 g / 样本");
  assert.equal(experimentParameterRows({ ...parameters, profile: "degradation", degradation_rate_per_sample: 0.000000123 }).find(([name]) => name === "每样本退化增量")[1], "1.23e-7 g / 样本");
  assert.match(experimentParameterRows({ ...parameters, seed: null }).find(([name]) => name === "随机种子")[1], /—/);
});

test("connection indicator is neutral while waiting, paused or stale and red on failures", () => {
  for (const state of ["waiting", "paused", "stale", "ready", "error"]) assert.equal(connectionPresentation({ mode: "live", liveStatus: { state, label: state } }).state, state);
  assert.equal(connectionPresentation({ mode: "live", following: false, liveStatus: { state: "ready", label: "正在采集" } }).state, "paused");
  assert.equal(connectionPresentation({ mode: "live", following: false, readError: true }).state, "error");
  assert.equal(connectionPresentation({ mode: "live", following: false, liveStatus: { state: "error", label: "设备错误" } }).state, "error");
  assert.equal(connectionPresentation({ mode: "experiment", catalogueAvailable: false }).state, "waiting");
  for (const status of ["queued", "generating", "analysing", "reporting"]) assert.equal(connectionPresentation({ mode: "experiment", catalogueAvailable: true, experiment: { status } }).state, "waiting");
  assert.equal(connectionPresentation({ mode: "experiment", catalogueAvailable: true, experiment: { status: "failed" } }).state, "error");
  assert.equal(connectionPresentation({ mode: "experiment", catalogueAvailable: true, experiment: { status: "completed" } }).state, "ready");
});
