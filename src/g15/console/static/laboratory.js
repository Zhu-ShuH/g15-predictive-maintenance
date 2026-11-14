const POLL_MS = 2000;
const STALE_MS = 10000;
const PROFILES = { normal: "健康基线", degradation: "持续退化", sudden_fault: "突发故障" };
const FAULT_TYPES = { spike: "瞬态冲击", step: "阶跃偏移", burst: "振荡突发" };
const ACTIVE_STATES = new Set(["queued", "generating", "analysing", "reporting"]);
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const format = (value, precision = 3) => finite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: precision }) : "—";
const timeText = (value) => {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";
};
const pairKey = (device) => JSON.stringify([device.run_id, device.device_id]);
const invalid = () => Object.assign(new Error("invalid_response"), { code: "invalid_response" });

export function experimentParameters(values) {
  const read = (name) => {
    const value = values[name];
    if (typeof value !== "number" && typeof value !== "string" || typeof value === "string" && !value.trim()) throw new Error("参数不能为空。");
    const result = Number(value);
    if (!Number.isFinite(result)) throw new Error("参数必须为有限数字。");
    return result;
  };
  if (!Object.hasOwn(PROFILES, values.profile)) throw new Error("请选择支持的仿真场景。");
  const parameters = { profile: values.profile, seed: read("seed"), total_points: read("total_points"), noise_std: read("noise_std"),
    degradation_rate_per_sample: values.profile === "degradation" ? read("degradation_rate_per_sample") : 0,
    fault_amplitude: values.profile === "sudden_fault" ? read("fault_amplitude") : 0 };
  if (!integer(parameters.seed) || parameters.seed > 2147483647) throw new Error("随机种子需为 0 至 2,147,483,647 的整数。");
  if (!integer(parameters.total_points) || parameters.total_points < 8192 || parameters.total_points > 65536 || parameters.total_points % 256) throw new Error("样本点数需为 8,192 至 65,536 之间的 256 整数倍。");
  if (parameters.noise_std < 0 || parameters.noise_std > 1) throw new Error("噪声标准差需在 0 至 1 g 之间。");
  if (parameters.degradation_rate_per_sample < 0 || parameters.degradation_rate_per_sample > 0.001) throw new Error("每样本退化增量需在 0 至 0.001 之间。");
  if (parameters.fault_amplitude < 0 || parameters.fault_amplitude > 10) throw new Error("故障振幅需在 0 至 10 g 之间。");
  const injectionKeys = ["fault_type", "fault_start_seq", "fault_duration_points"];
  if (injectionKeys.some((key) => Object.hasOwn(values, key))) {
    if (parameters.profile !== "sudden_fault" || !injectionKeys.every((key) => Object.hasOwn(values, key)) || !Object.hasOwn(FAULT_TYPES, values.fault_type)) throw new Error("故障注入需同时指定支持的类型、开始时间与持续时间。");
    const start = read("fault_start_seq"), duration = read("fault_duration_points");
    if (!integer(start) || !integer(duration) || duration < (values.fault_type === "burst" ? 3 : 1) || start + duration > parameters.total_points) throw new Error("故障区间须为整数样本点，完整位于本次运行内；振荡突发至少持续 3 点。");
    Object.assign(parameters, { fault_type: values.fault_type, fault_start_seq: start, fault_duration_points: duration });
  }
  return parameters;
}

export function secondsToSamples(value, rate = 256) {
  if (typeof value !== "number" && typeof value !== "string" || String(value).trim() === "") throw new Error("请填写注入时间。");
  const samples = Number(value) * rate;
  if (!Number.isSafeInteger(samples) || samples < 0) throw new Error(`时间须精确对应整数样本点（1 / ${rate} 秒），不会自动舍入。`);
  return samples;
}

export function injectionDraft(parameters, configuration = null) {
  if (parameters.profile !== "sudden_fault") return { ...parameters };
  return { ...parameters, fault_type: parameters.fault_type ?? configuration?.fault_type ?? "spike",
    fault_start_seq: parameters.fault_start_seq ?? configuration?.fault_start_seq ?? Math.floor(parameters.total_points / 2 / 256) * 256,
    fault_duration_points: parameters.fault_duration_points ?? configuration?.fault_duration_points ?? 512 };
}

export function normalizeWaveform(value) {
  if (!value || typeof value.run_id !== "string" || typeof value.device_id !== "string" || !finite(value.sample_rate_hz) || value.sample_rate_hz <= 0 || !integer(value.count) || value.count > 4096 || !integer(value.start_seq) || !integer(value.end_seq_exclusive) || !integer(value.available_points) || value.end_seq_exclusive - value.start_seq !== value.count || value.end_seq_exclusive > value.available_points || value.units?.vibration !== "g" || value.units?.temperature !== "degC") throw invalid();
  for (const field of ["seq", "timestamp_ns", "time_seconds", "vibration", "temperature"]) if (!Array.isArray(value[field]) || value[field].length !== value.count) throw invalid();
  for (let i = 0; i < value.count; i += 1) {
    if (value.seq[i] !== value.start_seq + i || typeof value.timestamp_ns[i] !== "string" || !/^-?\d{1,19}$/.test(value.timestamp_ns[i]) || !finite(value.time_seconds[i]) || i > 0 && value.time_seconds[i] <= value.time_seconds[i - 1] || !finite(value.vibration[i]) || !finite(value.temperature[i])) throw invalid();
    const actualTime = Number(BigInt(value.timestamp_ns[i]) - BigInt(value.timestamp_ns[0])) / 1e9;
    if (Math.abs(value.time_seconds[i] - actualTime) > 1e-9) throw invalid();
  }
  if (value.spectrum !== null && value.spectrum !== undefined) {
    const spectrum = value.spectrum;
    if (!Array.isArray(spectrum.frequency_hz) || !Array.isArray(spectrum.amplitude_g) || spectrum.frequency_hz.length !== spectrum.amplitude_g.length || spectrum.frequency_hz.length > 1024 || !integer(spectrum.sample_count) || spectrum.sample_count > value.count || !integer(spectrum.start_seq) || !integer(spectrum.end_seq_exclusive) || spectrum.start_seq < value.start_seq || spectrum.end_seq_exclusive > value.end_seq_exclusive || spectrum.end_seq_exclusive - spectrum.start_seq !== spectrum.sample_count) throw invalid();
    for (let i = 0; i < spectrum.frequency_hz.length; i += 1) if (!finite(spectrum.frequency_hz[i]) || spectrum.frequency_hz[i] < 0 || spectrum.frequency_hz[i] > value.sample_rate_hz / 2 + 1e-8 || i > 0 && spectrum.frequency_hz[i] <= spectrum.frequency_hz[i - 1] || !finite(spectrum.amplitude_g[i]) || spectrum.amplitude_g[i] < 0) throw invalid();
  }
  return value;
}

