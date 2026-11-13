import test from "node:test";
import assert from "node:assert/strict";
import { createNotificationsClient, createNotificationsController, notificationSettings,
  notificationHistoryState, notificationErrorText } from "../../src/g15/console/static/notifications.js";

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
