import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createNotificationsClient, createNotificationsController, notificationSettings,
  notificationHistoryState, notificationErrorText, emailNotificationSettings, createEmailNotificationsClient,
  createEmailNotificationsController, emailNotificationErrorText, notificationTestPending, mountNotifications } from "../../src/g15/console/static/notifications.js";

const A = { run_id: "run/a", device_id: "device a" };
const B = { run_id: "run-b", device_id: "device-b" };
const settings = { enabled: false, threshold: 1, cooldown_seconds: 600, channel: "pushplus" };
const status = (extra = {}) => ({ settings, configured: true, status: { state: "paused" }, history: [], ...extra });
const json = (body, statusCode = 200) => new Response(JSON.stringify(body), { status: statusCode });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

test("only explicit settings and test actions POST with the same-origin action header", async () => {
  const calls = [];
  const client = createNotificationsClient({ fetchImpl: async (url, options) => {
    calls.push({ url, ...options }); return json(status());
  } });
  await client.settings();
  await client.preview(A);
  assert.equal(calls.every((call) => call.method === "GET"), true);
  const previewURL = new URL(calls[1].url, "http://localhost");
  assert.equal(previewURL.searchParams.get("run_id"), A.run_id);
  assert.equal(previewURL.searchParams.get("device_id"), A.device_id);
  await client.save({ ...settings, token: " local-only-token " });
  await client.test(A);
  for (const call of calls.slice(2)) {
    assert.equal(call.method, "POST");
    assert.equal(call.headers["X-G15-Console-Action"], "notifications");
    assert.equal(call.headers["Content-Type"], "application/json");
    assert.equal(call.credentials, "same-origin");
    assert.equal(call.redirect, "error");
    assert.equal(call.url.includes("local-only-token"), false);
  }
  assert.deepEqual(JSON.parse(calls[2].body), { enabled: false, threshold: 1, cooldown_seconds: 600, token: "local-only-token" });
  assert.deepEqual(JSON.parse(calls[3].body), A);
});

test("invalid threshold, cooldown and device identity never reach fetch", async () => {
  let calls = 0;
  const client = createNotificationsClient({ fetchImpl: async () => { calls += 1; return json({}); } });
  for (const patch of [{ threshold: 0 }, { threshold: 1.5 }, { threshold: true }, { threshold: "" },
    { cooldown_seconds: -1 }, { cooldown_seconds: null }, { cooldown_seconds: "" }, { cooldown_seconds: 86401 }]) {
    assert.throws(() => client.save({ ...settings, ...patch }));
  }
  assert.throws(() => client.test({ run_id: A.run_id }));
  assert.throws(() => client.preview(null));
  assert.equal(calls, 0);
  assert.equal(Object.hasOwn(notificationSettings(settings), "token"), false);
});

test("provider and network error details never become user-visible messages", async () => {
  const secret = "private-token-in-upstream-error";
  const badResponse = createNotificationsClient({ fetchImpl: async () => json({ error: secret, detail: secret }, 502) });
  const network = createNotificationsClient({ fetchImpl: async () => { throw new Error(`https://provider.invalid/${secret}`); } });
  for (const client of [badResponse, network]) {
    await assert.rejects(client.settings(), (error) => {
      assert.equal(error.message.includes(secret), false);
      assert.equal(notificationErrorText(error.code).includes(secret), false);
      return true;
    });
  }
});

test("unconfigured channels cannot queue a test, and preview never queues a send", async () => {
  let sends = 0, previews = 0;
  const controller = createNotificationsController({ client: {
    settings: async () => status({ configured: false }),
    preview: async () => { previews += 1; return { title: "预览", markdown: "真实报告", red_count: 0 }; },
    test: async () => { sends += 1; },
  } });
  controller.select(A);
  await controller.refresh(); await controller.test(); await controller.preview();
  assert.equal(sends, 0); assert.equal(previews, 1);
  assert.equal(controller.getState().preview.markdown, "真实报告");
});

test("a late preview from a previous device cannot replace the current device", async () => {
  const pending = deferred();
  const controller = createNotificationsController({ client: { preview: () => pending.promise } });
  controller.select(A);
  const operation = controller.preview();
  controller.select(B);
  pending.resolve({ title: "wrong device", markdown: "old report", red_count: 7 });
  await operation;
  assert.deepEqual(controller.getState().selected, B);
  assert.equal(controller.getState().preview, null);
  assert.equal(controller.getState().previewLoading, false);
});

