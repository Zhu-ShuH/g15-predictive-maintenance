import { normalizeWaveform, plotGeometry } from "./laboratory.js";

const COLORS = ["#80d8cf", "#f3c77a", "#8fbef2", "#efa8cd"];
const SOURCE_LABELS = { live: "当前会话", archive: "历史归档", experiment: "独立实验" };
const SCENARIO_LABELS = { normal: "健康基线", degradation: "持续退化", sudden_fault: "突发故障", live: "实时仿真" };
const RUN_STATES = { queued: "等待执行", generating: "正在生成", analysing: "正在分析", reporting: "正在生成报告", failed: "执行失败" };
const finite = (v) => typeof v === "number" && Number.isFinite(v);
const integer = (v) => Number.isSafeInteger(v) && v >= 0;
const format = (v, digits = 3) => finite(v) ? v.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
const dateText = (v) => v && Number.isFinite(Date.parse(v)) ? new Date(v).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }) : "—";
const invalid = () => Object.assign(new Error("invalid_response"), { code: "invalid_response" });
export const readableRun = (run) => typeof run?.run_id === "string" && (run.source_kind !== "experiment" || run.status === "completed");
const ERRORS = { operations_unavailable: "分析服务未启用，请检查服务配置后刷新。", not_found: "该运行已不在当前目录中，请刷新运行列表。", experiment_not_ready: "实验尚未完成，请稍后刷新。", source_too_large: "本次运行超过工作台读取上限，未返回部分数据。", operations_full: "处置记录已达容量上限，未追加本次内容。", request_conflict: "这次请求标识对应其他内容，请先核对处置记录。", already_acknowledged: "该告警已被确认，请刷新记录；可继续追加处理备注。", invalid_source: "运行源数据未通过读取校验，未展示替代数据。", invalid_request: "提交参数不符合要求，请检查后重试。", invalid_response: "返回的数据结构不完整，请刷新后重试。", waveform_range_unavailable: "该片段当前不可读，请选择其他位置或刷新。", waveform_busy: "波形读取正忙，请稍后重试。", waveform_invalid_data: "原始样本未通过校验，无法绘制波形。" };

