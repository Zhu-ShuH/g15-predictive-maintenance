import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { readableRun, normalizeRunDetail, workbenchConnection, replaySnapshot, replayWaveRange, latencyPresentation, dispositionRequest, escapeHTML, csvCell, healthChartSVG, configurationRows, reportContentHTML, exportCSV, exportHTML, createWorkbenchClient } from "../../src/g15/console/static/workbench.js";

const detail = () => ({ available_points: 1024, complete: true, observed_at_utc: "2026-11-14T12:00:00Z", run: { id: "live:run-1", run_id: "run-1", device_id: "DEV-1", source_kind: "archive", scenario: "normal" }, configuration: { parameters: { seed: 7 }, sample_rate_hz: 256, window_points: 256, scoring_versions: ["v1"], rule_ids: ["R1"] }, windows: [{ start_seq: 0, end_seq_exclusive: 256, time_seconds: 1, health_score: 99 }, { start_seq: 256, end_seq_exclusive: 512, time_seconds: 2, health_score: null }, { start_seq: 512, end_seq_exclusive: 768, time_seconds: 3, health_score: 40 }], alarms: [{ alarm_key: "a1", rule_id: "R1", start_seq: 256, level: "yellow", reason: "check", advice: ["检查轴承"] }, { alarm_key: "a2", rule_id: "R1", start_seq: 512, level: "red", reason: "fault", advice: ["检查轴承", "停机复核"] }], summary: { processed_points: 1024, window_count: 3, alarm_count: 2, red_count: 1, yellow_count: 1, health_min: 40, health_max: 99, health_latest: 40 }, latency: { kind: "unavailable" }, dispositions: [] });
const snapshot = () => ({ runs: [detail()], generated_at_utc: "2026-11-14T12:00:00Z", alignment: "elapsed_seconds" });
const request = () => ({ id: "live:run-1", alarm_key: "a1", action: "acknowledge", operator: " A ", note: " 已检查传感器 ", request_id: "8b707d3d-9205-42b7-9ea3-901ce0b374a0" });