export function waveformStatistics(waveform) {
  if (!waveform?.count) return { rms: null, peakToPeak: null, peakFrequency: null };
  let minimum = Infinity, maximum = -Infinity, scale = 0;
  for (const sample of waveform.vibration) { minimum = Math.min(minimum, sample); maximum = Math.max(maximum, sample); scale = Math.max(scale, Math.abs(sample)); }
  const rms = scale ? scale * Math.sqrt(waveform.vibration.reduce((sum, value) => sum + (value / scale) ** 2, 0) / waveform.count) : 0;
  let peakFrequency = null, peakAmplitude = 0;
  for (let i = 0; i < (waveform.spectrum?.frequency_hz.length || 0); i += 1) if (waveform.spectrum.frequency_hz[i] > 0 && waveform.spectrum.amplitude_g[i] > peakAmplitude) { peakFrequency = waveform.spectrum.frequency_hz[i]; peakAmplitude = waveform.spectrum.amplitude_g[i]; }
  return { rms: finite(rms) ? rms : null, peakToPeak: finite(maximum - minimum) ? maximum - minimum : null, peakFrequency };
}

export function plotGeometry(xValues, yValues, { width = 600, height = 230, includeZero = false, fixedY = null } = {}) {
  if (!Array.isArray(xValues) || !Array.isArray(yValues) || !xValues.length || xValues.length !== yValues.length || xValues.some((value, index) => !finite(value) || index > 0 && value < xValues[index - 1]) || yValues.some((value) => !finite(value))) return null;
  const bounds = { left: 49, right: Math.max(52, width - 13), top: 15, bottom: Math.max(20, height - 34) };
  const xmin = xValues[0], xmax = xValues[xValues.length - 1] > xmin ? xValues[xValues.length - 1] : xmin + 1;
  let ymin = fixedY ? fixedY[0] : Math.min(...yValues), ymax = fixedY ? fixedY[1] : Math.max(...yValues);
  if (!fixedY) {
    if (includeZero) { ymin = Math.min(ymin, 0); ymax = Math.max(ymax, 0); }
    const pad = (ymax - ymin) * 0.09 || Math.max(Math.abs(ymax) * 0.05, 0.1);
    ymin -= includeZero && ymin === 0 ? 0 : pad; ymax += pad;
  }
  if (!finite(ymin) || !finite(ymax) || ymax <= ymin) return null;
  const x = (value) => bounds.left + (value - xmin) / (xmax - xmin) * (bounds.right - bounds.left);
  const y = (value) => bounds.bottom - (value - ymin) / (ymax - ymin) * (bounds.bottom - bounds.top);
  const path = xValues.map((value, index) => `${index ? "L" : "M"}${x(value).toFixed(2)} ${y(yValues[index]).toFixed(2)}`).join(" ");
  return { path, bounds, xmin, xmax, ymin, ymax, points: xValues.map((value, index) => ({ x: x(value), y: y(yValues[index]) })),
    xTicks: [0, 0.5, 1].map((fraction) => ({ position: x(xmin + (xmax - xmin) * fraction), value: xmin + (xmax - xmin) * fraction })),
    yTicks: [0, 1 / 3, 2 / 3, 1].map((fraction) => ({ position: y(ymin + (ymax - ymin) * fraction), value: ymin + (ymax - ymin) * fraction })),
    zeroY: ymin < 0 && ymax > 0 ? y(0) : null };
}

export function experimentStatus(experiment) {
  const labels = { queued: "等待执行", generating: "正在生成", analysing: "正在分析", reporting: "正在生成报告", completed: "执行完成", failed: "执行失败" };
  return { label: labels[experiment?.status] || "状态未知", state: Object.hasOwn(labels, experiment?.status) ? experiment.status : "waiting" };
}

export function experimentDraft(experiment) {
  if (!experiment || typeof experiment.id !== "string" || !experiment.parameters) throw new Error("当前运行没有可载入的参数。");
  return experimentParameters(experiment.parameters);
}

export function experimentParameterRows(parameters, configuration = null) {
  if (!parameters) return [];
  const measurement = (value, unit) => finite(value) ? `${value} ${unit}` : "—";
  const inactive = (value) => finite(value) ? `不适用（配置 ${value}）` : "不适用";
  const rows = [["场景", PROFILES[parameters.profile] || "未知场景"], ["随机种子", format(parameters.seed, 0)], ["样本点数", finite(parameters.total_points) ? `${format(parameters.total_points, 0)} 点` : "—"],
    ["噪声标准差", measurement(parameters.noise_std, "g")], ["每样本退化增量", parameters.profile === "degradation" ? measurement(parameters.degradation_rate_per_sample, "g / 样本") : inactive(parameters.degradation_rate_per_sample)],
    ["故障振幅", parameters.profile === "sudden_fault" ? measurement(parameters.fault_amplitude, "g") : inactive(parameters.fault_amplitude)]];
  if (parameters.profile === "sudden_fault" && (Object.hasOwn(parameters, "fault_type") || configuration)) {
    const plan = injectionDraft(parameters, configuration);
    rows.push(["注入类型", FAULT_TYPES[plan.fault_type] || "未知"], ["计划开始", `${plan.fault_start_seq / 256} 秒 · seq ${plan.fault_start_seq}`], ["计划持续", `${plan.fault_duration_points / 256} 秒 · ${plan.fault_duration_points} 点`]);
  }
  return rows;
}

export function connectionPresentation({ mode, following = true, readError = false, liveStatus = null, catalogueAvailable = false, experiment = null, creating = false }) {
  if (readError) return { state: "error", label: "读取失败" };
  if (mode === "live") {
    if (liveStatus?.state === "error") return { state: "error", label: liveStatus.label };
    if (!following) return { state: "paused", label: "观察已暂停" };
    const state = liveStatus?.state;
    return { state: ["ready", "error", "stale", "paused"].includes(state) ? state : "waiting", label: liveStatus?.label || "正在连接" };
  }
  if (creating) return { state: "waiting", label: "正在创建实验" };
  if (!catalogueAvailable) return { state: "waiting", label: "等待实验服务" };
  if (!experiment) return { state: "ready", label: "独立实验就绪" };
  const status = experimentStatus(experiment);
  return { state: status.state === "failed" ? "error" : status.state === "completed" ? "ready" : "waiting", label: status.label };
}