test("tokens are submitted but never retained in observable controller state", async () => {
  const token = "never-retain-this-token";
  let saved;
  const controller = createNotificationsController({ client: {
    save: async (body) => { saved = body; },
    settings: async () => status({ settings: { ...settings, token } }),
  } });
  const snapshots = [];
  controller.subscribe((value) => snapshots.push(JSON.stringify(value)));
  await controller.save({ ...settings, token });
  assert.equal(saved.token, token);
  assert.equal(snapshots.some((value) => value.includes(token)), false);
});

test("duplicate test clicks while queueing cannot create duplicate requests", async () => {
  const pending = deferred(); let sends = 0;
  const controller = createNotificationsController({ client: {
    settings: async () => status(), test: async () => { sends += 1; await pending.promise; return { queued: true }; },
  } });
  controller.select(A); await controller.refresh();
  const first = controller.test(); await controller.test();
  assert.equal(sends, 1);
  pending.resolve(); await first;
  assert.equal(controller.getState().testing, false);
  assert.equal(notificationHistoryState("accepted"), "渠道已接收");
  assert.equal(notificationHistoryState("provider_accepted"), "渠道已接收");
  assert.equal(notificationHistoryState("retrying"), "等待重试");
  assert.equal(notificationHistoryState("retry_wait"), "等待重试");
  assert.equal(notificationHistoryState("unknown"), "状态未知");
});


const mailSettings = { enabled: false, threshold: 1, cooldown_seconds: 600, channel: "smtp163",
  sender: "demo-sender@163.com", recipient: "demo-recipient@example.com" };
const mailStatus = (extra = {}) => ({ settings: mailSettings, configured: true, status: { state: "paused" }, history: [], ...extra });
const attachments = [
  { filename: "summary.md", content_type: "text/markdown; charset=utf-8", bytes: 120 },
  { filename: "summary.json", content_type: "application/json; charset=utf-8", bytes: 230 },
];

test("email settings and explicit tests use a separate API without credentials in URLs", async () => {
  const calls = [];
  const client = createEmailNotificationsClient({ fetchImpl: async (url, options) => {
    calls.push({ url, ...options }); return json(mailStatus());
  } });
  await client.settings(); await client.preview(A);
  await client.save({ ...mailSettings, auth_code: " synthetic-client-auth-code " });
  await client.test(A);
  assert.equal(calls[0].url, "/api/email-notifications");
  assert.equal(calls[1].url.startsWith("/api/email-notifications/preview?"), true);
  assert.equal(calls[2].url, "/api/email-notifications/settings");
  assert.equal(calls[3].url, "/api/email-notifications/test");
  assert.deepEqual(JSON.parse(calls[2].body), { ...mailSettings, auth_code: "synthetic-client-auth-code" });
  assert.deepEqual(JSON.parse(calls[3].body), A);
  for (const call of calls.slice(2)) {
    assert.equal(call.headers["X-G15-Console-Action"], "notifications");
    assert.equal(call.credentials, "same-origin");
    assert.equal(call.redirect, "error");
    assert.equal(call.url.includes("synthetic-client-auth-code"), false);
    assert.equal(call.url.includes(mailSettings.recipient), false);
  }
  assert.equal(Object.hasOwn(emailNotificationSettings(mailSettings), "auth_code"), false);
  assert.equal(emailNotificationSettings({ ...mailSettings, auth_code: "" }).auth_code, "");
});

test("invalid mail addresses, authorization codes and channel cannot reach fetch", () => {
  let fetched = 0;
  const client = createEmailNotificationsClient({ fetchImpl: async () => { fetched += 1; return json({}); } });
  for (const patch of [{ sender: "wrong@example.com" }, { sender: "" }, { recipient: "" },
    { recipient: "two@example.com,another@example.com" }, { recipient: "Display <one@example.com>" },
    { recipient: "one@example.com\r\nBcc:evil@example.com" }, { recipient: "用户@example.com" },
    { auth_code: 1 }, { auth_code: "code\nprivate" }, { channel: "pushplus" }, { threshold: false }]) {
    assert.throws(() => client.save({ ...mailSettings, ...patch }));
  }
  assert.equal(fetched, 0);
});

