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

function pending() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

async function loadPanel(responses, options = {}) {
  const { hidden = false } = options;
  const controller = "controller" in options ? options.controller : AbortController;
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
    AbortController: controller,
    async fetch(url, options) {
      requests.push({ url, options });
      assert.ok(responses.length, "unexpected polling request");
      const next = responses.shift();
      return typeof next === "function" ? next(options) : next;
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
  const fireTimer = async (delay) => {
    const matches = [...timers].filter(([, timer]) => timer.delay === delay);
    assert.equal(matches.length, 1, `expected one ${delay}ms timer`);
    const [id, timer] = matches[0];
    timers.delete(id);
    void timer.callback();
    await settle();
  };
  return {
    nodes,
    requests,
    announcements,
    timers,
    async retry() {
      assert.equal(timers.size, 1);
      await fireTimer(3000);
    },
    async expire() {
      assert.equal(timers.size, 1);
      await fireTimer(15000);
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

for (const stage of ["headers", "body"]) {
  test(`a stalled ${stage} read expires, aborts and recovers on the next poll`, async () => {
    const stalled = pending();
    const initial = response(200, "application/json");
    initial.json = () => stalled.promise;
    const panel = await loadPanel([
      stage === "headers" ? stalled.promise : initial,
      response(200, "application/json", {
        task: { state: "RUNNING", attempt_number: 2 },
      }),
    ]);

    assert.equal(panel.requests.length, 1);
    assert.equal(panel.requests[0].options.signal.aborted, false);
    await panel.visibility(true);
    await panel.visibility(false);
    assert.equal(panel.requests.length, 1);
    assert.equal(panel.timers.size, 1);
    assert.equal([...panel.timers.values()][0].delay, 15000);

    await panel.expire();
    assert.equal(panel.requests[0].options.signal.aborted, true);
    assert.equal(panel.nodes.state.textContent, "durable state");
    assert.equal(panel.nodes.attempt.textContent, "durable attempt");
    assert.equal(panel.nodes.workflow.textContent, "durable workflow");
    assert.equal(
      panel.nodes.status.textContent,
      "Status update unavailable; durable page data is unchanged.",
    );
    assert.equal(panel.requests.length, 1);
    assert.equal([...panel.timers.values()][0].delay, 3000);

    await panel.retry();
    assert.equal(panel.requests.length, 2);
    assert.equal(panel.requests[1].options.signal.aborted, false);
    assert.equal(panel.nodes.state.textContent, "RUNNING");
    assert.equal(panel.nodes.attempt.textContent, "2");
    assert.equal(panel.timers.size, 1);
    assert.equal([...panel.timers.values()][0].delay, 3000);
  });

  test(`a normal abort rejection during ${stage} expiry is handled once`, async () => {
    const panel = await loadPanel([
      ({ signal }) => {
        const aborted = new Promise((_, reject) => {
          signal.addEventListener("abort", () => reject(new Error("aborted")));
        });
        if (stage === "headers") return aborted;
        const initial = response(200, "application/json");
        initial.json = () => aborted;
        return initial;
      },
    ]);

    await panel.expire();
    assert.equal(panel.requests[0].options.signal.aborted, true);
    assert.equal(panel.announcements.length, 1);
    assert.equal(panel.timers.size, 1);
    assert.equal([...panel.timers.values()][0].delay, 3000);
  });
}

test("one deadline covers headers followed by a stalled body", async () => {
  const headers = pending();
  const body = pending();
  const panel = await loadPanel([headers.promise]);
  const originalDeadline = [...panel.timers.keys()][0];
  const initial = response(200, "application/json");
  initial.json = () => body.promise;

  headers.resolve(initial);
  await settle();
  assert.deepEqual([...panel.timers.keys()], [originalDeadline]);
  await panel.expire();
  assert.equal(panel.requests[0].options.signal.aborted, true);
  assert.equal(panel.nodes.state.textContent, "durable state");
  assert.equal(panel.timers.size, 1);
});

for (const [label, stage, lateResult, rejects] of [
  [
    "terminal headers", "headers",
    response(200, "application/json", { state: "SUCCEEDED" }),
  ],
  ["unauthorized headers", "headers", response(401, "application/json")],
  ["redirected headers", "headers", response(200, "application/json", {}, true)],
  ["failed headers", "headers", response(503, "text/html")],
  ["header rejection", "headers", new Error("late transport failure"), true],
  ["terminal body", "body", { state: "SUCCEEDED", attempt_number: 1 }],
  ["older running body", "body", { state: "RUNNING", attempt_number: 1 }],
  ["body rejection", "body", new Error("late JSON failure"), true],
]) {
  test(`late ${label} cannot affect a replacement poll`, async () => {
    const oldRead = pending();
    const replacement = pending();
    const initial = response(200, "application/json");
    initial.json = () => oldRead.promise;
    const panel = await loadPanel([
      stage === "headers" ? oldRead.promise : initial,
      replacement.promise,
      response(200, "application/json", { state: "RUNNING", attempt_number: 3 }),
    ]);

    await panel.expire();
    await panel.retry();
    assert.equal(panel.requests.length, 2);
    const replacementDeadline = [...panel.timers.keys()][0];
    const announcements = [...panel.announcements];
    if (rejects) oldRead.reject(lateResult);
    else oldRead.resolve(lateResult);
    await settle();

    assert.equal(panel.nodes.state.textContent, "durable state");
    assert.deepEqual(panel.announcements, announcements);
    assert.deepEqual([...panel.timers.keys()], [replacementDeadline]);
    assert.equal(panel.requests[1].options.signal.aborted, false);
    await panel.visibility(true);
    await panel.visibility(false);
    assert.equal(panel.requests.length, 2);

    replacement.resolve(
      response(200, "application/json", { state: "RUNNING", attempt_number: 2 }),
    );
    await settle();
    assert.equal(panel.nodes.attempt.textContent, "2");
    assert.match(
      panel.nodes.status.textContent,
      /^Durable status updated: RUNNING/,
    );
    assert.equal(panel.timers.size, 1);
    assert.equal([...panel.timers.values()][0].delay, 3000);
    await panel.retry();
    assert.equal(panel.nodes.attempt.textContent, "3");
    assert.equal(panel.requests.length, 3);
  });
}

test("expiry while hidden waits for visibility before another request", async () => {
  const panel = await loadPanel([
    pending().promise,
    response(200, "application/json", { state: "RUNNING" }),
  ]);
  await panel.visibility(true);
  assert.equal(panel.timers.size, 1);
  await panel.expire();
  assert.equal(panel.timers.size, 0);
  assert.equal(panel.requests.length, 1);
  await panel.visibility(false);
  assert.equal(panel.requests.length, 2);
  assert.equal(panel.nodes.state.textContent, "RUNNING");
  assert.equal(panel.timers.size, 1);
});

for (const [label, lateResult, rejects] of [
  ["terminal", response(200, "application/json", { state: "SUCCEEDED" })],
  ["unauthorized", response(401, "application/json")],
  ["error", new Error("late failure"), true],
]) {
  test(`late ${label} headers leave a completed replacement unchanged`, async () => {
    const oldRead = pending();
    const panel = await loadPanel([
      oldRead.promise,
      response(200, "application/json", { state: "RUNNING", attempt_number: 2 }),
      response(200, "application/json", { state: "RUNNING", attempt_number: 3 }),
    ]);
    await panel.expire();
    await panel.retry();
    const announcements = [...panel.announcements];
    const retryTimer = [...panel.timers.keys()][0];
    if (rejects) oldRead.reject(lateResult);
    else oldRead.resolve(lateResult);
    await settle();

    assert.equal(panel.nodes.state.textContent, "RUNNING");
    assert.equal(panel.nodes.attempt.textContent, "2");
    assert.deepEqual(panel.announcements, announcements);
    assert.deepEqual([...panel.timers.keys()], [retryTimer]);
    await panel.retry();
    assert.equal(panel.nodes.attempt.textContent, "3");
  });
}

test("an unchanged successful status announces recovery once after expiry", async () => {
  const running = { state: "RUNNING", attempt_number: 2 };
  const panel = await loadPanel([
    response(200, "application/json", running),
    pending().promise,
    response(200, "application/json", running),
    response(200, "application/json", running),
  ]);
  const successMessage = panel.nodes.status.textContent;
  await panel.retry();
  await panel.expire();
  assert.equal(panel.announcements.length, 2);
  await panel.retry();
  assert.equal(panel.nodes.status.textContent, successMessage);
  assert.equal(panel.announcements.length, 3);
  await panel.retry();
  assert.equal(panel.announcements.length, 3);
});

test("successful reads clear the deadline without aborting the next request", async () => {
  const panel = await loadPanel([
    response(200, "application/json", { state: "RUNNING" }),
    pending().promise,
  ]);
  assert.equal(panel.timers.size, 1);
  assert.equal([...panel.timers.values()][0].delay, 3000);
  assert.equal(panel.requests[0].options.signal.aborted, false);
  await panel.retry();
  assert.equal(panel.timers.size, 1);
  assert.equal([...panel.timers.values()][0].delay, 15000);
  await panel.expire();
  assert.equal(panel.requests[0].options.signal.aborted, false);
  assert.equal(panel.requests[1].options.signal.aborted, true);
});

for (const [label, controller] of [
  ["missing", undefined],
  ["broken", class {
    constructor() {
      throw new Error("unavailable controller");
    }
  }],
]) {
  test(`${label} cancellation disables live polling`, async () => {
    const panel = await loadPanel([], { controller });
    assert.equal(panel.requests.length, 0);
    assert.equal(panel.timers.size, 0);
    assert.equal(panel.nodes.state.textContent, "durable state");
    assert.equal(panel.nodes.attempt.textContent, "durable attempt");
    assert.equal(panel.nodes.workflow.textContent, "durable workflow");
    assert.equal(
      panel.nodes.status.textContent,
      "Live updates unavailable in this browser; reload the page for current status.",
    );
    await panel.visibility(true);
    await panel.visibility(false);
    assert.equal(panel.requests.length, 0);
    assert.equal(panel.timers.size, 0);
    assert.equal(panel.announcements.length, 1);
  });
}

test("a throwing abort still recovers with a fresh request controller", async () => {
  let created = 0;
  class FirstAbortThrows extends AbortController {
    constructor() {
      super();
      this.failAbort = created++ === 0;
    }

    abort() {
      if (this.failAbort) throw new Error("broken browser cancellation");
      super.abort();
    }
  }
  const panel = await loadPanel([
    pending().promise,
    response(200, "application/json", { state: "RUNNING", attempt_number: 2 }),
    pending().promise,
  ], { controller: FirstAbortThrows });

  await assert.doesNotReject(panel.expire());
  assert.equal(panel.requests.length, 1);
  assert.equal(panel.requests[0].options.signal.aborted, false);
  assert.equal(panel.nodes.state.textContent, "durable state");
  assert.equal(
    panel.nodes.status.textContent,
    "Status update unavailable; durable page data is unchanged.",
  );
  assert.equal(panel.timers.size, 1);
  assert.equal([...panel.timers.values()][0].delay, 3000);
  await panel.retry();
  assert.equal(created, 2);
  assert.equal(panel.nodes.state.textContent, "RUNNING");
  assert.equal(panel.nodes.attempt.textContent, "2");
  assert.notEqual(panel.requests[0].options.signal, panel.requests[1].options.signal);
  await panel.retry();
  await panel.expire();
  assert.equal(created, 3);
  assert.equal(panel.requests[2].options.signal.aborted, true);
  assert.equal(panel.timers.size, 1);
  assert.equal([...panel.timers.values()][0].delay, 3000);
});