export function reportSummary(summary) {
  if (!summary) return { verdict: "尚未生成报告", state: "waiting", items: [] };
  const evaluated = summary.verifiable === true;
  return { verdict: evaluated && summary.passed === true ? "场景报告 · 通过" : evaluated && summary.passed === false ? "场景报告 · 未通过" : "场景报告 · 未判定",
    state: evaluated && summary.passed === true ? "completed" : evaluated && summary.passed === false ? "failed" : "waiting",
    items: [["已分析样本", format(summary.analysed_points, 0)], ["健康分最低值", format(summary.health_score_min, 2)], ["告警事件", format(summary.alarm_count, 0)],
      ["受控故障事件", format(summary.fault_events, 0)], ["检出率", summary.fault_events === 0 && summary.detection_rate === null ? "不适用" : finite(summary.detection_rate) ? `${format(summary.detection_rate * 100, 1)}%` : "未计算"], ["误报事件", format(summary.false_alarm_count, 0)]] };
}

export function initialExperimentStart(detail, count) {
  const total = detail?.experiment?.parameters?.total_points;
  if (!integer(total) || !integer(count) || !count) return 0;
  const maximum = Math.max(0, total - count), profile = detail.experiment.parameters.profile;
  if (profile === "sudden_fault" && integer(detail.configuration?.fault_start_seq)) {
    const before = count >= 1024 ? Math.min(512, count / 4) : 0;
    return Math.min(maximum, Math.max(0, Math.floor((detail.configuration.fault_start_seq - before) / 256) * 256));
  }
  return profile === "degradation" ? maximum : 0;
}

const ERROR_TEXT = {
  laboratory_busy: "已有实验正在执行，请等待完成后再运行。",
  laboratory_full: "已达到本机实验数量上限，当前记录仍可查看。",
  laboratory_storage_limit: "实验存储空间已达到上限，当前记录仍可查看。",
  laboratory_unavailable: "当前服务未启用实验台，请检查本机服务后刷新。",
  experiment_not_ready: "实验尚未完成，完成后即可查看原始波形。",
  not_found: "所选运行当前不可用，请刷新列表后重新选择。",
  live_unavailable: "当前持续仿真不可用，请检查本机服务或切换到独立实验。",
  waveform_unavailable: "当前运行的原始样本暂不可读取，请稍后刷新。",
  waveform_not_found: "未找到所选运行的原始分片，请检查该运行的保存状态。",
  waveform_range_unavailable: "所选样本位置超出已提交范围，请选择其他片段。",
  waveform_busy: "原始样本读取正在进行，请稍后刷新。",
  waveform_invalid_data: "原始分片未通过完整性检查，未绘制曲线。",
  waveform_invalid_cadence: "原始样本的时间间隔不符合声明的采样频率，未绘制曲线。",
  waveform_budget_exceeded: "当前片段超过读取限制，请减小显示样本数。",
  invalid_request: "参数未被服务接受，请核对输入与当前运行后重试。",
  forbidden_origin: "服务拒绝此次操作，请从本机控制台地址重新打开。",
  invalid_response: "返回数据不完整，未绘制曲线。请刷新或检查本机服务。",
};