test("email authorization code never enters observable state, even if returned in settings", async () => {
  const secret = "synthetic-code-that-must-not-be-retained";
  let saved;
  const controller = createEmailNotificationsController({ client: {
    save: async (body) => { saved = body; },
    settings: async () => mailStatus({ settings: { ...mailSettings, auth_code: secret, token: secret } }),
  } });
  const snapshots = [];
  controller.subscribe((value) => snapshots.push(JSON.stringify(value)));
  await controller.save({ ...mailSettings, auth_code: secret });
  assert.equal(saved.auth_code, secret);
  assert.equal(snapshots.some((value) => value.includes(secret)), false);
  assert.deepEqual(controller.getState().settings, mailSettings);
});

test("unconfigured empty email settings render without pretending to be configured", async () => {
  const controller = createEmailNotificationsController({ client: {
    settings: async () => mailStatus({ settings: { ...mailSettings, sender: "", recipient: "" }, configured: false }),
  } });
  await controller.refresh();
  assert.equal(controller.getState().error, "");
  assert.equal(controller.getState().configured, false);
  assert.equal(controller.getState().settings.sender, "");
});

test("email preview preserves full report and both attachment descriptions without sending", async () => {
  let sends = 0;
  const controller = createEmailNotificationsController({ client: {
    preview: async () => ({ title: "邮件预览", markdown: "建议\n完整报告\n最后一行", red_count: 4, attachments }),
    test: async () => { sends += 1; },
  } });
  controller.select(A); await controller.preview();
  assert.equal(controller.getState().preview.markdown, "建议\n完整报告\n最后一行");
  assert.deepEqual(controller.getState().preview.attachments, attachments);
  assert.equal(sends, 0);
});

test("incomplete attachment metadata cannot be presented as a complete email preview", async () => {
  const controller = createEmailNotificationsController({ client: {
    preview: async () => ({ title: "Incomplete", markdown: "report", red_count: 1, attachments: attachments.slice(0, 1) }),
  } });
  controller.select(A); await controller.preview();
  assert.equal(controller.getState().preview, null);
  assert.notEqual(controller.getState().error, "");
});

test("SMTP accepted and uncertain test history do not claim phone delivery or allow repeated sends", async () => {
  let sends = 0;
  const row = { ...A, kind: "test", state: "retry_wait", uncertain: true };
  const controller = createEmailNotificationsController({ client: {
    settings: async () => mailStatus({ history: [row] }), test: async () => { sends += 1; },
  } });
  controller.select(A); await controller.refresh(); await controller.test();
  assert.equal(sends, 0);
  assert.equal(notificationTestPending(controller.getState()), true);
  assert.equal(notificationHistoryState("provider_accepted", "smtp163"), "SMTP 已接收");
  assert.equal(notificationHistoryState("provider_accepted"), "渠道已接收");
  assert.equal(emailNotificationErrorText("smtp_auth_failed").includes("授权码"), true);
  assert.equal(emailNotificationErrorText("smtp_recipient_rejected").includes("收件邮箱"), true);
  assert.equal(emailNotificationErrorText("smtp_timeout").includes("待确认"), true);
});

test("an uncertain request failure blocks another explicit email test", async () => {
  let sends = 0;
  const controller = createEmailNotificationsController({ client: {
    settings: async () => mailStatus(),
    test: async () => { sends += 1; throw Object.assign(new Error("private provider detail"), { code: "request_failed" }); },
  } });
  controller.select(A); await controller.refresh(); await controller.test(); await controller.test();
  assert.equal(sends, 1);
  assert.equal(controller.getState().testUncertain, true);
  assert.equal(controller.getState().error.includes("private provider detail"), false);
});

function mountedDocument() {
  class Element {
    constructor() { this.value = ""; this.checked = false; this.dataset = {}; this.children = []; this.listeners = new Map(); }
    addEventListener(type, listener) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(listener); }
    removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    async emit(type, properties = {}) { await Promise.all([...this.listeners.get(type) || []].map((listener) => listener({ target: this, preventDefault() {}, ...properties }))); }
  }
  const html = readFileSync(new URL("../../src/g15/console/static/index.html", import.meta.url), "utf8");
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  const doc = new Element(); doc.hidden = true;
  doc.getElementById = (id) => { assert.equal(elements.has(id), true, `missing UI element ${id}`); return elements.get(id); };
  doc.createElement = () => new Element();
  return doc;
}