test("complete run validation preserves null health and rejects malformed evidence", () => {
  assert.equal(normalizeRunDetail(detail()).windows[1].health_score, null);
  for (const change of [d => { d.complete = false; }, d => { d.windows[0].health_score = 101; }, d => { d.windows[1].start_seq = -1; }, d => { d.windows[0].time_seconds = NaN; }, d => { d.alarms[0].advice = "invented"; }]) { const d = detail(); change(d); assert.throws(() => normalizeRunDetail(d)); }
});
test("missing or invalid replay bounds and sample rates fail explicitly before rendering", () => {
  for (const available of [undefined, null, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, "1024"]) {
    const d = detail(); d.available_points = available; assert.throws(() => normalizeRunDetail(d), { code: "invalid_response" });
  }
  for (const rate of [undefined, null, 0, -1, NaN, Infinity, "256"]) {
    const d = detail(); d.configuration.sample_rate_hz = rate; assert.throws(() => normalizeRunDetail(d), { code: "invalid_response" });
  }
  assert.equal(workbenchConnection({ reading: false, readError: true, catalogAvailable: true }).state, "error");
});
test("zero committed samples finish loading as a valid empty run without requesting a waveform", () => {
  const d = detail(); d.available_points = 0; d.windows = []; d.alarms = []; d.summary.processed_points = 0;
  assert.equal(normalizeRunDetail(d), d); assert.equal(replayWaveRange(d, 0), null);
  assert.equal(workbenchConnection({ reading: true, detail: d, catalogAvailable: true }).state, "waiting");
  assert.deepEqual(workbenchConnection({ reading: false, detail: d, catalogAvailable: true }), { state: "ready", label: "运行暂无已提交样本" });
  assert.deepEqual(replaySnapshot(d, 0), { window: null, alarms: [], advice: [] });
});
test("unfinished experiments cannot be selected as completed replay or comparison evidence", () => {
  for (const status of ["queued", "generating", "analysing", "reporting", "failed"]) assert.equal(readableRun({ source_kind: "experiment", run_id: "run-1", status }), false);
  assert.equal(readableRun({ source_kind: "experiment", run_id: "run-1", status: "completed" }), true);
  assert.equal(readableRun({ source_kind: "live", run_id: "run-1", status: "running" }), true);
  assert.equal(readableRun({ source_kind: "experiment", run_id: null, status: "completed" }), false);
});
test("replay uses completed windows and never imports future alarms or advice", () => {
  assert.equal(replaySnapshot(detail(), 254).window, null);
  assert.equal(replaySnapshot(detail(), 255).window.health_score, 99);
  const middle = replaySnapshot(detail(), 511);
  assert.equal(middle.alarms.length, 1); assert.deepEqual(middle.advice, ["检查轴承"]);
  const after = replaySnapshot(detail(), 767);
  assert.equal(after.window.health_score, 40); assert.deepEqual(after.advice, ["检查轴承", "停机复核"]);
});
test("alarm evidence becomes available only at its full source window boundary", () => {
  const d = detail(); d.alarms[0].end_seq_exclusive = 512; d.alarms[0].available_seq = 511;
  for (const cursor of [256, 510]) { assert.equal(replaySnapshot(d, cursor).alarms.length, 0); assert.deepEqual(replaySnapshot(d, cursor).advice, []); }
  assert.equal(replaySnapshot(d, 511).alarms.length, 1);
  delete d.alarms[0].end_seq_exclusive; delete d.alarms[0].available_seq;
  assert.equal(replaySnapshot(d, 510).alarms.length, 0); assert.equal(replaySnapshot(d, 511).alarms.length, 1);
  d.alarms[0].start_seq = 257;
  assert.equal(replaySnapshot(d, 511).alarms.length, 0);
});
test("waveform ranges stay within committed history at both timeline ends", () => {
  assert.deepEqual(replayWaveRange(detail(), 0, 256), { id: "live:run-1", start_seq: 0, count: 256 });
  assert.equal(replayWaveRange(detail(), 1023, 256).start_seq, 768);
  const d = detail(); d.available_points = 1; assert.equal(replayWaveRange(d, 0), null);
  d.available_points = 2048; d.summary.processed_points = 768;
  assert.equal(replayWaveRange(d, 2047, 256).start_seq, 1792);
});
test("processing latency and total experiment duration remain distinct with missing values", () => {
  assert.deepEqual(latencyPresentation({ kind: "capture_to_commit", count: 0, mean_seconds: null, max_seconds: null }), { label: "采集至提交延迟", value: "未计算", note: "暂无已提交计时样本" });
  assert.equal(latencyPresentation({ kind: "capture_to_commit", count: 1, mean_seconds: 0, max_seconds: 0 }).value, "0 秒");
  assert.equal(latencyPresentation({ kind: "experiment_elapsed", elapsed_seconds: 2 }).label, "实验总耗时");
  assert.equal(latencyPresentation(null).value, "不可用");
});
test("dispositions require content and stable UUID, without modifying inputs", () => {
  const original = request(), result = dispositionRequest(original);
  assert.equal(result.note, "已检查传感器"); assert.equal(original.note, " 已检查传感器 "); assert.equal(result.request_id, original.request_id);
  for (const changes of [{ note: " " }, { note: "a".repeat(2001) }, { operator: "a".repeat(81) }, { action: "delete" }, { request_id: "not-uuid" }, { alarm_key: "" }]) assert.throws(() => dispositionRequest({ ...request(), ...changes }));
});
test("health charts break at missing windows instead of inventing a connecting value", () => {
  const svg = healthChartSVG([detail()]);
  const path = /<path d="([^"]*)"/.exec(svg)[1];
  assert.equal((path.match(/M/g) || []).length, 2); assert.equal((path.match(/L/g) || []).length, 0);
  assert.equal((svg.match(/<circle /g) || []).length, 2);
  assert.doesNotMatch(svg, /NaN|Infinity/);
  const d = detail(); d.windows[1].health_score = 80; d.windows[1].start_seq = 300;
  assert.equal((/<path d="([^"]*)"/.exec(healthChartSVG([d]))[1].match(/M/g) || []).length, 2);
});
test("CSV exports quote commas and line breaks and neutralize spreadsheet formulas", () => {
  for (const text of ["=HYPERLINK(1)", "+CMD", "-CMD", "@SUM(1)", "  =1", "\t=1", "\n=1"]) assert.ok(csvCell(text).startsWith('"\''));
  assert.equal(csvCell('line,"one"\nnext'), '"line,""one""\nnext"');
  assert.equal(csvCell(null), '""');
  const data = snapshot(); data.runs[0].run.device_id = "=RUN()";
  const csv = exportCSV(data); assert.ok(csv.startsWith("\uFEFF")); assert.match(csv, /"'=RUN\(\) \/ run-1"/); assert.match(csv, /配置.seed/);
});
test("standalone HTML exports actual chart and escaped source text without executable payloads", () => {
  const data = snapshot(); data.runs[0].run.device_id = '</title><script>alert(1)</script>'; data.runs[0].configuration.parameters = { html: '<img src=x onerror="alert(2)">' };
  const html = exportHTML(data); assert.match(html, /<svg/); assert.match(html, /&lt;script&gt;/); assert.match(html, /&lt;img/); assert.doesNotMatch(html, /<script|<img|<[^>]*\sonerror=/i); assert.match(html, /计时口径/);
  assert.equal(escapeHTML('"<&'), "&quot;&lt;&amp;");
});
test("report preview and independent export share legends, actual summaries and Chinese parameter comparisons", () => {
  const data = snapshot(), second = detail(); second.run.run_id = "run-2"; second.run.device_id = "DEV-2"; second.run.scenario = "sudden_fault";
  second.configuration.parameters = { profile: "sudden_fault", seed: 42, noise_std: 0.000000123, fault_type: "burst", fault_start_seq: 771, fault_duration_points: 1031, fault_amplitude: 6.123456789123456 };
  data.runs.push(second);
  const rows = configurationRows(data.runs);
  assert.deepEqual(rows.find(([label]) => label === "故障类型"), ["故障类型", "不适用", "振荡突发"]);
  assert.equal(rows.find(([label]) => label === "故障开始位置")[2], "771 样本序号 · 3.01171875 秒");
  assert.equal(rows.find(([label]) => label === "故障持续时间")[2], "1031 点 · 4.02734375 秒");
  assert.equal(rows.find(([label]) => label === "噪声标准差")[2], "1.23e-7 g");
  assert.equal(rows.find(([label]) => label === "故障振幅")[2], "6.123456789123456 g");
  for (const html of [reportContentHTML(data), exportHTML(data)]) {
    assert.match(html, /运行曲线图例/); assert.match(html, /class="wb-series-0"/); assert.match(html, /class="wb-series-1"/);
    assert.match(html, /运行指标摘要/); assert.match(html, /最新健康分/); assert.match(html, /告警总数/); assert.match(html, /计时口径/);
    assert.match(html, /运行参数对照/); assert.match(html, /振荡突发/); assert.match(html, /<details class="wb-raw-configuration">/);
    assert.match(html, /fault_duration_points/); assert.match(html, /可横向滚动/);
    assert.equal((html.match(/<table /g) || []).length, 2);
    assert.doesNotMatch(html, /\sstyle=/);
  }
});
test("narrow health diagrams use visible pixel geometry while independent reports keep a fixed chart", () => {
  const svg = healthChartSVG([detail()], { width: 258, height: 200 });
  assert.match(svg, /viewBox="0 0 258 200"/); assert.match(svg, /font-size="12"/); assert.doesNotMatch(svg, /NaN|Infinity/);
  assert.match(exportHTML(snapshot()), /viewBox="0 0 900 255"/);
});
test("workbench keeps every laboratory navigation destination plus its current workspace", () => {
  const navLinks = (name) => { const html = readFileSync(new URL(`../../src/g15/console/static/${name}.html`, import.meta.url), "utf8"); const nav = /<nav class="main-nav"[^>]*>([\s\S]*?)<\/nav>/.exec(html)[1]; return [...nav.matchAll(/href="([^"]+)"/g)].map((match) => match[1]); };
  const laboratory = navLinks("laboratory"), workbench = navLinks("workbench");
  assert.deepEqual(new Set(workbench), new Set([...laboratory, "/workbench"]));
  assert.equal(workbench.length, new Set(workbench).size);
});
test("client encodes identity and sends explicit workbench mutation headers", async () => {
  const calls = [], client = createWorkbenchClient({ fetchImpl: async (path, options) => { calls.push({ path, options }); return { ok: true, json: async () => ({ ok: true }) }; } });
  await client.compare(["live:a", "experiment:lab-1"]); await client.waveform({ id: "live:a", start_seq: 0, count: 256 }); await client.save(request());
  assert.equal(calls[0].path, "/api/workbench/compare?ids=live%3Aa%2Cexperiment%3Alab-1");
  assert.equal(calls[1].path, "/api/workbench/waveform?id=live%3Aa&start_seq=0&count=256");
  assert.equal(calls[2].options.headers["X-G15-Console-Action"], "workbench"); assert.equal(calls[2].options.credentials, "same-origin");
  assert.equal(JSON.parse(calls[2].options.body).request_id, request().request_id);
});
test("failed writes are not retried automatically and can reuse the same idempotency key", async () => {
  const bodies = [], client = createWorkbenchClient({ fetchImpl: async (_path, options) => { bodies.push(options.body); throw new Error("network"); } });
  await assert.rejects(client.save(request())); assert.equal(bodies.length, 1);
  await assert.rejects(client.save(request())); assert.equal(bodies[0], bodies[1]);
});
