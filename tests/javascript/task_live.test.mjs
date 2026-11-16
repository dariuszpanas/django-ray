import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const liveScript = fs.readFileSync(
  fileURLToPath(
    new URL("../../src/django_ray/static/django_ray/admin/task_live.js", import.meta.url),
  ),
  "utf8",
);

function response(status, contentType, payload = {}, redirected = false) {
  return {
    status,
    ok: status >= 200 && status < 300,
    redirected,
    headers: { get: () => contentType },
    async json() {
      return payload;
    },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

async function loadPanel(responses, { hidden = false } = {}) {
  const nodes = Object.fromEntries(
    ["state", "attempt", "workflow", "status"].map((name) => [
      name,
      { textContent: `durable ${name}`, dataset: {} },
    ]),
  );
  const requests = [];
  const announcements = [];
  let statusText = nodes.status.textContent;
  Object.defineProperty(nodes.status, "textContent", {
    get: () => statusText,
    set(value) {
      statusText = value;
      announcements.push(value);
    },
  });
  const timers = new Map();
  const listeners = new Map();
  let nextTimer = 0;
  const document = {
    hidden,
    getElementById: () => ({
      dataset: { observabilityUrl: "/admin/task/1/observability/" },
      querySelector: (selector) => nodes[selector.match(/"([^"]+)"/)[1]],
    }),
    addEventListener: (name, listener) => listeners.set(name, listener),
  };
  const window = {
    async fetch(url, options) {
      requests.push({ url, options });
      assert.ok(responses.length, "unexpected polling request");
      return responses.shift();
    },
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
  };
  vm.runInNewContext(liveScript, { document, window });
  await settle();
  return {
    nodes,
    requests,
    announcements,
    timers,
    async retry() {
      assert.equal(timers.size, 1);
      const [id, timer] = timers.entries().next().value;
      assert.equal(timer.delay, 3000);
      timers.delete(id);
      await timer.callback();
    },
    async visibility(hidden) {
      document.hidden = hidden;
      listeners.get("visibilitychange")();
      await settle();
    },
  };
}

for (const [status, contentType] of [
  [502, "text/html"],
  [503, "text/html"],
  [503, "text/plain"],
  [504, null],
]) {
  test(`${status} with ${contentType} preserves durable data and retries`, async () => {
    const panel = await loadPanel([
      response(status, contentType),
      response(200, "application/json", { task: { state: "RUNNING", attempt_number: 2 } }),
    ]);

    assert.equal(panel.nodes.state.textContent, "durable state");
    assert.equal(panel.nodes.attempt.textContent, "durable attempt");
    assert.equal(panel.nodes.workflow.textContent, "durable workflow");
    assert.equal(
      panel.nodes.status.textContent,
      "Status update unavailable; durable page data is unchanged.",
    );
    await panel.retry();

    assert.equal(panel.nodes.state.textContent, "RUNNING");
    assert.equal(panel.nodes.state.dataset.state, "RUNNING");
    assert.equal(panel.nodes.attempt.textContent, "2");
    assert.equal(panel.requests.length, 2);
    assert.equal(panel.requests[1].options.credentials, "same-origin");
    assert.equal(panel.requests[1].options.cache, "no-store");
    assert.equal(panel.timers.size, 1);
  });
}

for (const initialResponse of [
  response(401, "application/json"),
  response(403, "text/html"),
  response(404, null),
  response(200, "application/json", {}, true),
  response(200, "text/html"),
]) {
  test(`authentication or unavailable response ${initialResponse.status}/${initialResponse.headers.get()} stops polling`, async () => {
    const panel = await loadPanel([initialResponse]);

    assert.equal(panel.timers.size, 0);
    assert.equal(panel.nodes.state.textContent, "durable state");
    assert.equal(
      panel.nodes.status.textContent,
      "Live updates stopped; reload after signing in again.",
    );
    await panel.visibility(true);
    await panel.visibility(false);
    assert.equal(panel.requests.length, 1);
  });
}

test("successful terminal task updates stop polling", async () => {
  const panel = await loadPanel([
    response(200, "application/json", { task: { state: "SUCCEEDED", attempt_number: 1 } }),
  ]);

  assert.equal(panel.nodes.state.textContent, "SUCCEEDED");
  assert.equal(panel.timers.size, 0);
  await panel.visibility(false);
  assert.equal(panel.requests.length, 1);
});

test("an unchanged successful status announces recovery once after a polling error", async () => {
  const running = { task: { state: "RUNNING", attempt_number: 2 } };
  const panel = await loadPanel([
    response(200, "application/json", running),
    response(200, "application/json", running),
    response(503, "text/html"),
    response(200, "application/json", running),
    response(200, "application/json", running),
  ]);

  const successMessage = panel.nodes.status.textContent;
  assert.match(successMessage, /^Durable status updated: RUNNING/);
  await panel.retry();
  assert.equal(panel.announcements.length, 1);
  await panel.retry();
  assert.equal(
    panel.nodes.status.textContent,
    "Status update unavailable; durable page data is unchanged.",
  );
  await panel.retry();
  assert.equal(panel.nodes.status.textContent, successMessage);
  assert.equal(panel.announcements.length, 3);
  await panel.retry();
  assert.equal(panel.announcements.length, 3);
});

test("a hidden page waits for visibility before recovering from a transient error", async () => {
  const panel = await loadPanel([
    response(503, "text/html"),
    response(200, "application/json", { task: { state: "RUNNING" } }),
  ], { hidden: true });

  assert.equal(panel.requests.length, 0);
  assert.equal(panel.timers.size, 0);
  await panel.visibility(false);
  assert.equal(panel.requests.length, 1);
  await panel.visibility(true);
  assert.equal(panel.timers.size, 0);
  await panel.visibility(false);
  assert.equal(panel.nodes.state.textContent, "RUNNING");
  assert.equal(panel.requests.length, 2);
  assert.equal(panel.timers.size, 1);
});