test("channel switching keeps independent settings, drafts and password fields without sending", async (t) => {
  const doc = mountedDocument(); let mailCalls = 0, pushCalls = 0, sends = 0;
  const mounted = mountNotifications({ document: doc, clients: {
    smtp163: { settings: async () => { mailCalls += 1; return mailStatus(); }, test: async () => { sends += 1; } },
    pushplus: { settings: async () => { pushCalls += 1; return status({ settings: { ...settings, threshold: 9 } }); }, test: async () => { sends += 1; } },
  } });
  t.after(() => mounted.destroy());
  mounted.sync({ view: "notifications", selected: A, devices: [A] }); await mounted.refresh();
  const $ = doc.getElementById;
  assert.equal($("notification-channel-choice").value, "smtp163");
  assert.equal($("notification-rule-help").textContent.includes("逐封排队发送"), true);
  assert.equal($("notification-rule-help").textContent.includes("合并"), false);
  assert.equal(pushCalls, 0);
  $("notification-threshold").value = "12";
  $("notification-email-recipient").value = "draft-recipient@example.com";
  $("notification-email-auth").value = "unsaved-private-code";
  await $("notification-form").emit("input");
  await mounted.refresh();
  assert.equal($("notification-email-recipient").value, "draft-recipient@example.com");
  $("notification-channel-choice").value = "pushplus"; await $("notification-channel-choice").emit("change"); await mounted.refresh();
  assert.equal($("notification-threshold").value, 9);
  assert.equal($("notification-rule-help").textContent.includes("间隔内新红色事件合并提醒"), true);
  assert.equal($("notification-email-fields").hidden, true);
  assert.equal($("notification-email-auth").disabled, true);
  $("notification-channel-choice").value = "smtp163"; await $("notification-channel-choice").emit("change"); await mounted.refresh();
  assert.equal($("notification-threshold").value, "12");
  assert.equal($("notification-rule-help").textContent.includes("逐封排队发送"), true);
  assert.equal($("notification-rule-help").textContent.includes("合并"), false);
  assert.equal($("notification-email-recipient").value, "draft-recipient@example.com");
  assert.equal($("notification-email-auth").value, "unsaved-private-code");
  assert.equal($("notification-test").disabled, true);
  assert.equal(mailCalls > 0 && pushCalls > 0, true);
  assert.equal(sends, 0);
});

test("saving clears only the submitted authorization code and retains edits made during the request", async (t) => {
  const doc = mountedDocument(), pending = deferred(); let saved, stored = { ...mailSettings };
  const mounted = mountNotifications({ document: doc, clients: {
    smtp163: {
      settings: async () => mailStatus({ settings: stored }),
      save: async (values) => { saved = values; await pending.promise; stored = { ...mailSettings, recipient: values.recipient }; },
    },
    pushplus: { settings: async () => status() },
  } });
  t.after(() => mounted.destroy());
  mounted.sync({ view: "notifications", selected: A, devices: [A] }); await mounted.refresh();
  const $ = doc.getElementById;
  $("notification-email-auth").value = "synthetic-smtp-code";
  $("notification-token").value = "different-channel-token";
  await $("notification-form").emit("input");
  const operation = $("notification-form").emit("submit");
  assert.equal($("notification-email-auth").value, "");
  assert.equal($("notification-token").value, "different-channel-token");
  assert.equal($("notification-channel-choice").disabled, true);
  assert.equal(saved.auth_code, "synthetic-smtp-code");
  $("notification-email-recipient").value = "new-unsaved-recipient@example.com";
  await $("notification-form").emit("input");
  pending.resolve(); await operation;
  assert.equal($("notification-email-recipient").value, "new-unsaved-recipient@example.com");
  assert.equal($("notification-test").disabled, true);
  assert.equal($("notification-channel-choice").disabled, false);
});


test("delete and cancel controls use explicit scoped POST requests with empty bodies", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => { calls.push({ url, ...options }); return json(mailStatus()); };
  const mail = createEmailNotificationsClient({ fetchImpl });
  const push = createNotificationsClient({ fetchImpl });
  assert.equal(calls.length, 0);
  await mail.deleteSettings(); await mail.cancelPending(); await push.deleteSettings();
  assert.deepEqual(calls.map((call) => call.url), ["/api/email-notifications/settings/delete", "/api/email-notifications/pending/cancel", "/api/notifications/settings/delete"]);
  assert.equal(push.cancelPending, undefined);
  for (const call of calls) {
    assert.equal(call.method, "POST"); assert.equal(call.body, "{}");
    assert.equal(call.headers["X-G15-Console-Action"], "notifications");
    assert.equal(call.headers["Content-Type"], "application/json");
    assert.equal(call.credentials, "same-origin"); assert.equal(call.redirect, "error");
  }
});