export const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
export function csvCell(value) {
  let text = value === null || value === undefined ? "" : String(value);
  if (/^[\s\u0000-\u001f]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

export function normalizeRunDetail(value) {
  if (!value || value.complete !== true || typeof value.run?.id !== "string" || typeof value.run?.run_id !== "string" || typeof value.run?.device_id !== "string" || !Array.isArray(value.windows) || value.windows.length > 8192 || !value.summary || !value.configuration) throw invalid();
  if (!integer(value.available_points) || !finite(value.configuration.sample_rate_hz) || value.configuration.sample_rate_hz <= 0) throw invalid();
  let previous = -1;
  for (const window of value.windows) {
    if (!integer(window.start_seq) || !integer(window.end_seq_exclusive) || window.end_seq_exclusive <= window.start_seq || window.start_seq < previous || !finite(window.time_seconds) || window.time_seconds < 0 || window.health_score !== null && (!finite(window.health_score) || window.health_score < 0 || window.health_score > 100)) throw invalid();
    previous = window.start_seq;
  }
  if (value.alarms !== undefined && (!Array.isArray(value.alarms) || value.alarms.length > 4096 || value.alarms.some((alarm) => typeof alarm.alarm_key !== "string" || !integer(alarm.start_seq) || !Array.isArray(alarm.advice) || alarm.advice.some((text) => typeof text !== "string")))) throw invalid();
  return value;
}

export function workbenchConnection({ reading = false, readError = false, detail = null, catalogAvailable = false, reportReady = false, now = Date.now() } = {}) {
  if (readError) return { state: "error", label: "读取失败" };
  if (reading) return { state: "waiting", label: "正在读取证据" };
  if (detail?.run.source_kind === "live" && (!Number.isFinite(Date.parse(detail.run.updated_at_utc)) || now - Date.parse(detail.run.updated_at_utc) > 10000)) return { state: "stale", label: "采集状态已过期" };
  if (catalogAvailable) return { state: "ready", label: reportReady ? "比较快照已就绪" : detail?.available_points === 0 ? "运行暂无已提交样本" : "运行证据已读取" };
  return { state: "waiting", label: "等待分析服务" };
}

export function alarmAvailableSequence(alarm, windows = []) {
  const window = windows.find((entry) => entry.start_seq === alarm.start_seq);
  const end = integer(alarm.end_seq_exclusive) && alarm.end_seq_exclusive > alarm.start_seq ? alarm.end_seq_exclusive : window?.end_seq_exclusive;
  return integer(end) && end > alarm.start_seq ? end - 1 : null;
}

export function replaySnapshot(detail, cursor) {
  const windows = detail?.windows || [], alarms = (detail?.alarms || []).filter((alarm) => { const available = alarmAvailableSequence(alarm, windows); return available !== null && available <= cursor; });
  const window = windows.filter((entry) => entry.end_seq_exclusive <= cursor + 1).at(-1) || null;
  return { window, alarms, advice: [...new Set(alarms.flatMap((alarm) => alarm.advice || []))] };
}

export function replayWaveRange(detail, cursor, count = 1024) {
  const available = detail?.available_points;
  if (!integer(available) || available < 2) return null;
  const actual = Math.min(count, available), start = Math.min(available - actual, Math.max(0, Math.floor(cursor - actual / 2)));
  return { id: detail.run.id, start_seq: start, count: actual };
}

export function latencyPresentation(latency) {
  if (latency?.kind === "capture_to_commit") return { label: "采集至提交延迟", value: latency.count > 0 && finite(latency.mean_seconds) ? `${format(latency.mean_seconds)} 秒` : "未计算", note: latency.count > 0 ? `最大 ${format(latency.max_seconds)} 秒 · ${format(latency.count, 0)} 次提交` : "暂无已提交计时样本" };
  if (latency?.kind === "experiment_elapsed") return { label: "实验总耗时", value: finite(latency.elapsed_seconds) ? `${format(latency.elapsed_seconds)} 秒` : "未计算", note: "—" };
  return { label: "处理延迟", value: "不可用", note: "未记录" };
}

export function dispositionRequest(values) {
  const text = (key, limit) => { if (typeof values[key] !== "string" || !values[key].trim() || values[key].trim().length > limit) throw new Error(key === "operator" ? "请填写 1 至 80 字的记录人。" : "请填写 1 至 2,000 字的处理备注。"); return values[key].trim(); };
  if (typeof values.id !== "string" || !values.id || typeof values.alarm_key !== "string" || !values.alarm_key || !["acknowledge", "note"].includes(values.action) || typeof values.request_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(values.request_id)) throw new Error("请选择运行、告警和有效处置动作。");
  return { id: values.id, alarm_key: values.alarm_key, action: values.action, operator: text("operator", 80), note: text("note", 2000), request_id: values.request_id };
}

export function healthChartSVG(runs, { cursorSeconds = null, width = 900, height = 255 } = {}) {
  const series = runs.map((detail, index) => ({ label: `${detail.run.device_id} / ${detail.run.run_id}`, color: COLORS[index % COLORS.length], points: detail.windows || [] }));
  const xmax = Math.max(1, ...series.flatMap((item) => item.points.map((point) => finite(point.time_seconds) ? point.time_seconds : 0)));
  const left = 48, right = width - 18, top = 18, bottom = height - 38;
  const x = (value) => left + value / xmax * (right - left), y = (value) => bottom - value / 100 * (bottom - top);
  let body = [0, 25, 50, 75, 100].map((value) => `<line x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}" stroke="#485966" stroke-dasharray="3 5"/><text x="${left - 9}" y="${y(value) + 4}" text-anchor="end">${value}</text>`).join("");
  body += [0, .5, 1].map((fraction) => `<text x="${x(xmax * fraction)}" y="${height - 15}" text-anchor="${fraction === 0 ? "start" : fraction === 1 ? "end" : "middle"}">${format(xmax * fraction, 2)} 秒</text>`).join("");
  for (const item of series) {
    let connected = false, previousEnd = null, path = "";
    for (const point of item.points) {
      if (!finite(point.health_score) || !finite(point.time_seconds)) { connected = false; previousEnd = null; continue; }
      if (previousEnd !== null && point.start_seq > previousEnd) connected = false;
      path += `${connected ? "L" : "M"}${x(point.time_seconds).toFixed(2)} ${y(point.health_score).toFixed(2)} `; connected = true; previousEnd = point.end_seq_exclusive;
    }
    body += `<path d="${path}" fill="none" stroke="${item.color}" stroke-width="2" vector-effect="non-scaling-stroke"><title>${escapeHTML(item.label)}</title></path>`;
    for (const [index, point] of item.points.entries()) {
      if (!finite(point.health_score)) continue;
      const before = item.points[index - 1], after = item.points[index + 1];
      if ((!before || !finite(before.health_score) || before.end_seq_exclusive < point.start_seq) && (!after || !finite(after.health_score) || point.end_seq_exclusive < after.start_seq)) body += `<circle cx="${x(point.time_seconds)}" cy="${y(point.health_score)}" r="3" fill="${item.color}"/>`;
    }
  }
  if (finite(cursorSeconds)) body += `<line x1="${x(Math.min(xmax, Math.max(0, cursorSeconds)))}" x2="${x(Math.min(xmax, Math.max(0, cursorSeconds)))}" y1="${top}" y2="${bottom}" stroke="#feffef" stroke-dasharray="5 4"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="按运行经过秒数对齐的真实健康窗口，0 至 100 分"><rect width="100%" height="100%" fill="#17222c"/><g fill="#c8d9df" font-size="12" font-family="sans-serif">${body}</g></svg>`;
}

export function comparisonRows(runs) {
  return [["来源", ...runs.map((d) => SOURCE_LABELS[d.run.source_kind] || "未知")], ["场景", ...runs.map((d) => SCENARIO_LABELS[d.run.scenario] || d.run.scenario || "—")], ["最新健康分", ...runs.map((d) => format(d.summary.health_latest, 2))], ["最低健康分", ...runs.map((d) => format(d.summary.health_min, 2))], ["告警总数", ...runs.map((d) => format(d.summary.alarm_count, 0))], ["红色 / 黄色告警", ...runs.map((d) => `${format(d.summary.red_count, 0)} / ${format(d.summary.yellow_count, 0)}`)], ["已分析覆盖样本", ...runs.map((d) => format(d.summary.processed_points, 0))], ["计时口径", ...runs.map((d) => latencyPresentation(d.latency).label)], ["计时结果", ...runs.map((d) => latencyPresentation(d.latency).value)], ["计时统计", ...runs.map((d) => latencyPresentation(d.latency).note)], ["评分版本", ...runs.map((d) => d.configuration.scoring_versions?.join("、") || "未记录")], ["规则", ...runs.map((d) => d.configuration.rule_ids?.join("、") || "未记录")]];
}

function comparisonTable(runs, rows = comparisonRows(runs), caption = "运行指标比较") {
  return `<table class="wb-table"><caption class="sr-only">${escapeHTML(caption)}</caption><thead><tr><th scope="col">比较项</th>${runs.map((d) => `<th scope="col">${escapeHTML(d.run.device_id)}<small>${escapeHTML(d.run.run_id)}</small></th>`).join("")}</tr></thead><tbody>${rows.map(([label, ...values]) => `<tr><th scope="row">${escapeHTML(label)}</th>${values.map((value) => `<td>${escapeHTML(value)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

const PARAMETER_LABELS = { profile: "仿真场景", seed: "随机种子", total_points: "样本点数", noise_std: "噪声标准差", degradation_rate_per_sample: "每样本退化增量", fault_type: "故障类型", fault_amplitude: "故障振幅", fault_start_seq: "故障开始位置", fault_duration_points: "故障持续时间", fault_end_seq_exclusive: "故障结束位置（不含）", sample_rate_hz: "采样频率", window_points: "分析窗口长度", chunk_points: "样本分块长度", devices: "设备配置", interval_seconds: "采集批次间隔", queue_capacity: "采集队列容量", max_duration_seconds: "采集时限", max_disk_bytes: "存储容量上限" };
const PARAMETER_UNITS = { total_points: "点", noise_std: "g", degradation_rate_per_sample: "g / 样本", fault_amplitude: "g", sample_rate_hz: "Hz", window_points: "点", chunk_points: "点", interval_seconds: "秒", max_duration_seconds: "秒", max_disk_bytes: "字节" };
const FAULT_LABELS = { spike: "瞬态冲击", step: "阶跃偏移", burst: "振荡突发" };
function recordedParameters(detail) {
  const config = detail.configuration, values = { ...(config.parameters || {}) };
  for (const key of ["sample_rate_hz", "window_points", "fault_type", "fault_start_seq", "fault_duration_points", "fault_end_seq_exclusive"]) if (!Object.hasOwn(values, key) && Object.hasOwn(config, key)) values[key] = config[key];
  return values;
}
function parameterText(key, value, detail) {
  const scenario = detail.configuration.parameters?.profile || detail.run.scenario;
  const inactive = key.startsWith("fault_") && scenario !== "sudden_fault" || key === "degradation_rate_per_sample" && scenario !== "degradation";
  if (value === undefined || value === null) return inactive ? "不适用" : "未记录";
  let output;
  if (key === "profile") output = SCENARIO_LABELS[value] || String(value);
  else if (key === "fault_type") output = FAULT_LABELS[value] || String(value);
  else if (["fault_start_seq", "fault_duration_points", "fault_end_seq_exclusive"].includes(key) && finite(value)) {
    const unit = key === "fault_duration_points" ? "点" : "样本序号";
    const rate = detail.configuration.sample_rate_hz;
    output = `${value} ${unit}${finite(rate) && rate > 0 ? ` · ${value / rate} 秒` : ""}`;
  } else output = `${typeof value === "object" ? JSON.stringify(value) : String(value)}${PARAMETER_UNITS[key] ? ` ${PARAMETER_UNITS[key]}` : ""}`;
  return inactive ? `不适用（配置 ${output}）` : output;
}

export function configurationRows(runs) {
  const parameters = runs.map(recordedParameters), keys = [...new Set(parameters.flatMap((values) => Object.keys(values)))];
  const order = Object.keys(PARAMETER_LABELS);
  keys.sort((a, b) => (order.includes(a) ? order.indexOf(a) : 999) - (order.includes(b) ? order.indexOf(b) : 999) || a.localeCompare(b));
  return keys.map((key) => [PARAMETER_LABELS[key] || `其他参数（${key}）`, ...runs.map((detail, index) => parameterText(key, parameters[index][key], detail))]);
}

export function reportContentHTML(snapshot, headingLevel = 3) {
  const runs = snapshot.runs || [], heading = headingLevel === 2 ? "h2" : "h3";
  const tableRegion = (table, label) => `<div class="wb-table-scroll" role="region" tabindex="0" aria-label="${escapeHTML(label)}，可横向滚动">${table}</div>`;
  return `<${heading}>实际健康窗口</${heading}><div class="wb-chart wb-comparison-chart">${healthChartSVG(runs)}</div><div class="wb-legend" aria-label="运行曲线图例">${runs.map((d, i) => `<span><i class="wb-series-${i % COLORS.length}" aria-hidden="true"></i>${escapeHTML(d.run.device_id)} / ${escapeHTML(d.run.run_id)}</span>`).join("")}</div><${heading}>运行指标摘要</${heading}>${tableRegion(comparisonTable(runs), "运行指标比较")}<${heading}>运行参数对照</${heading}>${tableRegion(comparisonTable(runs, configurationRows(runs), "运行参数比较"), "运行参数比较")}<details class="wb-raw-configuration"><summary>查看原始配置与来源</summary>${runs.map((d) => `<section><h4>${escapeHTML(d.run.device_id)} / ${escapeHTML(d.run.run_id)}</h4><p>来源：${escapeHTML(SOURCE_LABELS[d.run.source_kind] || d.run.source_kind)}；读取时间：${escapeHTML(d.observed_at_utc || "未记录")}。</p><pre>${escapeHTML(JSON.stringify(d.configuration, null, 2))}</pre></section>`).join("")}</details>`;
}

export function exportCSV(snapshot) {
  const runs = snapshot.runs || [], headers = ["比较项", ...runs.map((d) => `${d.run.device_id} / ${d.run.run_id}`)];
  const configs = [...new Set(runs.flatMap((d) => Object.keys(d.configuration.parameters || {})))].sort().map((key) => [`配置.${key}`, ...runs.map((d) => Object.hasOwn(d.configuration.parameters || {}, key) ? JSON.stringify(d.configuration.parameters[key]) : null)]);
  return "\uFEFF" + [headers, ["快照生成时间", ...runs.map(() => snapshot.generated_at_utc || "")], ...comparisonRows(runs), ...configs].map((row) => row.map(csvCell).join(",")).join("\r\n");
}

export function exportHTML(snapshot) {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>G15 运行比较报告</title><style>body{margin:0;background:#feffef;color:#173f39;font-family:system-ui,sans-serif;line-height:1.7}main{max-width:1200px;margin:auto;padding:32px}h1{font-size:30px}h2{margin-top:32px}svg{width:100%;height:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:12px;text-align:left;border-bottom:1px solid #9aa89f;vertical-align:top;overflow-wrap:anywhere;min-width:150px}small{display:block}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#e5ebdf;padding:18px}.wb-table-scroll{overflow-x:auto}p{max-width:75ch}.wb-legend{display:flex;gap:20px;flex-wrap:wrap;margin-top:14px}.wb-legend span{padding:4px 10px;background:#28313c;color:#feffef}.wb-legend i{display:inline-block;width:16px;height:3px;vertical-align:middle;margin-right:6px}.wb-legend .wb-series-0{background:#80d8cf}.wb-legend .wb-series-1{background:#f3c77a}.wb-legend .wb-series-2{background:#8fbef2}.wb-legend .wb-series-3{background:#efa8cd}.wb-raw-configuration{margin-top:28px}summary{cursor:pointer}summary:focus-visible,[tabindex]:focus-visible{outline:2px solid #b87019;outline-offset:3px}.wb-scroll-hint{display:none;font-size:12px}.sr-only{position:absolute;width:1px;height:1px;padding:0;overflow:hidden;clip-path:inset(50%);white-space:nowrap}@media(max-width:650px){main{padding:20px 14px}.wb-scroll-hint{display:block}.wb-legend{font-size:12px;gap:8px}h1{font-size:24px}}@media print{body{background:white}main{padding:0}h2{break-after:avoid}table{font-size:11px}.wb-table-scroll{overflow:visible}td,th{min-width:0}.wb-scroll-hint{display:none}}</style></head><body><main><h1>G15 运行比较报告</h1><p>快照时间：${escapeHTML(snapshot.generated_at_utc || "未记录")}</p>${reportContentHTML(snapshot, 2)}</main></body></html>`;
}

export function createWorkbenchClient({ fetchImpl = globalThis.fetch, timeoutMs = 10000 } = {}) {
  async function request(path, { signal, body } = {}) {
    const controller = new AbortController(), abort = () => controller.abort();
    if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, timeoutMs);
    try {
      const response = await fetchImpl(path, { method: body ? "POST" : "GET", cache: "no-store", credentials: "same-origin", signal: controller.signal, headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json", "X-G15-Console-Action": "workbench" } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
      const value = await response.json();
      if (!response.ok) throw Object.assign(new Error(value?.error || "request_failed"), { code: value?.error, status: response.status });
      if (!value || typeof value !== "object") throw invalid();
      return value;
    } finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
  }
  const query = (path, values, signal) => request(`${path}?${new URLSearchParams(values)}`, { signal });
  return { catalog: (signal) => request("/api/workbench", { signal }), run: (id, signal) => query("/api/workbench/run", { id }, signal), compare: (ids, signal) => query("/api/workbench/compare", { ids: ids.join(",") }, signal), waveform: (values, signal) => query("/api/workbench/waveform", values, signal), dispositions: (id, signal) => query("/api/workbench/dispositions", { id }, signal), save: (values, signal) => request("/api/workbench/dispositions", { body: dispositionRequest(values), signal }) };
}

export function startWorkbench(documentRef = document, client = createWorkbenchClient()) {
  const $ = (id) => documentRef.getElementById(id), node = (tag, text = "", cls = "") => { const element = documentRef.createElement(tag); element.textContent = text; if (cls) element.className = cls; return element; };
  const text = (id, value) => { $(id).textContent = value; };
  const message = (id, value) => { text(id, value); $(id).hidden = !value; };
  let workspace = "replay", catalog = null, selectedId = "", detail = null, cursor = 0, snapshot = null, reading = false, saving = false, playing = false, disposed = false;
  let controller = null, waveController = null, saveController = null, generation = 0, waveGeneration = 0, timer = null, playTimer = null, readError = "", saveError = "", waveError = "", pendingSave = null, lastRead = null;
  let cachedWaveform = null;
  const selected = new Set(), choices = new Map();
  function status(state, label) { $("wb-connection").dataset.state = state; text("wb-status", label); }
  function errorText(error) { return ERRORS[error.code] || (error.name === "AbortError" ? "读取超时，请刷新重试。" : "读取失败，请检查服务连接后刷新。"); }
  function showErrors() { message("wb-error", [saveError, readError, waveError].filter(Boolean).join(" ")); }
  function stopPlayback() { playing = false; clearTimeout(playTimer); text("wb-play", "播放回放"); }
  function invalidate() { generation += 1; controller?.abort(); controller = null; reading = false; waveGeneration += 1; waveController?.abort(); waveController = null; clearTimeout(timer); }
  function maxCursor() { return Math.max(0, (detail?.available_points || 0) - 1); }
  function rate() { return finite(detail?.configuration?.sample_rate_hz) && detail.configuration.sample_rate_hz > 0 ? detail.configuration.sample_rate_hz : null; }
  function chartSize(element) { return { width: Math.max(200, Math.round(element.clientWidth || 900)), height: Math.max(150, Math.round(element.clientHeight || 255)) }; }
  function renderHealthCharts() {
    if (workspace === "replay") $("wb-health").innerHTML = detail?.windows.length ? healthChartSVG([detail], { ...chartSize($("wb-health")), cursorSeconds: rate() ? cursor / rate() : null }) : '<p class="lab-empty">暂无已分析健康窗口</p>';
    if (workspace === "compare" && snapshot?.runs.length) $("wb-compare-chart").innerHTML = healthChartSVG(snapshot.runs, chartSize($("wb-compare-chart")));
    const previewChart = $("wb-report-preview").querySelector(".wb-chart");
    if (workspace === "report" && previewChart && snapshot?.runs.length) previewChart.innerHTML = healthChartSVG(snapshot.runs, chartSize(previewChart));
  }
  function renderWaveformChart(data) {
    const size = chartSize($("wb-wave")), geometry = plotGeometry(data.time_seconds, data.vibration, size);
    if (!geometry) { $("wb-wave").replaceChildren(node("p", "该窗口暂无可读样本。", "lab-empty")); return; }
    const grid = geometry.yTicks.map((tick) => `<line x1="${geometry.bounds.left}" x2="${geometry.bounds.right}" y1="${tick.position}" y2="${tick.position}" stroke="#485966"/><text x="40" y="${tick.position + 4}" text-anchor="end">${format(tick.value, 2)}</text>`).join("");
    const ticks = geometry.xTicks.map((tick, index) => `<text x="${tick.position}" y="${size.height - 12}" text-anchor="${index === 0 ? "start" : index === 2 ? "end" : "middle"}">${format(tick.value, 2)} s</text>`).join("");
    $("wb-wave").innerHTML = `<svg viewBox="0 0 ${size.width} ${size.height}" role="img" aria-label="振动单位 g，横轴为本片段相对时间秒"><g fill="#c8d9df" font-size="11" font-family="sans-serif">${grid}${ticks}</g><path d="${geometry.path}" fill="none" stroke="#80d8cf" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
  }
  function renderStatus() {
    const connection = workbenchConnection({ reading, readError: !!(readError || waveError), detail, catalogAvailable: catalog?.available === true, reportReady: workspace === "report" && !!snapshot });
    status(connection.state, connection.label);
    text("wb-observed", lastRead ? `读取于 ${dateText(lastRead)}` : "尚未读取");
  }
  function renderCatalog() {
    const runs = catalog?.runs || [], oldValue = $("wb-run").value;
    const signature = runs.map((run) => `${run.id}|${run.status}`).join(";");
    if ($("wb-run").dataset.signature !== signature) {
      $("wb-run").replaceChildren(...runs.map((run) => { const option = node("option", `${run.device_id} · ${run.run_id || run.experiment_id || run.id} · ${readableRun(run) ? SOURCE_LABELS[run.source_kind] || "未知来源" : RUN_STATES[run.status] || "尚不可读"}`); option.value = run.id; option.disabled = !readableRun(run); return option; }));
      if (!runs.some(readableRun)) { const option = node("option", runs.length ? "实验完成后可读取结果" : "暂无可读运行"); option.value = ""; $("wb-run").prepend(option); }
      $("wb-run").dataset.signature = signature;
    }
    $("wb-run").value = selectedId || oldValue; $("wb-run").disabled = !runs.some(readableRun) || saving || !!pendingSave;
    const present = new Set(runs.map((run) => run.id));
    for (const id of selected) if (!present.has(id)) selected.delete(id);
    for (const [id, entry] of choices) if (!present.has(id)) { entry.label.remove(); choices.delete(id); }
    if (runs.length && !choices.size) $("wb-run-choices").replaceChildren();
    for (const run of runs) {
      let entry = choices.get(run.id);
      if (!entry) {
        const label = node("label", "", "wb-run-choice"), input = node("input"), span = node("span"), title = node("strong"), info = node("small"); input.type = "checkbox";
        input.addEventListener("change", () => { if (input.checked) selected.add(run.id); else selected.delete(run.id); renderSelection(); });
        span.append(title, info); label.append(input, span); $("wb-run-choices").append(label); entry = { label, input, title, info }; choices.set(run.id, entry);
      }
      entry.title.textContent = run.device_id; entry.info.textContent = `${run.run_id || run.experiment_id || run.id} · ${readableRun(run) ? SOURCE_LABELS[run.source_kind] || "未知" : RUN_STATES[run.status] || "尚不可读"}`; entry.readable = readableRun(run);
      if (!entry.readable) selected.delete(run.id);
    }
    if (!runs.length) $("wb-run-choices").replaceChildren(node("p", "暂无运行", "lab-empty"));
    renderSelection();
  }
  function renderSelection() {
    const limit = catalog?.limits?.max_compare || 4;
    for (const [id, entry] of choices) { entry.input.checked = selected.has(id); entry.input.disabled = !entry.readable || reading || !selected.has(id) && selected.size >= limit; }
    $("wb-apply-comparison").disabled = selected.size < 2 || reading;
    text("wb-selection-note", `已选择 ${selected.size} / ${limit} 个运行`);
  }
  function renderReplay() {
    const hasData = !!detail && detail.available_points > 0;
    for (const id of ["wb-cursor", "wb-seq", "wb-seek", "wb-play"]) $(id).disabled = !hasData;
    $("wb-previous").disabled = !hasData || cursor <= 0; $("wb-next").disabled = !hasData || cursor >= maxCursor();
    $("wb-cursor").max = String(maxCursor()); $("wb-cursor").value = String(cursor); $("wb-seq").max = String(maxCursor()); $("wb-seq").value = String(cursor);
    text("wb-cursor-value", hasData ? `seq ${format(cursor, 0)} · ${rate() ? format(cursor / rate(), 3) + " 秒" : "时间未知"}` : "—");
    text("wb-replay-source", detail ? `${detail.run.device_id} / ${detail.run.run_id} · ${SOURCE_LABELS[detail.run.source_kind] || "未知来源"}` : "暂无运行");
    text("wb-source-state", detail ? detail.run.source_kind === "live" ? "已提交样本" : "已存运行" : "等待运行");
    $("wb-source-state").dataset.state = detail ? "ready" : "waiting";
    renderHealthCharts();
    const current = replaySnapshot(detail, cursor);
    const facts = [["游标前完整窗口健康分", format(current.window?.health_score, 2)], ["截至游标告警", format(current.alarms.length, 0)], ["已提交样本", format(detail?.available_points, 0)], ["窗口范围", current.window ? `${current.window.start_seq}–${current.window.end_seq_exclusive - 1}` : "尚无完整窗口"]];
    $("wb-at-cursor").replaceChildren(...facts.map(([label, value]) => { const pair = node("div"); pair.append(node("dt", label), node("dd", value)); return pair; }));
    text("wb-alarm-count", `${current.alarms.length} 条 · 最近 8 条`);
    $("wb-replay-alarms").replaceChildren(...current.alarms.slice(-8).reverse().map((alarm) => {
      const available = alarmAvailableSequence(alarm, detail.windows), li = node("li"), heading = node("div", "", "wb-event-heading"), level = node("span", `${alarm.level === "red" ? "红色" : alarm.level === "yellow" ? "黄色" : alarm.level} · ${alarm.rule_id}`, "wb-alarm-level"), jump = node("button", `定位 seq ${available}`, "button button-quiet"); level.dataset.level = alarm.level; jump.type = "button"; jump.addEventListener("click", () => seek(available)); heading.append(level, jump); li.append(heading, node("p", alarm.reason)); return li;
    }));
    if (!current.alarms.length) $("wb-replay-alarms").append(node("li", detail ? "截至当前游标未记录告警。" : "尚未读取运行。", "lab-empty"));
    $("wb-advice").replaceChildren(...(current.advice.length ? current.advice.map((advice) => node("li", advice)) : [node("li", "截至当前游标暂无告警关联的维护建议。") ]));
  }
  function renderDispositions() {
    const alarms = detail?.alarms || [], current = $("wb-alarm").value, signature = `${detail?.run.id}|${alarms.map((a) => a.alarm_key).join(",")}`;
    if ($("wb-alarm").dataset.signature !== signature) {
      $("wb-alarm").replaceChildren(...alarms.map((alarm) => { const option = node("option", `${alarm.level} · ${alarm.rule_id} · seq ${alarm.start_seq}`); option.value = alarm.alarm_key; return option; }));
      if (alarms.some((alarm) => alarm.alarm_key === current)) $("wb-alarm").value = current;
      if (!alarms.length) { const option = node("option", "本次运行暂无告警"); option.value = ""; $("wb-alarm").append(option); }
      $("wb-alarm").dataset.signature = signature;
    }
    const alarm = alarms.find((item) => item.alarm_key === $("wb-alarm").value), events = (detail?.dispositions || []).filter((event) => event.alarm_key === alarm?.alarm_key);
    text("wb-alarm-detail", alarm ? alarm.reason : "暂无告警");
    for (const id of ["wb-alarm", "wb-operator", "wb-action", "wb-note"]) $(id).disabled = !alarm || saving || !!pendingSave;
    const confirmed = events.some((event) => event.action === "acknowledge");
    $("wb-action").querySelector('[value="acknowledge"]').disabled = confirmed;
    if (confirmed && !pendingSave && !saving) $("wb-action").value = "note";
    $("wb-save").disabled = !alarm || saving || reading || !!readError;
    text("wb-save", saving ? "正在保存…" : pendingSave ? "重试同一条记录" : "保存处置记录");
    $("wb-audit").replaceChildren(...events.slice().reverse().map((event) => {
      const li = node("li"), heading = node("div", "", "wb-event-heading"), time = node("time", dateText(event.created_at_utc)); time.dateTime = event.created_at_utc;
      heading.append(node("strong", event.action === "acknowledge" ? "已确认" : "处理备注"), node("span", event.operator), time);
      const audit = node("details"), summary = node("summary", `记录 #${event.sequence} · 查看校验信息`), metadata = node("p"); metadata.append(node("code", `请求标识：${event.request_id}\n前序摘要：${event.previous_hash || "—"}\n本条摘要：${event.event_hash || "—"}`)); audit.append(summary, metadata); li.append(heading, node("p", event.note), audit); return li;
    }));
    if (!events.length) $("wb-audit").append(node("li", alarm ? "该告警尚无处置记录。" : "暂无处置记录", "lab-empty"));
    renderCatalog();
  }
  function renderComparison() {
    const runs = snapshot?.runs || [];
    $("wb-compare-chart").innerHTML = runs.length ? healthChartSVG(runs, chartSize($("wb-compare-chart"))) : '<p class="lab-empty">暂无对比数据</p>';
    $("wb-compare-legend").replaceChildren(...runs.map((value, index) => { const label = node("span"), mark = node("i", "", `wb-series-${index % COLORS.length}`); label.append(mark, documentRef.createTextNode(`${value.run.device_id} / ${value.run.run_id}`)); return label; }));
    $("wb-comparison-table").innerHTML = runs.length ? comparisonTable(runs) : '<p class="lab-empty">尚无对比数据</p>';
    for (const type of ["html", "json", "csv"]) $(`wb-export-${type}`).disabled = !runs.length;
    text("wb-report-source", runs.length ? `当前快照：${runs.length} 个运行 · ${dateText(snapshot.generated_at_utc)}` : "暂无比较快照");
    const preview = $("wb-report-preview"); preview.replaceChildren();
    if (runs.length) preview.innerHTML = reportContentHTML(snapshot);
    else preview.append(node("p", "暂无比较快照", "lab-empty"));
    renderHealthCharts();
  }
  async function loadWave() {
    waveController?.abort(); const token = ++waveGeneration, request = replayWaveRange(detail, cursor);
    cachedWaveform = null;
    waveError = ""; showErrors();
    if (!request || workspace !== "replay") { $("wb-wave").replaceChildren(node("p", "暂无可读原始样本。", "lab-empty")); text("wb-wave-range", "无已提交样本"); renderStatus(); return; }
    waveController = new AbortController(); const source = detail.run;
    $("wb-wave").replaceChildren(node("p", "正在读取游标前后原始样本…", "lab-empty"));
    try {
      const data = normalizeWaveform(await client.waveform(request, waveController.signal));
      if (token !== waveGeneration || disposed) return;
      if (data.run_id !== source.run_id || data.device_id !== source.device_id) throw invalid();
      cachedWaveform = data; renderWaveformChart(data);
      text("wb-wave-range", `seq ${data.start_seq}–${data.end_seq_exclusive - 1} · ${data.count} 点 · g`);
    } catch (error) { if (token !== waveGeneration || disposed) return; waveError = errorText(error); $("wb-wave").replaceChildren(node("p", "原始片段读取失败，未绘制替代信号。", "lab-empty")); showErrors(); }
    finally { if (token === waveGeneration && !disposed) renderStatus(); }
  }
  function seek(value, { playback = false } = {}) { if (!playback) stopPlayback(); if (!Number.isSafeInteger(value)) { message("wb-feedback", "样本序号须为整数。"); return; } cursor = Math.max(0, Math.min(maxCursor(), value)); renderReplay(); void loadWave(); }
  function tick() { if (!playing || documentRef.hidden) return; const next = Math.min(maxCursor(), cursor + (detail?.configuration.window_points || 256)); seek(next, { playback: true }); if (next >= maxCursor()) stopPlayback(); else playTimer = setTimeout(tick, 1200); }
  function schedule() { clearTimeout(timer); if (!disposed && !documentRef.hidden) timer = setTimeout(() => { if (workspace === "replay" && detail?.run.source_kind === "live" && !playing || workspace === "disposition" && !saving && !pendingSave) void refresh(); else { renderStatus(); schedule(); } }, 5000); }
  async function refresh({ compare = false } = {}) {
    if (disposed || documentRef.hidden || reading) return;
    const token = ++generation; controller = new AbortController(); reading = true; readError = ""; renderStatus(); renderSelection(); showErrors();
    try {
      const value = await client.catalog(controller.signal);
      if (token !== generation || disposed) return;
      if (value.available !== true || !Array.isArray(value.runs)) throw invalid();
      catalog = value;
      if (!value.runs.some((run) => run.id === selectedId && readableRun(run))) { selectedId = value.runs.find(readableRun)?.id || ""; detail = null; cursor = 0; }
      renderCatalog();
      if (compare && selected.size >= 2) {
        const result = await client.compare([...selected], controller.signal);
        if (token !== generation || disposed) return;
        if (!Array.isArray(result.runs) || result.runs.length !== selected.size || result.runs.some((run) => !selected.has(run.run?.id))) throw invalid();
        result.runs.forEach(normalizeRunDetail); snapshot = result; renderComparison();
      }
      if (selectedId && (workspace === "replay" || workspace === "disposition")) {
        const result = normalizeRunDetail(await client.run(selectedId, controller.signal));
        if (token !== generation || disposed) return;
        if (result.run.id !== selectedId) throw invalid();
        const changed = detail?.run.id !== result.run.id; detail = result;
        if (pendingSave && !saving && pendingSave.id === detail.run.id && detail.dispositions?.some((entry) => entry.request_id === pendingSave.request_id)) {
          pendingSave = null; saveError = ""; $("wb-note").value = ""; message("wb-feedback", "已在保存记录中找到此前请求，确认保存成功；没有重复追加。");
        }
        if (changed) cursor = maxCursor(); else cursor = Math.min(cursor, maxCursor());
        renderReplay(); renderDispositions(); if (workspace === "replay") void loadWave();
      } else if (!selectedId) { detail = null; renderReplay(); renderDispositions(); }
      lastRead = new Date().toISOString();
    } catch (error) { if (token !== generation || disposed) return; readError = errorText(error); }
    finally { if (token === generation && !disposed) { reading = false; renderStatus(); renderSelection(); renderDispositions(); showErrors(); schedule(); } }
  }
  function switchWorkspace(next) {
    if (!["replay", "compare", "disposition", "report"].includes(next)) return;
    stopPlayback(); invalidate(); workspace = next; readError = ""; waveError = ""; showErrors();
    for (const button of documentRef.querySelectorAll(".wb-tabs [role=tab]")) { const current = button.dataset.workspace === next; button.setAttribute("aria-selected", String(current)); button.tabIndex = current ? 0 : -1; $(`wb-${button.dataset.workspace}`).hidden = !current; }
    renderHealthCharts();
    if (next === "report") { renderComparison(); renderStatus(); schedule(); } else void refresh();
  }
  async function saveDisposition(event) {
    event.preventDefault(); if (saving || !detail) return;
    try { pendingSave ||= dispositionRequest({ id: detail.run.id, alarm_key: $("wb-alarm").value, action: $("wb-action").value, operator: $("wb-operator").value, note: $("wb-note").value, request_id: crypto.randomUUID() }); }
    catch (error) { saveError = error.message; showErrors(); return; }
    saving = true; saveError = ""; saveController = new AbortController(); renderDispositions(); showErrors();
    try {
      const result = await client.save(pendingSave, saveController.signal);
      if (disposed) return;
      if (result.event?.request_id !== pendingSave.request_id) throw invalid();
      if (detail.run.id === pendingSave.id && !(detail.dispositions || []).some((entry) => entry.request_id === result.event.request_id)) (detail.dispositions ||= []).push(result.event);
      pendingSave = null; $("wb-note").value = ""; message("wb-feedback", result.duplicate ? "已核对同一请求：该条记录此前已保存，没有重复追加。" : "处置记录已保存");
    } catch (error) {
      if (disposed) return;
      const rejected = [400, 404, 409, 413, 429].includes(error.status);
      saveError = rejected ? errorText(error) : "保存结果尚未确认。请刷新记录核对，或重试同一条记录；请求标识将保持不变，避免重复追加。";
      if (rejected) pendingSave = null;
    } finally { saving = false; saveController = null; if (!disposed) { renderDispositions(); showErrors(); } }
  }
  function download(type) {
    if (!snapshot?.runs.length) return;
    const contents = type === "html" ? exportHTML(snapshot) : type === "csv" ? exportCSV(snapshot) : JSON.stringify(snapshot, null, 2), mime = { html: "text/html;charset=utf-8", csv: "text/csv;charset=utf-8", json: "application/json;charset=utf-8" };
    const blob = new Blob([contents], { type: mime[type] }), url = URL.createObjectURL(blob), link = node("a"); link.href = url; link.download = `g15-comparison-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.${type}`; documentRef.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    message("wb-feedback", `已生成 ${type.toUpperCase()} 报告，数据来自 ${dateText(snapshot.generated_at_utc)} 的比较快照。`);
  }
  for (const tab of documentRef.querySelectorAll(".wb-tabs [role=tab]")) {
    tab.addEventListener("click", () => switchWorkspace(tab.dataset.workspace));
    tab.addEventListener("keydown", (event) => { const names = ["replay", "compare", "disposition", "report"]; if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault(); const next = event.key === "Home" ? names[0] : event.key === "End" ? names.at(-1) : names[(names.indexOf(workspace) + (event.key === "ArrowRight" ? 1 : 3)) % 4]; switchWorkspace(next); $(`wb-tab-${next}`).focus(); });
  }
  $("wb-run").addEventListener("change", () => { stopPlayback(); invalidate(); selectedId = $("wb-run").value; detail = null; cachedWaveform = null; cursor = 0; $("wb-wave").replaceChildren(node("p", "正在读取所选运行…", "lab-empty")); text("wb-wave-range", "等待所选运行的样本"); renderReplay(); renderDispositions(); void refresh(); });
  $("wb-refresh").addEventListener("click", () => { invalidate(); void refresh({ compare: ["compare", "report"].includes(workspace) && selected.size >= 2 }); });
  $("wb-apply-comparison").addEventListener("click", () => { invalidate(); void refresh({ compare: true }); });
  $("wb-cursor").addEventListener("input", () => { stopPlayback(); cursor = Number($("wb-cursor").value); renderReplay(); });
  $("wb-cursor").addEventListener("change", () => void loadWave());
  $("wb-seek").addEventListener("click", () => seek(Number($("wb-seq").value)));
  $("wb-seq").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); seek(Number($("wb-seq").value)); } });
  $("wb-previous").addEventListener("click", () => seek(cursor - (detail?.configuration.window_points || 256)));
  $("wb-next").addEventListener("click", () => seek(cursor + (detail?.configuration.window_points || 256)));
  $("wb-play").addEventListener("click", () => { if (playing) stopPlayback(); else { if (cursor >= maxCursor()) cursor = 0; playing = true; text("wb-play", "暂停回放"); tick(); } });
  $("wb-alarm").addEventListener("change", renderDispositions);
  $("wb-disposition-form").addEventListener("submit", (event) => void saveDisposition(event));
  $("wb-report-select").addEventListener("click", () => { switchWorkspace("compare"); $("wb-tab-compare").focus(); });
  for (const type of ["html", "json", "csv"]) $(`wb-export-${type}`).addEventListener("click", () => download(type));
  const visibility = () => { if (documentRef.hidden) { stopPlayback(); invalidate(); } else void refresh(); };
  documentRef.addEventListener("visibilitychange", visibility);
  let observedSize = "";
  const resize = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
    const size = `${$("main-content").clientWidth}:${window.innerWidth}`;
    if (disposed || documentRef.hidden || size === observedSize) return;
    observedSize = size; renderHealthCharts();
    if (workspace === "replay" && cachedWaveform && cachedWaveform.run_id === detail?.run.run_id && cachedWaveform.device_id === detail?.run.device_id) renderWaveformChart(cachedWaveform);
  }) : null;
  resize?.observe($("main-content"));
  const destroy = () => { disposed = true; stopPlayback(); invalidate(); saveController?.abort(); resize?.disconnect(); documentRef.removeEventListener("visibilitychange", visibility); };
  window.addEventListener("pagehide", destroy, { once: true }); window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
  renderReplay(); renderComparison(); void refresh();
  return { refresh, destroy };
}

if (typeof document !== "undefined" && document.getElementById("wb-replay")) startWorkbench();
