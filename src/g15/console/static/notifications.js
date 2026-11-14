const ACTION_HEADER = { "Content-Type": "application/json", "X-G15-Console-Action": "notifications" };
const ERROR_TEXT = Object.freeze({
  invalid_request: "设置格式无效，请检查告警事件数、提醒间隔与 Token。",
  not_configured: "尚未配置 PushPlus Token，请先保存渠道设置。",
  notification_busy: "有通知正在发送或等待确认，请先核对记录并稍后重试。",
  notification_enabled: "请先关闭并保存自动提醒，再取消待发邮件。",
  notification_cancelled: "该通知已取消，未尝试发送；记录已保留。",
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
  delivery_attempt_limit: "已达到尝试次数上限，请核对接收渠道与通知记录。",
  smtp_auth_failed: "163 邮箱认证失败，请检查 SMTP 服务开关并重新填写客户端授权码。",
  smtp_sender_rejected: "163 拒绝了发件邮箱，请核对发件地址与授权码是否属于同一账号。",
  smtp_recipient_rejected: "收件邮箱被拒绝，请核对地址后重新保存。",
  smtp_tls_error: "邮件安全连接失败，请检查网络后查看通知记录。",
  smtp_timeout: "邮件服务响应超时，接收结果待确认；请先核对邮箱与通知记录，避免重复发送。",
  smtp_network_error: "邮件连接失败，请检查网络并查看后续重试状态。",
  smtp_protocol_error: "邮件服务返回异常，请查看通知记录后再处理。",
  smtp_sender_credentials_missing: "该发件邮箱的授权码不可用，请重新填写对应邮箱的授权码。",
  smtp_payload_invalid: "邮件内容暂不可发送，请重新预览报告并查看通知记录。",
  smtp_payload_too_large: "报告超过邮件大小限制，本封邮件未发送。",
  smtp_data_rejected: "邮件服务拒绝接收正文，请查看通知记录并核对邮箱设置。",
  provider_request_in_progress: "上一条消息仍在确认接收结果，请先等待通知记录更新。",
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
export function emailNotificationSettings(values) {
  const policy = notificationSettings({ enabled: values.enabled, threshold: values.threshold,
    cooldown_seconds: values.cooldown_seconds });
  const email = (value) => typeof value === "string" && value.length <= 254 &&
    /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$/.test(value);
  const sender = typeof values.sender === "string" ? values.sender.trim() : "";
  const recipient = typeof values.recipient === "string" ? values.recipient.trim() : "";
  if (!email(sender) || !/@163\.com$/i.test(sender) || !email(recipient) ||
      (values.channel !== undefined && values.channel !== "smtp163") ||
      (values.auth_code !== undefined && (typeof values.auth_code !== "string" ||
        values.auth_code.length > 8192 || /[\r\n\0]/.test(values.auth_code)))) {
    throw Object.assign(new Error("invalid_request"), { code: "invalid_request" });
  }
  return { ...policy, channel: "smtp163", sender, recipient,
    ...(values.auth_code === undefined ? {} : { auth_code: values.auth_code.trim() }) };
}
export function emailNotificationErrorText(code) {
  return ({ invalid_request: "请检查发件邮箱、收件邮箱、授权码和提醒规则。发件邮箱须为 163 邮箱。",
    not_configured: "尚未配置邮件渠道，请填写发件邮箱、收件邮箱与客户端授权码后保存。",
    provider_rejected: "邮件服务拒绝接收，请检查邮箱设置与通知记录。",
    request_failed: "请求结果暂未确认，请先核对邮箱与通知记录，避免重复发送。" })[code] || notificationErrorText(code);
}
export function notificationTestPending(state) {
  if (state.testing || state.testUncertain) return true;
  return state.history.some((row) => row.kind === "test" && row.run_id === state.selected?.run_id &&
    row.device_id === state.selected?.device_id &&
    (row.uncertain === true || ["queued", "retry_wait", "sending"].includes(row.state)));
}
export function notificationHistoryState(value, channel = "pushplus", errorCode = null) {
  if (value === "failed" && errorCode === "notification_cancelled") return "已取消 · 未发送";
  if (channel === "smtp163" && ["accepted", "provider_accepted"].includes(value)) return "SMTP 已接收";
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
export function createNotificationsClient({ fetchImpl = globalThis.fetch, timeoutMs = 10000, channel = "pushplus" } = {}) {
  const base = channel === "smtp163" ? "/api/email-notifications" : "/api/notifications";
  const normalize = channel === "smtp163" ? emailNotificationSettings : notificationSettings;
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
    settings: () => request(base),
    save: (values) => request(`${base}/settings`, normalize(values)),
    preview: (device) => request(`${base}/preview?${new URLSearchParams(selectedBody(device))}`),
    test: (device) => request(`${base}/test`, selectedBody(device)),
    deleteSettings: () => request(`${base}/settings/delete`, {}),
    ...(channel === "smtp163" ? { cancelPending: () => request(`${base}/pending/cancel`, {}) } : {}),
  };
}
export function createEmailNotificationsClient(options = {}) {
  return createNotificationsClient({ ...options, channel: "smtp163" });
}
export function createNotificationsController({ client, channel = "pushplus" } = {}) {
  client ||= createNotificationsClient({ channel });
  const email = channel === "smtp163";
  const normalize = email ? emailNotificationSettings : notificationSettings;
  const errorText = email ? emailNotificationErrorText : notificationErrorText;
  let state = { settings: null, configured: false, status: null, history: [], loading: false,
    saving: false, testing: false, deleting: false, cancelling: false, testUncertain: false, previewLoading: false, preview: null, selected: null, error: "", message: "" };
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
    if (email && (typeof body.settings.sender !== "string" || typeof body.settings.recipient !== "string")) throw new Error("invalid_response");
    update({ settings: { ...settings, channel, ...(email ? { sender: body.settings.sender, recipient: body.settings.recipient } : {}) }, configured: body.configured,
      status: body.status || null, history: Array.isArray(body.history) ? body.history : [], error: "" });
  }
  const mutationBusy = () => state.saving || state.testing || state.deleting || state.cancelling;
  async function refresh({ silent = false } = {}) {
    if (state.deleting || state.cancelling || destroyed) return;
    const revision = ++refreshRevision;
    if (!silent) update({ loading: true, error: "" });
    try { const body = await client.settings(); if (revision === refreshRevision && !destroyed) apply(body); }
    catch (error) { if (revision === refreshRevision) update({ error: errorText(error?.code) }); }
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
      if (mutationBusy() || destroyed) return;
      update({ saving: true, error: "", message: "" });
      try {
        await client.save(normalize(values));
        await refresh({ silent: true });
        update({ message: "通知设置已保存。" });
      } catch (error) { update({ error: errorText(error?.code) }); }
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
        let attachments = [];
        if (email) {
          if (!Array.isArray(body.attachments) || body.attachments.length !== 2 ||
              !["summary.md", "summary.json"].every((name) => body.attachments.some((item) => item.filename === name)) ||
              body.attachments.some((item) => typeof item.content_type !== "string" || !Number.isSafeInteger(item.bytes) || item.bytes < 0)) {
            throw new Error("invalid_response");
          }
          attachments = body.attachments.map((item) => ({ filename: item.filename, content_type: item.content_type, bytes: item.bytes }));
        }
        update({ preview: { title: body.title, markdown: body.markdown, red_count: body.red_count,
          report_generated_at: body.report_generated_at, triggered: body.triggered === true, attachments } });
      } catch (error) { if (revision === previewRevision) update({ error: errorText(error?.code) }); }
      finally { if (revision === previewRevision) update({ previewLoading: false }); }
    },
    async test() {
      if (!state.configured || !state.selected || notificationTestPending(state) || mutationBusy() || destroyed) return;
      update({ testing: true, error: "", message: "" });
      try {
        await client.test({ ...state.selected });
        update({ message: email ? "测试邮件已排队。请查看通知记录，并在手机邮箱确认正文与两份报告附件。" : "测试消息已排队。渠道接收状态请查看通知记录。" });
        await refresh({ silent: true });
      } catch (error) { update({ error: errorText(error?.code), testUncertain: error?.code === "request_failed" }); }
      finally { update({ testing: false }); }
    },
    async deleteSettings() {
      if (mutationBusy() || destroyed) return false;
      refreshRevision += 1; previewRevision += 1;
      update({ deleting: true, previewLoading: false, error: "", message: "" });
      try {
        const body = await client.deleteSettings();
        if (body.deleted !== true || !Number.isSafeInteger(body.cancelled_count) || body.cancelled_count < 0) throw new Error("invalid_response");
        apply(body);
        update({ preview: null, testUncertain: false, message: `本渠道通知设置已删除，自动提醒已关闭；已取消 ${body.cancelled_count} 条未发送任务，历史记录保留。` });
        return true;
      } catch (error) { update({ error: errorText(error?.code) }); return false; }
      finally { update({ deleting: false }); }
    },
    async cancelPending() {
      if (!email || mutationBusy() || destroyed) return false;
      if (state.settings?.enabled !== false) { update({ error: errorText("notification_enabled") }); return false; }
      refreshRevision += 1;
      update({ cancelling: true, error: "", message: "" });
      try {
        const body = await client.cancelPending();
        if (!Number.isSafeInteger(body.cancelled_count) || body.cancelled_count < 0) throw new Error("invalid_response");
        apply(body);
        update({ message: `已取消 ${body.cancelled_count} 封未发送的自动邮件；邮箱配置和历史记录保留，自动提醒仍关闭。` });
        return true;
      } catch (error) { update({ error: errorText(error?.code) }); return false; }
      finally { update({ cancelling: false }); }
    },
    destroy() { destroyed = true; previewRevision += 1; refreshRevision += 1; listeners.clear(); },
  };
}
export function createEmailNotificationsController(options = {}) {
  return createNotificationsController({ ...options, channel: "smtp163" });
}
export function mountNotifications({ document: doc = globalThis.document, onSelect = () => {}, clients = {} } = {}) {
  const $ = (id) => doc.getElementById(id);
  const controllers = {
    pushplus: createNotificationsController({ client: clients.pushplus }),
    smtp163: createEmailNotificationsController({ client: clients.smtp163 }),
  };
  let channel = "smtp163", visible = false, timer = null, selected = null, devices = [], deviceSignature = "", confirmation = null;
  const dirty = { pushplus: false, smtp163: false }, drafts = {}, revisions = { pushplus: 0, smtp163: 0 };
  const current = () => controllers[channel];
  const timestamp = (value) => {
    const parsed = value ? new Date(typeof value === "number" ? value * 1000 : value) : null;
    return parsed && Number.isFinite(parsed.getTime()) ? new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).format(parsed) : "尚无记录";
  };
  function element(tag, content, className = "") {
    const item = doc.createElement(tag); item.textContent = String(content ?? ""); item.className = className; return item;
  }
  function fill(values) {
    $("notification-enabled").checked = values.enabled;
    $("notification-threshold").value = values.threshold;
    $("notification-cooldown").value = values.cooldown_seconds;
    if (channel === "smtp163") {
      $("notification-email-sender").value = values.sender || "";
      $("notification-email-recipient").value = values.recipient || "";
    }
  }
  function publicFields() {
    return { enabled: $("notification-enabled").checked, threshold: $("notification-threshold").value,
      cooldown_seconds: $("notification-cooldown").value, ...(channel === "smtp163" ? {
        sender: $("notification-email-sender").value, recipient: $("notification-email-recipient").value } : {}) };
  }
  function render(state) {
    const email = channel === "smtp163", pending = notificationTestPending(state);
    const destructive = state.deleting || state.cancelling, busy = state.saving || state.testing || destructive;
    if (state.settings && !dirty[channel]) fill(state.settings);
    $("notification-title").textContent = email ? "红色告警 · 邮件提醒" : "红色告警 · 微信提醒";
    $("notification-channel-choice").value = channel;
    $("notification-channel-choice").disabled = busy;
    $("notification-channel-name").textContent = email ? "163 邮箱" : "PushPlus · 微信";
    $("notification-pushplus-fields").hidden = email;
    $("notification-email-fields").hidden = !email;
    $("notification-token").disabled = email || destructive;
    for (const id of ["notification-email-sender", "notification-email-recipient", "notification-email-auth"]) $(id).disabled = !email || destructive;
    for (const id of ["notification-enabled", "notification-threshold", "notification-cooldown"]) $(id).disabled = destructive;
    $("notification-email-sender").required = email;
    $("notification-email-recipient").required = email;
    const status = !state.settings ? "正在读取设置" : ({ paused: "通知已暂停", unconfigured: "渠道未配置",
      source_idle: "采集已停止，等待新数据", monitoring: "正在监测红色告警", error: "通知服务异常" })[state.status?.state] || "通知状态未知";
    $("notification-status").textContent = status;
    $("notification-status").dataset.level = state.status?.state === "monitoring" ? "healthy" : state.status?.state === "error" ? "error" : "unknown";
    $("notification-channel-status").textContent = email
      ? state.configured ? "授权码已配置" : "尚未配置邮件授权码"
      : state.configured ? "Token 已配置" : "尚未配置 Token";
    $("notification-checked").textContent = `最近检查（北京时间）${timestamp(state.status?.last_checked_at)}`;
    const errorText = email ? emailNotificationErrorText : notificationErrorText;
    const error = state.error || (state.status?.last_error_code ? errorText(state.status.last_error_code) : "");
    $("notification-error").textContent = error; $("notification-error").hidden = !error;
    $("notification-feedback").textContent = state.message; $("notification-feedback").hidden = !state.message;
    $("notification-save").disabled = !state.settings || busy || state.loading;
    $("notification-save").textContent = state.saving ? "正在保存…" : "保存通知设置";
    $("notification-refresh").disabled = state.loading || destructive;
    $("notification-refresh").textContent = state.loading ? "正在读取…" : "刷新通知状态";
    $("notification-preview-button").disabled = !selected || state.previewLoading || dirty[channel] || busy;
    $("notification-preview-button").textContent = state.previewLoading ? "正在生成预览…" : "预览建议与报告";
    $("notification-test").disabled = !state.configured || !selected || pending || busy || dirty[channel];
    $("notification-test").textContent = state.testing ? "正在提交…" : email ? "发送测试邮件" : "发送测试到微信";
    $("notification-delete-settings").disabled = !state.settings || busy || state.loading;
    $("notification-delete-settings").ariaExpanded = String(confirmation?.action === "delete");
    $("notification-cancel-pending").hidden = !email;
    $("notification-cancel-pending").disabled = !state.settings || state.settings.enabled || busy || state.loading;
    $("notification-cancel-pending").ariaExpanded = String(confirmation?.action === "cancel");
    $("notification-confirmation").hidden = !confirmation;
    if (confirmation) {
      const deleting = confirmation.action === "delete";
      $("notification-confirm-title").textContent = deleting ? `删除${email ? "邮箱" : "微信"}通知设置？` : "取消待发自动邮件？";
      $("notification-confirm-description").textContent = deleting
        ? `将${email ? "清除发件邮箱、收件邮箱和授权码" : "清除 Token"}，关闭自动提醒并恢复默认规则，取消本渠道尚未尝试发送的任务。${dirty[channel] ? "本渠道未保存的更改也会丢弃。" : ""}已发送记录和其他渠道不会删除。`
        : "将取消所有尚未尝试发送的自动邮件，保留邮箱配置、历史记录与未保存的更改，自动提醒继续关闭。";
      $("notification-confirm-action").textContent = destructive ? "正在处理…" : deleting ? "确认删除设置" : "确认取消待发";
      $("notification-confirm-action").disabled = busy || state.loading;
      $("notification-confirm-dismiss").disabled = destructive;
    }
    $("notification-preview").hidden = !state.preview;
    $("notification-preview-empty").hidden = Boolean(state.preview);
    $("notification-preview-empty").textContent = state.previewLoading ? "正在生成消息预览…" : "暂无预览";
    $("notification-attachments").hidden = !email || !state.preview;
    if (state.preview) {
      $("notification-preview-title").textContent = state.preview.title;
      $("notification-preview-meta").textContent = `${state.preview.red_count} 条红色告警事件 · ${state.preview.triggered ? "已达到提醒阈值" : "未达到提醒阈值"} · 报告生成（北京时间）${timestamp(state.preview.report_generated_at)}`;
      $("notification-preview-body").textContent = state.preview.markdown;
      if (email) $("notification-attachments").textContent = `完整报告附件：${state.preview.attachments.map((item) => item.filename).join(" · ")}`;
    }
    const history = state.history.map((row) => {
      const item = element("tr"), detail = element("td", row.device_id || "设备未知");
      detail.append(element("small", row.run_id || "运行未知", "notification-run"));
      const statusCell = element("td", notificationHistoryState(row.state, channel, row.error_code));
      statusCell.dataset.state = row.error_code === "notification_cancelled" ? "cancelled" : row.state;
      if (row.error_code) statusCell.append(element("small", errorText(row.error_code), "notification-history-error"));
      if (row.uncertain === true) statusCell.append(element("small", "接收结果待确认", "notification-history-error"));
      item.append(detail, element("td", Number.isSafeInteger(row.red_count) ? row.red_count : "未知"),
        statusCell, element("td", Number.isSafeInteger(row.attempts) ? row.attempts : "未知"), element("td", timestamp(row.created_at)));
      return item;
    });
    $("notification-history").replaceChildren(...history);
    $("notification-history-empty").hidden = history.length > 0;
  }
  for (const [key, controller] of Object.entries(controllers)) controller.subscribe((state) => { if (channel === key) render(state); });
  $("notification-channel-choice").addEventListener("change", () => {
    const next = $("notification-channel-choice").value;
    const state = current().getState();
    if (!Object.hasOwn(controllers, next) || state.saving || state.testing || state.deleting || state.cancelling) {
      $("notification-channel-choice").value = channel; return;
    }
    if (dirty[channel]) drafts[channel] = publicFields();
    confirmation = null; channel = next;
    fill(drafts[channel] || current().getState().settings || { enabled: false, threshold: 1, cooldown_seconds: 600 });
    render(current().getState());
    if (visible) void current().refresh();
  });
  $("notification-form").addEventListener("input", () => { confirmation = null; dirty[channel] = true; revisions[channel] += 1; render(current().getState()); });
  $("notification-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const state = current().getState();
    if (state.deleting || state.cancelling || state.testing || state.saving) return;
    confirmation = null;
    const key = channel, controller = current(), revision = revisions[key], values = publicFields();
    const secretField = $(key === "smtp163" ? "notification-email-auth" : "notification-token");
    const secret = secretField.value; secretField.value = "";
    if (secret) values[key === "smtp163" ? "auth_code" : "token"] = secret;
    await controller.save(values);
    if (!controller.getState().error && revision === revisions[key]) { dirty[key] = false; delete drafts[key]; }
    if (channel === key) render(controller.getState());
  });
  function requestConfirmation(action) {
    const state = current().getState();
    if (!state.settings || state.saving || state.testing || state.deleting || state.cancelling || state.loading) return;
    if (action === "cancel" && (channel !== "smtp163" || state.settings.enabled)) return;
    confirmation = { action, channel }; render(state);
    $("notification-confirm-dismiss").focus?.();
  }
  $("notification-delete-settings").addEventListener("click", () => requestConfirmation("delete"));
  $("notification-cancel-pending").addEventListener("click", () => requestConfirmation("cancel"));
  $("notification-confirm-dismiss").addEventListener("click", () => { confirmation = null; render(current().getState()); });
  $("notification-confirm-action").addEventListener("click", async () => {
    if (!confirmation || confirmation.channel !== channel) return;
    const key = channel, action = confirmation.action, controller = current();
    const success = await (action === "delete" ? controller.deleteSettings() : controller.cancelPending());
    if (success) {
      confirmation = null;
      if (action === "delete") {
        dirty[key] = false; delete drafts[key]; revisions[key] += 1;
        $(key === "smtp163" ? "notification-email-auth" : "notification-token").value = "";
      }
    }
    if (channel === key) render(controller.getState());
    if (success) $(action === "delete" ? "notification-delete-settings" : "notification-cancel-pending").focus?.();
  });
  $("notification-device").addEventListener("change", (event) => {
    const device = devices.find((item) => deviceIdentity(item) === event.target.value);
    if (device) onSelect(device);
  });
  $("notification-preview-button").addEventListener("click", () => { void current().preview(); });
  $("notification-test").addEventListener("click", () => { void current().test(); });
  $("notification-refresh").addEventListener("click", () => { void current().refresh(); });
  function schedule() {
    clearTimeout(timer); timer = null;
    if (visible && !doc.hidden) timer = setTimeout(async () => { await current().refresh({ silent: true }); schedule(); }, 10000);
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
      for (const controller of Object.values(controllers)) controller.select(selected);
      const active = consoleState.view === "notifications";
      if (active !== visible) { visible = active; if (visible) void current().refresh(); schedule(); }
      render(current().getState());
    },
    refresh: () => current().refresh(),
    destroy() { clearTimeout(timer); doc.removeEventListener("visibilitychange", visibility); for (const controller of Object.values(controllers)) controller.destroy(); },
  };
}