const deletedMailStatus = (extra = {}) => mailStatus({ settings: { ...mailSettings, sender: "", recipient: "" },
  configured: false, deleted: true, cancelled_count: 3, ...extra });

test("delete blocks duplicate actions and ignores a late pre-deletion settings refresh", async () => {
  const oldRefresh = deferred(), deleting = deferred(); let refreshCalls = 0, deletions = 0, tests = 0;
  const controller = createEmailNotificationsController({ client: {
    settings: async () => ++refreshCalls === 1 ? mailStatus() : oldRefresh.promise,
    deleteSettings: async () => { deletions += 1; await deleting.promise; return deletedMailStatus(); },
    test: async () => { tests += 1; },
  } });
  controller.select(A); await controller.refresh();
  const refreshOperation = controller.refresh();
  const deletion = controller.deleteSettings();
  assert.equal(controller.getState().deleting, true);
  assert.equal(await controller.deleteSettings(), false); await controller.test();
  assert.equal(deletions, 1); assert.equal(tests, 0);
  deleting.resolve(); assert.equal(await deletion, true);
  oldRefresh.resolve(mailStatus()); await refreshOperation;
  assert.equal(controller.getState().configured, false);
  assert.equal(controller.getState().settings.enabled, false);
  assert.equal(controller.getState().settings.sender, "");
  assert.equal(controller.getState().preview, null);
  assert.equal(controller.getState().deleting, false);
});

test("cancel pending requires a saved paused policy and keeps channel configuration", async () => {
  let enabled = true, cancellations = 0;
  const cancelled = { ...A, kind: "automatic", state: "failed", attempts: 0, error_code: "notification_cancelled" };
  const controller = createEmailNotificationsController({ client: {
    settings: async () => mailStatus({ settings: { ...mailSettings, enabled } }),
    cancelPending: async () => { cancellations += 1; return mailStatus({ cancelled_count: 8, history: [cancelled] }); },
  } });
  await controller.refresh(); assert.equal(await controller.cancelPending(), false);
  assert.equal(cancellations, 0); assert.equal(controller.getState().error.includes("先关闭"), true);
  enabled = false; await controller.refresh(); assert.equal(await controller.cancelPending(), true);
  assert.equal(cancellations, 1);
  assert.equal(controller.getState().configured, true);
  assert.deepEqual(controller.getState().settings, mailSettings);
  assert.equal(controller.getState().settings.enabled, false);
  assert.equal(notificationHistoryState("failed", "smtp163", "notification_cancelled"), "已取消 · 未发送");
  assert.equal(notificationHistoryState("failed", "smtp163"), "发送失败");
  assert.equal(notificationErrorText("notification_cancelled").includes("未尝试发送"), true);
});

test("busy deletion preserves configured state and presents a safe recovery message", async () => {
  const controller = createEmailNotificationsController({ client: {
    settings: async () => mailStatus(),
    deleteSettings: async () => { throw Object.assign(new Error("private transport text"), { code: "notification_busy" }); },
  } });
  await controller.refresh(); assert.equal(await controller.deleteSettings(), false);
  assert.equal(controller.getState().configured, true);
  assert.deepEqual(controller.getState().settings, mailSettings);
  assert.equal(controller.getState().error.includes("等待确认"), true);
  assert.equal(controller.getState().error.includes("private transport"), false);
  assert.equal(controller.getState().deleting, false);
});