export function createLaboratoryClient({ fetchImpl = globalThis.fetch, timeoutMs = 8000 } = {}) {
  async function request(path, { signal, body } = {}) {
    const controller = new AbortController(), abort = () => controller.abort();
    if (signal?.aborted) controller.abort(); else signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, timeoutMs);
    try {
      const response = await fetchImpl(path, { method: body ? "POST" : "GET", cache: "no-store", credentials: "same-origin", signal: controller.signal,
        headers: body ? { Accept: "application/json", "Content-Type": "application/json", "X-G15-Console-Action": "laboratory" } : { Accept: "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}) });
      const result = await response.json();
      if (!response.ok) throw Object.assign(new Error(result?.error || "request_failed"), { code: result?.error, status: response.status });
      if (!result || typeof result !== "object") throw invalid();
      return result;
    } finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
  }
  return { live: (signal) => request("/api/live", { signal }), list: (signal) => request("/api/laboratory", { signal }),
    detail: (id, signal) => request(`/api/laboratory/experiment?${new URLSearchParams({ id })}`, { signal }),
    waveform: (parameters, signal) => request(`/api/waveform?${new URLSearchParams(parameters)}`, { signal }),
    experimentWaveform: (parameters, signal) => request(`/api/laboratory/waveform?${new URLSearchParams(parameters)}`, { signal }),
    create: (parameters, signal) => request("/api/laboratory/experiments", { signal, body: experimentParameters(parameters) }) };
}

export function startLaboratoryPage(documentRef = document, client = createLaboratoryClient()) {
  const $ = (id) => documentRef.getElementById(id);
  const text = (id, value) => { $(id).textContent = value; };
  const node = (tag, value, className = "") => { const element = documentRef.createElement(tag); if (value !== undefined) element.textContent = value; if (className) element.className = className; return element; };
  const svgNode = (tag, attributes = {}, value) => { const element = documentRef.createElementNS("http://www.w3.org/2000/svg", tag); for (const [name, attr] of Object.entries(attributes)) element.setAttribute(name, String(attr)); if (value !== undefined) element.textContent = value; return element; };
  let mode = "live", following = true, count = 2048, position = 0, autoPosition = true;
  let live = null, liveReadAt = 0, liveKey = "", catalogue = null, selectedId = "", detail = null, waveform = null;
  let reading = false, creating = false, readError = false, destroyed = false, generation = 0, timer = null, pollAbort = null, mutationAbort = null;
  let waveformState = "waiting", waveformMessage = "等待样本", selectedSample = null, mainPlot = null, detailSignature = "", presetLoaded = false, operationError = "";
  const plots = new Map(), historyRows = new Map();
  const initialQuery = new URLSearchParams(window.location.search);
  if (initialQuery.get("mode") === "experiment" || initialQuery.get("id")) { mode = "experiment"; selectedId = initialQuery.get("id") || ""; }

  function message(id, value) { text(id, value); $(id).hidden = !value; }
  function emptyChart(id, value) { const host = $(id); host.replaceChildren(node("p", value, "lab-empty")); host.setAttribute("aria-label", value); plots.delete(id); if (id === "lab-vibration-chart") mainPlot = null; }
  function clearWave(messageText = "正在读取真实样本…") {
    waveform = null; selectedSample = null;
    for (const id of ["lab-vibration-chart", "lab-temperature-chart", "lab-spectrum-chart"]) emptyChart(id, messageText);
    for (const id of ["lab-rms", "lab-ptp", "lab-dominant", "lab-sample-count", "lab-wave-range"]) text(id, "—");
    $("lab-sample-inspector").disabled = true;
    text("lab-sample-readout", "—");
    text("lab-wave-provenance", "—");
  }
  function drawChart(id, xs, ys, options) {
    const host = $(id), width = Math.max(220, host.clientWidth || 600), height = host.clientHeight || 230;
    const geometry = plotGeometry(xs, ys, { width, height, ...options });
    if (!geometry) { emptyChart(id, "当前片段没有可绘制的数据。"); return null; }
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
    const { bounds } = geometry;
    for (const tick of geometry.yTicks) svg.append(svgNode("path", { d: `M${bounds.left} ${tick.position}H${bounds.right}`, class: "lab-grid-line" }), svgNode("text", { x: bounds.left - 7, y: tick.position + 4, "text-anchor": "end" }, format(tick.value, 2)));
    for (const [index, tick] of geometry.xTicks.entries()) svg.append(svgNode("text", { x: tick.position, y: height - 11, "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle" }, `${format(tick.value, 2)}${options.xUnit || ""}`));
    if (geometry.zeroY !== null) svg.append(svgNode("path", { d: `M${bounds.left} ${geometry.zeroY}H${bounds.right}`, class: "lab-zero-line" }));
    const clipId = `${id}-clip`, defs = svgNode("defs"), clip = svgNode("clipPath", { id: clipId });
    clip.append(svgNode("rect", { x: bounds.left, y: bounds.top, width: bounds.right - bounds.left, height: bounds.bottom - bounds.top })); defs.append(clip); svg.append(defs);
    for (const marker of options.markers || []) if (finite(marker.x) && marker.x >= geometry.xmin && marker.x <= geometry.xmax) {
      const px = bounds.left + (marker.x - geometry.xmin) / (geometry.xmax - geometry.xmin) * (bounds.right - bounds.left);
      svg.append(svgNode("path", { d: `M${px} ${bounds.top}V${bounds.bottom}`, stroke: marker.level === "red" ? "#ffb3a9" : "#ffd88b", "stroke-width": 1, "stroke-dasharray": "3 5", opacity: 0.65 }));
    }
    svg.append(svgNode("path", { d: geometry.path, class: `lab-signal ${options.className || ""}`, "clip-path": `url(#${clipId})` }));
    if (xs.length === 1) svg.append(svgNode("circle", { cx: geometry.points[0].x, cy: geometry.points[0].y, r: 2.5, fill: "#80d8cf" }));
    host.replaceChildren(svg); host.setAttribute("aria-label", options.description || `${xs.length} 个真实数据点`);
    const entry = { xs, ys, options, geometry, svg, width }; plots.set(id, entry); return entry;
  }
  function inspectSample(index) {
    if (!waveform?.count || !mainPlot) return;
    index = Math.max(0, Math.min(waveform.count - 1, Math.round(index))); selectedSample = index;
    const point = mainPlot.geometry.points[index];
    mainPlot.svg.querySelectorAll(".lab-inspect-line,.lab-inspect-point").forEach((element) => element.remove());
    mainPlot.svg.append(svgNode("path", { d: `M${point.x} ${mainPlot.geometry.bounds.top}V${mainPlot.geometry.bounds.bottom}`, class: "lab-inspect-line" }), svgNode("circle", { cx: point.x, cy: point.y, r: 3.5, class: "lab-inspect-point" }));
    const readout = `seq ${waveform.seq[index]} · ${format(waveform.time_seconds[index])} s · ${format(waveform.vibration[index], 4)} g · ${format(waveform.temperature[index], 2)} °C`;
    text("lab-sample-readout", readout); $("lab-sample-inspector").value = String(index); $("lab-sample-inspector").setAttribute("aria-valuetext", readout);
  }
  function drawWaveform() {
    if (!waveform?.count) { clearWave("当前运行尚无已提交原始样本。"); waveformState = "waiting"; waveformMessage = "等待提交"; return; }
    const stats = waveformStatistics(waveform), spectrum = waveform.spectrum;
    mainPlot = drawChart("lab-vibration-chart", waveform.time_seconds, waveform.vibration, { includeZero: true, xUnit: " s", description: `真实振动波形，${waveform.count} 点，样本 ${waveform.start_seq} 至 ${waveform.end_seq_exclusive - 1}，片段 RMS ${format(stats.rms)} g。` });
    drawChart("lab-temperature-chart", waveform.time_seconds, waveform.temperature, { xUnit: " s", className: "lab-temperature-signal", description: `同一真实片段的温度趋势，${waveform.count} 点，单位摄氏度，温度不参与当前评分。` });
    if (spectrum?.frequency_hz.length) drawChart("lab-spectrum-chart", spectrum.frequency_hz, spectrum.amplitude_g, { includeZero: true, xUnit: " Hz", className: "lab-spectrum-signal", description: `真实振动片段的单边幅值谱，Hann 窗、去均值，${spectrum.sample_count} 个实际样本；频率单位 Hz，幅值单位 g，不是功率谱密度。` });
    else emptyChart("lab-spectrum-chart", "当前片段没有可用的频谱数据。");
    text("lab-rms", stats.rms === null ? "—" : `${format(stats.rms)} g`);
    text("lab-ptp", stats.peakToPeak === null ? "—" : `${format(stats.peakToPeak)} g`);
    text("lab-dominant", stats.peakFrequency === null ? "—" : `${format(stats.peakFrequency, 2)} Hz`);
    text("lab-sample-count", `${format(waveform.count, 0)} 点`);
    text("lab-wave-range", `seq ${format(waveform.start_seq, 0)}–${format(waveform.end_seq_exclusive - 1, 0)}`);
    text("lab-wave-provenance", `本次读取 ${format(waveform.count, 0)} 点 · 已提交 ${format(waveform.available_points, 0)} 点`);
    text("lab-spectrum-note", spectrum ? `seq ${format(spectrum.start_seq, 0)}–${format(spectrum.end_seq_exclusive - 1, 0)} · ${format(spectrum.sample_count, 0)} 点` : "暂无频谱结果");
    $("lab-sample-inspector").max = String(waveform.count - 1); $("lab-sample-inspector").disabled = false;
    if (selectedSample !== null) inspectSample(Math.min(selectedSample, waveform.count - 1));
  }

  function selectedDevice() { return live?.devices.find((device) => pairKey(device) === liveKey); }
  function liveFresh() {
    const now = Date.now(), heartbeat = Date.parse(live?.updated_at_utc);
    return live && !readError && now - liveReadAt <= STALE_MS && (live.status !== "running" || Number.isFinite(heartbeat) && now - heartbeat <= STALE_MS);
  }
  function liveState() {
    const device = selectedDevice();
    if (!live || !device) return { label: "等待设备", state: "waiting" };
    if (!liveFresh()) return { label: "状态已过期", state: "stale" };
    if (live.error || device.error || live.status === "error") return { label: "采集异常", state: "error" };
    if (live.status !== "running") return { label: live.status === "starting" ? "正在启动" : "会话已结束", state: "waiting" };
    if (device.capture_paused === true) return { label: "已暂停采集", state: "paused" };
    if (device.processed_samples > 0 && (!Number.isFinite(Date.parse(device.last_commit_at_utc)) || Date.now() - Date.parse(device.last_commit_at_utc) > STALE_MS)) return { label: "提交已过期", state: "stale" };
    return { label: "正在采集", state: "ready" };
  }
  function updateDevices() {
    const select = $("lab-device"), devices = live?.devices || [];
    const signature = devices.map(pairKey).join("|");
    if (select.dataset.signature !== signature) {
      select.replaceChildren(...devices.map((device) => { const option = node("option", device.device_id); option.value = pairKey(device); return option; }));
      select.dataset.signature = signature;
    }
    if (!devices.some((device) => pairKey(device) === liveKey)) { liveKey = devices.length ? pairKey(devices[0]) : ""; clearWave(); }
    select.value = liveKey; select.disabled = !devices.length;
    if (!devices.length) select.replaceChildren(node("option", "当前会话没有设备"));
  }
  function syncForm() {
    const profile = $("lab-profile").value, active = catalogue?.experiments?.some((experiment) => ACTIVE_STATES.has(experiment.status));
    const available = catalogue?.available === true && !creating;
    for (const id of ["lab-profile", "lab-seed", "lab-total-points", "lab-noise"]) $(id).disabled = !available;
    $("lab-degradation-field").hidden = profile !== "degradation"; $("lab-degradation").disabled = !available || profile !== "degradation";
    $("lab-fault-field").hidden = profile !== "sudden_fault"; $("lab-fault").disabled = !available || profile !== "sudden_fault";
    $("lab-injection-fields").hidden = profile !== "sudden_fault";
    const supported = catalogue?.limits?.fault_types || [];
    if ($( "lab-fault-type").dataset.options !== supported.join(",")) {
      const current = $("lab-fault-type").value;
      $("lab-fault-type").replaceChildren(...supported.filter((type) => Object.hasOwn(FAULT_TYPES, type)).map((type) => { const option = node("option", FAULT_TYPES[type]); option.value = type; return option; }));
      if (supported.includes(current)) $("lab-fault-type").value = current;
      $("lab-fault-type").dataset.options = supported.join(",");
    }
    for (const id of ["lab-fault-type", "lab-fault-start", "lab-fault-duration"]) $(id).disabled = !available || profile !== "sudden_fault" || !supported.length;
    $("lab-create").disabled = !available || active || catalogue.experiments.length >= (catalogue.limits?.max_experiments || 20);
    text("lab-create", creating ? "正在创建实验…" : "运行实验");
    text("lab-parameter-summary", `256 Hz · ${format(Number($("lab-total-points").value) / 256, 0)} 秒 · 每窗口 256 点`);
    try { text("lab-injection-summary", `计划区间：seq ${secondsToSamples($("lab-fault-start").value)} 起，持续 ${secondsToSamples($("lab-fault-duration").value)} 点`); } catch { text("lab-injection-summary", "时间需对应整数样本点；最小时间步长为 0.00390625 秒。"); }
    text("lab-capacity-note", !catalogue ? "正在读取实验服务状态…" : catalogue.available !== true ? "当前服务未启用独立实验。" : active ? "实验执行中" : `已保留 ${catalogue.experiments.length} / ${catalogue.limits?.max_experiments || 20} 次实验 · 同时执行 1 次`);
  }
  function fillDraft(parameters) {
    $("lab-profile").value = parameters.profile;
    const points = $("lab-total-points"), value = String(parameters.total_points);
    if (![...points.options].some((option) => option.value === value)) {
      const option = node("option", format(parameters.total_points, 0)); option.value = value; points.append(option);
    }
    for (const [id, field] of [["lab-seed", "seed"], ["lab-total-points", "total_points"], ["lab-noise", "noise_std"], ["lab-degradation", "degradation_rate_per_sample"], ["lab-fault", "fault_amplitude"]]) $(id).value = String(parameters[field]);
    syncForm();
    if (parameters.profile === "sudden_fault") {
      const plan = injectionDraft(parameters);
      $("lab-fault-type").value = plan.fault_type; $("lab-fault-start").value = String(plan.fault_start_seq / 256); $("lab-fault-duration").value = String(plan.fault_duration_points / 256); syncForm();
    }
  }
  function applyPreset(profile) {
    const preset = catalogue?.presets?.find((item) => item.profile === profile);
    if (preset) fillDraft(preset);
  }
  function renderHistory() {
    const experiments = catalogue?.experiments || [], present = new Set();
    for (const [index, experiment] of experiments.entries()) {
      present.add(experiment.id); let row = historyRows.get(experiment.id);
      if (!row) {
        const tr = node("tr"), nameCell = node("td"), title = node("strong", "", "lab-history-title"), id = node("span", "", "lab-history-id"); nameCell.append(title, id);
        const stateCell = node("td"), state = node("span", "", "lab-state"); stateCell.append(state);
        const params = node("td"), created = node("td"), elapsed = node("td"), actionCell = node("td"), button = node("button", "查看", "button button-secondary"); button.type = "button";
        button.addEventListener("click", () => selectExperiment(experiment.id)); actionCell.append(button); tr.append(nameCell, stateCell, params, created, elapsed, actionCell);
        row = { tr, title, id, state, params, created, elapsed, button }; historyRows.set(experiment.id, row);
      }
      const status = experimentStatus(experiment);
      row.title.textContent = PROFILES[experiment.parameters?.profile] || "未知场景"; row.id.textContent = experiment.run_id || experiment.id; row.id.title = experiment.run_id || experiment.id;
      row.state.textContent = status.label; row.state.dataset.state = status.state;
      row.params.textContent = `${format(experiment.parameters?.total_points, 0)} / ${format(experiment.parameters?.seed, 0)}`;
      row.created.textContent = timeText(experiment.created_at_utc); row.created.title = experiment.created_at_utc || "";
      row.elapsed.textContent = finite(experiment.elapsed_seconds) ? `${format(experiment.elapsed_seconds, 2)} s` : "—";
      row.tr.dataset.selected = String(experiment.id === selectedId); row.button.textContent = experiment.id === selectedId ? "正在查看" : "查看";
      row.button.setAttribute("aria-label", `查看${row.title.textContent}实验 ${experiment.id}`); row.button.setAttribute("aria-pressed", String(experiment.id === selectedId));
      const body = $("lab-history-rows");
      if (body.children[index] !== row.tr) body.insertBefore(row.tr, body.children[index] || null);
    }
    for (const [id, row] of historyRows) if (!present.has(id)) { row.tr.remove(); historyRows.delete(id); }
    $("lab-history-empty").hidden = experiments.length > 0;
    text("lab-history-caption", `${experiments.length} 次独立运行`);
  }
  function renderResults() {
    const result = detail?.results, summary = reportSummary(result?.summary), experiment = detail?.experiment;
    $("lab-parameter-record").hidden = !experiment;
    $("lab-load-parameters").disabled = !experiment?.parameters || creating || catalogue?.available !== true;
    const signature = JSON.stringify([selectedId, experiment?.updated_at_utc, experiment?.status, Boolean(result)]);
    if (detailSignature === signature) return; detailSignature = signature;
    text("lab-result-source", experiment ? `${PROFILES[experiment.parameters?.profile] || "实验"} · ${experiment.run_id || experiment.id}` : "暂无实验结果");
    text("lab-result-verdict", summary.verdict); $("lab-result-verdict").dataset.state = summary.state;
    $("lab-recorded-parameters").replaceChildren(...experimentParameterRows(experiment?.parameters, detail?.configuration).map(([label, value]) => { const pair = node("div"); pair.append(node("dt", label), node("dd", value)); return pair; }));
    $("lab-result-summary").replaceChildren(...summary.items.map(([label, value]) => { const pair = node("div"); pair.append(node("dt", label), node("dd", value)); return pair; }));
    const windows = result?.windows;
    if (Array.isArray(windows) && windows.length && windows.every((window) => integer(window.start_seq) && finite(window.health_score))) {
      const sorted = [...windows].sort((a, b) => a.start_seq - b.start_seq);
      const markers = (Array.isArray(result.alarms) ? result.alarms : []).map((alarm) => ({ x: alarm.start_seq / result.sample_rate_hz, level: alarm.level }));
      drawChart("lab-health-chart", sorted.map((window) => window.start_seq / result.sample_rate_hz), sorted.map((window) => window.health_score), { fixedY: [0, 100], className: "lab-health-signal", xUnit: " s", markers, description: `实验全程 ${windows.length} 个已存健康窗口，横轴为仿真相对秒，纵轴0至100；竖线对应实际历史告警。` });
    } else emptyChart("lab-health-chart", experiment?.status === "failed" ? "本次实验失败，未提供可用分析结果。" : "等待实际分析结果");
    const alarms = Array.isArray(result?.alarms) ? [...result.alarms].sort((a, b) => b.start_seq - a.start_seq).slice(0, 5) : [];
    $("lab-result-alarms").replaceChildren(...alarms.map((alarm) => { const item = node("li"), heading = node("div", undefined, "lab-alarm-heading"); item.dataset.level = alarm.level;
      heading.append(node("strong", alarm.level === "red" ? "红色历史告警" : alarm.level === "yellow" ? "黄色历史告警" : "告警级别未知", "lab-alarm-level"), node("span", `${format(alarm.start_seq / result.sample_rate_hz, 2)} s · seq ${format(alarm.start_seq, 0)}`));
      const reason = node("details"), caption = node("summary", "查看规则与原因"); reason.append(caption, node("p", `${alarm.rule_id || "未知规则"} · ${alarm.reason || "未提供原因"}`)); item.append(heading, reason); return item; }));
    if (!alarms.length) $("lab-result-alarms").append(node("li", result ? "当前结果没有告警事件。" : "等待告警数据", "lab-empty"));
    text("lab-alarm-note", result ? `最近 ${alarms.length} 条历史告警${result.alarms_truncated ? " · 非全量" : ""}` : "等待分析结果");
  }
  function render() {
    for (const key of ["live", "experiment"]) { const selected = mode === key; $(`lab-tab-${key}`).setAttribute("aria-selected", String(selected)); $(`lab-tab-${key}`).tabIndex = selected ? 0 : -1; $(`lab-${key}-controls`).hidden = !selected; }
    $("lab-history").hidden = mode !== "experiment"; $("lab-results").hidden = mode !== "experiment";
    $("lab-follow").hidden = mode !== "live"; $("lab-follow").setAttribute("aria-pressed", String(following)); text("lab-follow", following ? "暂停观察" : "继续观察");
    $("lab-refresh").disabled = reading || creating; $("lab-history-refresh").disabled = reading || creating;
    if (mode === "live") {
      const device = selectedDevice(), status = liveState();
      text("lab-device-state", status.label); text("lab-sample-rate", live ? `${format(live.sample_rate_hz, 0)} Hz` : "—"); text("lab-live-run", device?.run_id || "—");
      text("lab-wave-source", device ? `${device.device_id} · 已提交原始样本` : "等待数据来源");
      const connection = connectionPresentation({ mode, following, readError, liveStatus: status });
      text("lab-connection", connection.label); $("lab-connection-container").dataset.state = connection.state;
      const badge = !following && waveform ? { state: "paused", label: "观察已暂停" } : status.state === "stale" || status.state === "error" ? status : { state: waveformState, label: waveformMessage };
      text("lab-wave-state", badge.label); $("lab-wave-state").dataset.state = badge.state;
    } else {
      const experiment = detail?.experiment || catalogue?.experiments.find((item) => item.id === selectedId), status = experimentStatus(experiment);
      const connection = connectionPresentation({ mode, creating, readError, catalogueAvailable: catalogue?.available === true, experiment });
      text("lab-connection", connection.label); $("lab-connection-container").dataset.state = connection.state;
      text("lab-wave-source", experiment ? `${PROFILES[experiment.parameters?.profile] || "实验"} · ${experiment.run_id || experiment.id}` : "暂无实验片段");
      text("lab-wave-state", experiment?.status !== "completed" ? status.label : waveformMessage); $("lab-wave-state").dataset.state = experiment?.status !== "completed" ? status.state : waveformState;
      syncForm(); renderHistory(); renderResults();
    }
    const canBrowse = mode === "experiment" && detail?.experiment?.status === "completed";
    $("lab-position-controls").hidden = mode !== "experiment";
    const max = Math.max(0, (detail?.experiment?.parameters?.total_points || 0) - count);
    $("lab-position").max = String(max); $("lab-position").value = String(Math.min(position, max)); $("lab-position").disabled = !canBrowse || reading;
    text("lab-position-label", `从 seq ${format(position, 0)} 开始`);
    $("lab-previous-window").disabled = !canBrowse || reading || position <= 0; $("lab-next-window").disabled = !canBrowse || reading || position >= max;
  }

  function schedule() { clearTimeout(timer); if (!destroyed && !documentRef.hidden && (mode === "experiment" || following)) timer = setTimeout(() => void refresh(), POLL_MS); }
  function invalidate(messageText) { generation += 1; pollAbort?.abort(); clearTimeout(timer); reading = false; readError = false; waveformState = "waiting"; waveformMessage = "等待样本"; if (messageText) clearWave(messageText); }
  function selectExperiment(id) { if (id === selectedId && detail) return; selectedId = id; detail = null; detailSignature = ""; autoPosition = true; position = 0; invalidate("正在读取所选实验…"); render(); void refresh(true); }
  function selectMode(next) { if (mode === next) return; mode = next; invalidate(mode === "live" ? "正在读取设备片段…" : "暂无实验波形"); message("lab-error", ""); render(); void refresh(true); }
  async function refresh(force = false) {
    if (reading || creating || destroyed || documentRef.hidden || mode === "live" && !following && !force) return;
    clearTimeout(timer); const token = generation, observedMode = mode, controller = new AbortController(); pollAbort = controller; reading = true; render();
    const current = () => !destroyed && token === generation && observedMode === mode;
    try {
      if (observedMode === "live") {
        const result = await client.live(controller.signal); if (!current()) return;
        if (!Array.isArray(result.devices) || result.devices.some((device) => typeof device?.run_id !== "string" || typeof device.device_id !== "string")) throw invalid();
        const previousSession = live?.session_id; live = result; liveReadAt = Date.now(); readError = false;
        if (previousSession && previousSession !== result.session_id) { liveKey = ""; clearWave("仿真会话已切换，正在读取新片段。"); }
        updateDevices(); const device = selectedDevice();
        if (!device) { clearWave("当前持续仿真会话没有可选设备。"); waveformState = "waiting"; waveformMessage = "暂无设备"; }
        else {
          const value = normalizeWaveform(await client.waveform({ run_id: device.run_id, device_id: device.device_id, count: String(count) }, controller.signal));
          if (!current()) return;
          if (value.run_id !== device.run_id || value.device_id !== device.device_id) throw invalid();
          waveform = value; selectedSample = null; waveformState = value.count ? "ready" : "waiting"; waveformMessage = value.count ? "已提交片段" : "等待提交"; drawWaveform();
        }
      } else {
        const result = await client.list(controller.signal); if (!current()) return;
        if (!Array.isArray(result.experiments) || !Array.isArray(result.presets) || result.experiments.some((experiment) => typeof experiment?.id !== "string")) throw invalid();
        catalogue = result;
        if (!presetLoaded) { applyPreset($("lab-profile").value); presetLoaded = true; }
        if (!selectedId && result.experiments.length) { selectedId = result.experiments[0].id; autoPosition = true; }
        if (selectedId) {
          const value = await client.detail(selectedId, controller.signal); if (!current()) return;
          if (value.experiment?.id !== selectedId) throw invalid(); detail = value;
          if (value.experiment.status === "completed") {
            if (autoPosition) { position = initialExperimentStart(value, count); autoPosition = false; }
            const signal = normalizeWaveform(await client.experimentWaveform({ id: selectedId, count: String(count), start_seq: String(position) }, controller.signal));
            if (!current()) return;
            if (signal.run_id !== value.experiment.run_id || signal.device_id !== value.experiment.device_id) throw invalid();
            const changed = !waveform || waveform.run_id !== signal.run_id || waveform.start_seq !== signal.start_seq || waveform.count !== signal.count;
            waveform = signal; waveformState = signal.count ? "ready" : "waiting"; waveformMessage = signal.count ? "实验片段" : "暂无样本";
            if (changed) { selectedSample = null; drawWaveform(); }
          } else { clearWave(value.experiment.status === "failed" ? "本次实验失败，没有可用的完成波形。" : "实验执行中"); waveformState = value.experiment.status; waveformMessage = experimentStatus(value.experiment).label; }
        } else { detail = null; clearWave("暂无实验波形"); }
      }
      if (!current()) return;
      readError = false; text("lab-updated", `读取 ${timeText(Date.now())}`); message("lab-error", operationError);
    } catch (error) {
      if (!current() || controller.signal.aborted && documentRef.hidden) return;
      readError = true; waveformState = waveform ? "stale" : "error"; waveformMessage = waveform ? "保留上次片段" : "读取失败";
      message("lab-error", ERROR_TEXT[error.code] || (error.status === 404 ? "当前服务未提供此数据，请刷新或检查本机服务。" : "读取失败，已保留当前可用结果。请检查本机服务并刷新。"));
    } finally { if (current()) { reading = false; pollAbort = null; render(); schedule(); } }
  }
  async function createExperiment(event) {
    event.preventDefault(); if (creating || !catalogue?.available) return;
    let parameters;
    try { parameters = experimentParameters({ profile: $("lab-profile").value, seed: $("lab-seed").value, total_points: $("lab-total-points").value, noise_std: $("lab-noise").value, degradation_rate_per_sample: $("lab-degradation").value, fault_amplitude: $("lab-fault").value,
      ...($("lab-profile").value === "sudden_fault" && catalogue?.limits?.fault_types?.length ? { fault_type: $("lab-fault-type").value, fault_start_seq: secondsToSamples($("lab-fault-start").value), fault_duration_points: secondsToSamples($("lab-fault-duration").value) } : {}) }); }
    catch (error) { message("lab-error", error.message); return; }
    invalidate(); creating = true; operationError = ""; mutationAbort = new AbortController(); message("lab-error", ""); message("lab-feedback", "正在创建独立实验…"); render();
    try {
      const result = await client.create(parameters, mutationAbort.signal);
      if (destroyed) return;
      if (typeof result.experiment?.id !== "string") throw invalid();
      catalogue.experiments = [result.experiment, ...catalogue.experiments.filter((item) => item.id !== result.experiment.id)];
      if (mode === "experiment") { selectedId = result.experiment.id; detail = { experiment: result.experiment, results: null }; detailSignature = ""; autoPosition = true; position = 0; clearWave("实验执行中"); }
      message("lab-feedback", `${PROFILES[parameters.profile]}实验已创建`);
    } catch (error) {
      if (destroyed) return;
      operationError = ERROR_TEXT[error.code] || "创建结果尚未确认，请刷新运行记录核对后再操作，避免重复创建。";
      message("lab-feedback", ""); message("lab-error", operationError);
    } finally { creating = false; mutationAbort = null; if (!destroyed) { render(); schedule(); } }
  }
  function choosePosition(next) { const max = Math.max(0, (detail?.experiment?.parameters?.total_points || 0) - count); position = Math.min(max, Math.max(0, next)); autoPosition = false; invalidate("正在读取所选样本片段…"); render(); void refresh(true); }
  function pauseForInspection() { if (mode === "live" && following) { following = false; invalidate(); render(); } }
  $("lab-device").addEventListener("change", () => { liveKey = $("lab-device").value; invalidate("正在读取所选设备片段…"); render(); void refresh(true); });
  for (const button of documentRef.querySelectorAll(".lab-tabs [role=tab]")) {
    button.addEventListener("click", () => selectMode(button.dataset.mode));
    button.addEventListener("keydown", (event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? "live" : event.key === "End" ? "experiment" : mode === "live" ? "experiment" : "live"; selectMode(next); $(`lab-tab-${next}`).focus(); } });
  }
  $("lab-profile").addEventListener("change", () => applyPreset($("lab-profile").value));
  $("lab-load-parameters").addEventListener("click", () => {
    if (creating || catalogue?.available !== true) return;
    try {
      fillDraft(injectionDraft(experimentDraft(detail?.experiment), detail?.configuration));
      message("lab-feedback", `参数已载入 · ${detail.experiment.run_id || detail.experiment.id}`);
      $("lab-profile").focus();
    } catch (error) { message("lab-error", error.message); }
  });
  $("lab-total-points").addEventListener("change", syncForm);
  for (const id of ["lab-fault-type", "lab-fault-start", "lab-fault-duration"]) $(id).addEventListener("input", syncForm);
  $("lab-experiment-form").addEventListener("submit", (event) => void createExperiment(event));
  $("lab-count").addEventListener("change", () => { count = Number($("lab-count").value); position = Math.min(position, Math.max(0, (detail?.experiment?.parameters?.total_points || 0) - count)); invalidate("正在读取新的窗口范围…"); render(); void refresh(true); });
  $("lab-refresh").addEventListener("click", () => void refresh(true));
  $("lab-history-refresh").addEventListener("click", () => void refresh(true));
  $("lab-follow").addEventListener("click", () => { following = !following; invalidate(); render(); if (following) void refresh(true); });
  $("lab-position").addEventListener("input", () => text("lab-position-label", `从 seq ${format(Number($("lab-position").value), 0)} 开始`));
  $("lab-position").addEventListener("change", () => choosePosition(Number($("lab-position").value)));
  $("lab-previous-window").addEventListener("click", () => choosePosition(position - count));
  $("lab-next-window").addEventListener("click", () => choosePosition(position + count));
  $("lab-sample-inspector").addEventListener("input", () => { pauseForInspection(); inspectSample(Number($("lab-sample-inspector").value)); });
  function inspectPointer(event) { if (!waveform?.count || !mainPlot) return; const rect = $("lab-vibration-chart").getBoundingClientRect(); const px = (event.clientX - rect.left) / rect.width * mainPlot.width; const { left, right } = mainPlot.geometry.bounds; inspectSample((px - left) / (right - left) * (waveform.count - 1)); }
  $("lab-vibration-chart").addEventListener("pointermove", inspectPointer);
  $("lab-vibration-chart").addEventListener("click", (event) => { pauseForInspection(); inspectPointer(event); });
  const resize = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
    for (const [id, entry] of [...plots]) if (Math.abs($(id).clientWidth - entry.width) > 1) { const next = drawChart(id, entry.xs, entry.ys, entry.options); if (id === "lab-vibration-chart") { mainPlot = next; if (selectedSample !== null) inspectSample(selectedSample); } }
  }) : null;
  for (const id of ["lab-vibration-chart", "lab-temperature-chart", "lab-spectrum-chart", "lab-health-chart"]) resize?.observe($(id));
  const visibility = () => { clearTimeout(timer); if (documentRef.hidden) { generation += 1; pollAbort?.abort(); reading = false; } else void refresh(); };
  documentRef.addEventListener("visibilitychange", visibility);
  const destroy = () => { destroyed = true; generation += 1; clearTimeout(timer); pollAbort?.abort(); mutationAbort?.abort(); resize?.disconnect(); documentRef.removeEventListener("visibilitychange", visibility); };
  window.addEventListener("pagehide", destroy, { once: true });
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
  render(); void refresh(true);
  return { refresh, destroy };
}

if (typeof document !== "undefined" && document.getElementById("lab-vibration-chart")) startLaboratoryPage();
