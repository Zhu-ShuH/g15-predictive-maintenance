const ACTION_HEADER = { "Content-Type": "application/json", "X-G15-Console-Action": "notifications" };
const ERROR_TEXT = Object.freeze({
  invalid_request: "设置格式无效，请检查告警事件数、提醒间隔与 Token。",
  not_configured: "尚未配置 PushPlus Token，请先保存渠道设置。",
  not_found: "所选设备暂不可读取，请重新选择设备。",
  forbidden_origin: "操作来源未通过校验，请从本机控制台重新打开。",
  unavailable: "通知服务暂不可用，请稍后刷新。",
  notification_unavailable: "通知服务暂不可用，请稍后刷新。",
  notification_source_unavailable: "采集或告警来源暂不可用，请检查持续采集状态后刷新。",
  notification_snapshot_unavailable: "所选设备的告警与报告暂不可读取，请稍后重新预览。",
  notification_outbox_full: "待发送队列已满，请检查通知记录并稍后重试。",
  notification_content_too_large: "报告内容超过渠道限制，本条通知未发送。",
  provider_payload_too_large: "报告内容超过渠道限制，本条通知未发送。",
  provider_timeout: "渠道响应超时，接收结果暂不确定；请查看后续发送状态。",
  provider_network_error: "渠道连接失败，请检查网络并查看后续重试状态。",
  provider_http_error: "渠道服务暂不可用，请查看后续重试状态。",
  provider_rejected: "渠道拒绝接收，请检查 PushPlus Token 与渠道设置。",
  provider_invalid_response: "渠道未返回有效回执，接收结果暂不确定。",
  provider_receipt_invalid: "渠道回执无效，接收结果暂不确定。",
  provider_response_too_large: "渠道回执异常，接收结果暂不确定。",
  delivery_attempt_limit: "已达到尝试次数上限，请核对微信与通知记录。",
});
export const notificationErrorText = (code) => Object.hasOwn(ERROR_TEXT, code) ? ERROR_TEXT[code] : "通知操作失败，请刷新状态后重试。";
export function notificationSettings(values) {
  const numeric = (value) => typeof value === "number" || typeof value === "string" && value.trim() !== "";
  const threshold = Number(values.threshold), cooldown = Number(values.cooldown_seconds);
  if (typeof values.enabled !== "boolean" || !numeric(values.threshold) || !numeric(values.cooldown_seconds) ||
      !Number.isSafeInteger(threshold) || threshold < 1 || threshold > 1000000 ||
      !Number.isSafeInteger(cooldown) || cooldown < 0 || cooldown > 86400 ||
      (values.token !== undefined && (typeof values.token !== "string" || values.token.length > 8192))) {
    throw Object.assign(new Error("invalid_request"), { code: "invalid_request" });
  }
  const body = { enabled: values.enabled, threshold, cooldown_seconds: cooldown };
  if (values.token?.trim()) body.token = values.token.trim();
  return body;
}
export function notificationHistoryState(value) {
  return ({ accepted: "渠道已接收", provider_accepted: "渠道已接收", queued: "等待发送", retrying: "等待重试", retry_wait: "等待重试", retry: "等待重试",
    pending: "等待发送", failed: "发送失败", sending: "正在发送" })[value] || "状态未知";
}
function deviceIdentity(device) { return device ? JSON.stringify([device.run_id, device.device_id]) : ""; }
function selectedBody(device) {
  if (!device || typeof device.run_id !== "string" || !device.run_id ||
      typeof device.device_id !== "string" || !device.device_id) {
    throw Object.assign(new Error("invalid_request"), { code: "invalid_request" });
  }
  return { run_id: device.run_id, device_id: device.device_id };
}
export function createNotificationsClient({ fetchImpl = globalThis.fetch, timeoutMs = 10000 } = {}) {
  async function request(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetchImpl(path, { method: body === undefined ? "GET" : "POST",
        credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal,
        headers: body === undefined ? { Accept: "application/json" } : { ...ACTION_HEADER, Accept: "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      const result = await response.json();
      if (!response.ok) throw Object.assign(new Error("notification_request_failed"),
        { code: Object.hasOwn(ERROR_TEXT, result?.error) ? result.error : "request_failed" });
      if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("invalid_response");
      return result;
    } catch (error) {
      // Network/provider messages can contain private URLs or credentials.
      if (error?.code && Object.hasOwn(ERROR_TEXT, error.code)) throw error;
      throw Object.assign(new Error("notification_request_failed"), { code: "request_failed" });
    } finally { clearTimeout(timeout); }
  }
  return {
    settings: () => request("/api/notifications"),
    save: (values) => request("/api/notifications/settings", notificationSettings(values)),
    preview: (device) => request(`/api/notifications/preview?${new URLSearchParams(selectedBody(device))}`),
    test: (device) => request("/api/notifications/test", selectedBody(device)),
  };
}
export function createNotificationsController({ client = createNotificationsClient() } = {}) {
  let state = { settings: null, configured: false, status: null, history: [], loading: false,
    saving: false, testing: false, previewLoading: false, preview: null, selected: null, error: "", message: "" };
  let destroyed = false, previewRevision = 0, refreshRevision = 0;
  const listeners = new Set();
  function update(patch) {
    if (destroyed) return;
    state = { ...state, ...patch }; for (const listener of listeners) listener(state);
  }
  function apply(body) {
    if (!body?.settings || typeof body.configured !== "boolean") throw new Error("invalid_response");
    const settings = notificationSettings({ enabled: body.settings.enabled, threshold: body.settings.threshold,
      cooldown_seconds: body.settings.cooldown_seconds });
    update({ settings: { ...settings, channel: "pushplus" }, configured: body.configured,
      status: body.status || null, history: Array.isArray(body.history) ? body.history : [], error: "" });
  }
  async function refresh({ silent = false } = {}) {
    const revision = ++refreshRevision;
    if (!silent) update({ loading: true, error: "" });
    try { const body = await client.settings(); if (revision === refreshRevision && !destroyed) apply(body); }
    catch (error) { if (revision === refreshRevision) update({ error: notificationErrorText(error?.code) }); }
    finally { if (revision === refreshRevision) update({ loading: false }); }
  }
  return {
    getState: () => state,
    subscribe(listener) { listeners.add(listener); listener(state); return () => listeners.delete(listener); },
    select(device) {
      if (deviceIdentity(device) === deviceIdentity(state.selected)) return;
      previewRevision += 1;
      update({ selected: device ? selectedBody(device) : null, preview: null, previewLoading: false, message: "" });
    },
    refresh,
    async save(values) {
      if (state.saving || destroyed) return;
      update({ saving: true, error: "", message: "" });
      try {
        await client.save(notificationSettings(values));
        await refresh({ silent: true });
        update({ message: "通知设置已保存。" });
      } catch (error) { update({ error: notificationErrorText(error?.code) }); }
      finally { update({ saving: false }); }
    },
    async preview() {
      if (!state.selected || destroyed) return;
      const revision = ++previewRevision, device = { ...state.selected };
      update({ previewLoading: true, preview: null, error: "", message: "" });
      try {
        const body = await client.preview(device);
        if (revision !== previewRevision || destroyed) return;
        if (typeof body.title !== "string" || typeof body.markdown !== "string" ||
            !Number.isSafeInteger(body.red_count) || body.red_count < 0) throw new Error("invalid_response");
        update({ preview: { title: body.title, markdown: body.markdown, red_count: body.red_count,
          report_generated_at: body.report_generated_at, triggered: body.triggered === true } });
      } catch (error) { if (revision === previewRevision) update({ error: notificationErrorText(error?.code) }); }
      finally { if (revision === previewRevision) update({ previewLoading: false }); }
    },
    async test() {
      if (!state.configured || !state.selected || state.testing || destroyed) return;
      update({ testing: true, error: "", message: "" });
      try {
        await client.test({ ...state.selected });
        update({ message: "测试消息已排队。渠道接收状态请查看通知记录。" });
        await refresh({ silent: true });
      } catch (error) { update({ error: notificationErrorText(error?.code) }); }
      finally { update({ testing: false }); }
    },
    destroy() { destroyed = true; previewRevision += 1; refreshRevision += 1; listeners.clear(); },
  };
}
export function mountNotifications({ document: doc = globalThis.document, onSelect = () => {} } = {}) {
  const $ = (id) => doc.getElementById(id), controller = createNotificationsController();
  let dirty = false, visible = false, timer = null, selected = null, devices = [], deviceSignature = "";
  const timestamp = (value) => {
    const parsed = value ? new Date(typeof value === "number" ? value * 1000 : value) : null;
    return parsed && Number.isFinite(parsed.getTime()) ? new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).format(parsed) : "尚无记录";
  };
  function element(tag, content, className = "") {
    const item = doc.createElement(tag); item.textContent = String(content ?? ""); item.className = className; return item;
  }
  function render(state) {
    if (state.settings && !dirty) {
      $("notification-enabled").checked = state.settings.enabled;
      $("notification-threshold").value = state.settings.threshold;
      $("notification-cooldown").value = state.settings.cooldown_seconds;
    }
    const status = !state.settings ? "正在读取设置" : ({ paused: "通知已暂停", unconfigured: "渠道未配置",
      source_idle: "采集已停止，等待新数据", monitoring: "正在监测红色告警", error: "通知服务异常" })[state.status?.state] || "通知状态未知";
    $("notification-status").textContent = status;
    $("notification-status").dataset.level = state.status?.state === "monitoring" ? "healthy" : state.status?.state === "error" ? "error" : "unknown";
    $("notification-channel-status").textContent = state.configured ? "Token 已配置 · 内容不会回显" : "尚未配置 Token";
    $("notification-checked").textContent = `最近检查（北京时间）${timestamp(state.status?.last_checked_at)}`;
    const error = state.error || (state.status?.last_error_code ? notificationErrorText(state.status.last_error_code) : "");
    $("notification-error").textContent = error; $("notification-error").hidden = !error;
    $("notification-feedback").textContent = state.message; $("notification-feedback").hidden = !state.message;
    $("notification-save").disabled = !state.settings || state.saving || state.loading;
    $("notification-save").textContent = state.saving ? "正在保存…" : "保存通知设置";
    $("notification-refresh").disabled = state.loading;
    $("notification-refresh").textContent = state.loading ? "正在读取…" : "刷新通知状态";
    $("notification-preview-button").disabled = !selected || state.previewLoading || dirty || state.saving;
    $("notification-preview-button").textContent = state.previewLoading ? "正在生成预览…" : "预览建议与报告";
    $("notification-test").disabled = !state.configured || !selected || state.testing || state.saving || dirty;
    $("notification-test").textContent = state.testing ? "正在提交…" : "发送测试到微信";
    $("notification-test-scope").textContent = dirty ? "先保存更改，再预览或发送测试消息。" : state.configured
      ? "点击会向已配置渠道发送一条真实测试消息。" : "先保存 Token，才能发送真实测试消息。";
    $("notification-preview").hidden = !state.preview;
    $("notification-preview-empty").hidden = Boolean(state.preview);
    $("notification-preview-empty").textContent = state.previewLoading ? "正在从所选设备生成消息预览…" : "选择设备并生成预览，核对维护建议与报告内容。预览不会发送消息。";
    if (state.preview) {
      $("notification-preview-title").textContent = state.preview.title;
      $("notification-preview-meta").textContent = `${state.preview.red_count} 条红色告警事件 · ${state.preview.triggered ? "已达到提醒阈值" : "未达到提醒阈值"} · 报告生成（北京时间）${timestamp(state.preview.report_generated_at)}`;
      $("notification-preview-body").textContent = state.preview.markdown;
    }
    const history = state.history.map((row) => {
      const item = element("tr"), detail = element("td", row.device_id || "设备未知");
      detail.append(element("small", row.run_id || "运行未知", "notification-run"));
      const statusCell = element("td", notificationHistoryState(row.state)); statusCell.dataset.state = row.state;
      if (row.error_code) statusCell.append(element("small", notificationErrorText(row.error_code), "notification-history-error"));
      if (row.uncertain === true) statusCell.append(element("small", "渠道接收结果待确认", "notification-history-error"));
      item.append(detail, element("td", Number.isSafeInteger(row.red_count) ? row.red_count : "未知"),
        statusCell, element("td", Number.isSafeInteger(row.attempts) ? row.attempts : "未知"), element("td", timestamp(row.created_at)));
      return item;
    });
    $("notification-history").replaceChildren(...history);
    $("notification-history-empty").hidden = history.length > 0;
  }
  controller.subscribe(render);
  $("notification-form").addEventListener("input", () => { dirty = true; render(controller.getState()); });
  $("notification-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const tokenField = $("notification-token"), token = tokenField.value;
    tokenField.value = "";
    const values = { enabled: $("notification-enabled").checked, threshold: $("notification-threshold").value,
      cooldown_seconds: $("notification-cooldown").value, ...(token ? { token } : {}) };
    await controller.save(values);
    if (!controller.getState().error) { dirty = false; render(controller.getState()); }
  });
  $("notification-device").addEventListener("change", (event) => {
    const device = devices.find((item) => deviceIdentity(item) === event.target.value);
    if (device) onSelect(device);
  });
  $("notification-preview-button").addEventListener("click", () => { void controller.preview(); });
  $("notification-test").addEventListener("click", () => { void controller.test(); });
  $("notification-refresh").addEventListener("click", () => { void controller.refresh(); });
  function schedule() {
    clearTimeout(timer); timer = null;
    if (visible && !doc.hidden) timer = setTimeout(async () => { await controller.refresh({ silent: true }); schedule(); }, 10000);
  }
  function visibility() { schedule(); }
  doc.addEventListener("visibilitychange", visibility);
  return {
    sync(consoleState) {
      selected = consoleState.selected; devices = consoleState.devices || [];
      const signature = JSON.stringify(devices.map(deviceIdentity));
      if (signature !== deviceSignature) {
        deviceSignature = signature;
        $("notification-device").replaceChildren(...devices.map((device) => {
          const option = element("option", `${device.device_id} · ${device.run_id}`); option.value = deviceIdentity(device); return option;
        }));
      }
      $("notification-device").disabled = devices.length === 0;
      $("notification-device").value = deviceIdentity(selected);
      controller.select(selected);
      const active = consoleState.view === "notifications";
      if (active !== visible) { visible = active; if (visible) void controller.refresh(); schedule(); }
      render(controller.getState());
    },
    refresh: () => controller.refresh(),
    destroy() { clearTimeout(timer); doc.removeEventListener("visibilitychange", visibility); controller.destroy(); },
  };
}