test("delete requires inline confirmation and clears only current-channel fields and draft", async (t) => {
  const doc = mountedDocument(); let deletions = 0, saves = 0;
  const mounted = mountNotifications({ document: doc, clients: {
    smtp163: {
      settings: async () => mailStatus(), save: async () => { saves += 1; },
      deleteSettings: async () => { deletions += 1; return deletedMailStatus(); },
    },
    pushplus: { settings: async () => status({ settings: { ...settings, threshold: 9 } }) },
  } });
  t.after(() => mounted.destroy());
  mounted.sync({ view: "notifications", selected: A, devices: [A] }); await mounted.refresh();
  const $ = doc.getElementById;
  $("notification-token").value = "other-channel-unsaved-token";
  $("notification-channel-choice").value = "pushplus"; await $("notification-channel-choice").emit("change"); await mounted.refresh();
  $("notification-threshold").value = "15"; await $("notification-form").emit("input");
  $("notification-channel-choice").value = "smtp163"; await $("notification-channel-choice").emit("change"); await mounted.refresh();
  $("notification-email-auth").value = "unsaved-synthetic-auth-code";
  $("notification-email-recipient").value = "unsaved-recipient@example.com";
  await $("notification-form").emit("input");
  await $("notification-delete-settings").emit("click");
  assert.equal(deletions, 0); assert.equal($("notification-confirmation").hidden, false);
  assert.equal($("notification-confirm-description").textContent.includes("未保存的更改"), true);
  await $("notification-confirm-dismiss").emit("click");
  assert.equal(deletions, 0); assert.equal($("notification-confirmation").hidden, true);
  await $("notification-delete-settings").emit("click"); await $("notification-confirm-action").emit("click");
  assert.equal(deletions, 1); assert.equal(saves, 0);
  assert.equal($("notification-confirmation").hidden, true);
  assert.equal($("notification-email-auth").value, "");
  assert.equal($("notification-email-sender").value, "");
  assert.equal($("notification-email-recipient").value, "");
  assert.equal($("notification-enabled").checked, false);
  assert.equal($("notification-test").disabled, true);
  assert.equal($("notification-token").value, "other-channel-unsaved-token");
  $("notification-channel-choice").value = "pushplus"; await $("notification-channel-choice").emit("change"); await mounted.refresh();
  assert.equal($("notification-threshold").value, "15");
});

test("cancel pending confirmation keeps unsaved fields and never submits settings or a message", async (t) => {
  const doc = mountedDocument(); let cancellations = 0, saves = 0, tests = 0;
  const mounted = mountNotifications({ document: doc, clients: {
    smtp163: {
      settings: async () => mailStatus(), save: async () => { saves += 1; }, test: async () => { tests += 1; },
      cancelPending: async () => { cancellations += 1; return mailStatus({ cancelled_count: 4 }); },
    }, pushplus: { settings: async () => status() },
  } });
  t.after(() => mounted.destroy()); mounted.sync({ view: "notifications", selected: A, devices: [A] }); await mounted.refresh();
  const $ = doc.getElementById;
  $("notification-email-auth").value = "unsaved-synthetic-code";
  $("notification-email-recipient").value = "unsaved-new-recipient@example.com";
  await $("notification-form").emit("input");
  await $("notification-cancel-pending").emit("click"); assert.equal(cancellations, 0);
  assert.equal($("notification-confirm-description").textContent.includes("未保存的更改"), true);
  await $("notification-confirm-action").emit("click");
  assert.equal(cancellations, 1); assert.equal(saves, 0); assert.equal(tests, 0);
  assert.equal($("notification-email-auth").value, "unsaved-synthetic-code");
  assert.equal($("notification-email-recipient").value, "unsaved-new-recipient@example.com");
  assert.equal($("notification-enabled").checked, false);
  assert.equal($("notification-feedback").textContent.includes("取消 4 封"), true);
});

test("changing channel or editing fields dismisses a stale destructive confirmation", async (t) => {
  const doc = mountedDocument(); let deletions = 0;
  const mounted = mountNotifications({ document: doc, clients: {
    smtp163: { settings: async () => mailStatus(), deleteSettings: async () => { deletions += 1; return deletedMailStatus(); } },
    pushplus: { settings: async () => status() },
  } });
  t.after(() => mounted.destroy()); mounted.sync({ view: "notifications", selected: A, devices: [A] }); await mounted.refresh();
  const $ = doc.getElementById;
  await $("notification-delete-settings").emit("click");
  await $("notification-form").emit("input"); await $("notification-confirm-action").emit("click"); assert.equal(deletions, 0);
  await $("notification-delete-settings").emit("click");
  $("notification-channel-choice").value = "pushplus"; await $("notification-channel-choice").emit("change"); await mounted.refresh();
  assert.equal($("notification-confirmation").hidden, true);
  assert.equal($("notification-cancel-pending").hidden, true);
  await $("notification-confirm-action").emit("click"); assert.equal(deletions, 0);
});
