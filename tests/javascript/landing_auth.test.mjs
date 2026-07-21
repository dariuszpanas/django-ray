import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const dashboardScript = fs.readFileSync(
  fileURLToPath(
    new URL("../../testproject/static/testproject/landing.js", import.meta.url),
  ),
  "utf8",
);

const elementIds = [
  "api-token",
  "use-token",
  "credential-status",
  "forget-token",
  "output",
  "trigger",
  "view-metrics",
  "view-executions",
  "protected-response",
  "protected-response-title",
  "protected-response-body",
  "close-protected-response",
  "stat-total",
  "stat-queued",
  "stat-running",
  "stat-succeeded",
  "stat-failed",
];

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...values) {
    for (const value of values) this.values.add(value);
  }

  remove(...values) {
    for (const value of values) this.values.delete(value);
  }

  contains(value) {
    return this.values.has(value);
  }
}

class FakeElement {
  constructor() {
    this.classList = new FakeClassList();
    this.disabled = false;
    this.focused = false;
    this.hidden = true;
    this.listeners = new Map();
    this.textContent = "";
    this.value = "";
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  focus() {
    this.focused = true;
  }

  scrollIntoView() {}
}

class FakeHeaders {
  constructor(initial = {}) {
    this.values = new Map();
    if (initial instanceof FakeHeaders) {
      for (const [name, value] of initial.values) this.set(name, value);
      return;
    }
    for (const [name, value] of Object.entries(initial)) this.set(name, value);
  }

  get(name) {
    return this.values.get(name.toLowerCase());
  }

  set(name, value) {
    this.values.set(name.toLowerCase(), String(value));
  }
}

function response(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return payload;
    },
    async text() {
      return String(payload);
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function loadDashboard() {
  const elements = new Map(elementIds.map((id) => [id, new FakeElement()]));
  const fetchCalls = [];
  const fetchResponses = [];
  const scheduled = [];

  globalThis.document = {
    querySelector(selector) {
      return elements.get(selector.slice(1));
    },
  };
  globalThis.Headers = FakeHeaders;
  globalThis.window = {
    async fetch(url, options) {
      fetchCalls.push({ url, options });
      assert.notEqual(fetchResponses.length, 0, `unexpected fetch for ${url}`);
      return fetchResponses.shift();
    },
    setTimeout(callback) {
      scheduled.push(callback);
    },
  };
  vm.runInThisContext(dashboardScript, { filename: "landing.js" });

  return {
    elements,
    fetchCalls,
    fetchResponses,
    scheduled,
    async click(id) {
      return elements.get(id).listeners.get("click")({});
    },
  };
}

const stats = {
  total: 1,
  queued: 0,
  running: 0,
  succeeded: 1,
  failed: 0,
};

test("statistics and enqueue use the shared bearer request path", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "valid-dashboard-token";

  await dashboard.click("use-token");

  assert.equal(dashboard.elements.get("api-token").value, "");
  assert.equal(dashboard.fetchCalls[0].url, "/api/executions/stats");
  assert.equal(
    dashboard.fetchCalls[0].options.headers.get("Authorization"),
    "Bearer valid-dashboard-token",
  );
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this loaded page.",
  );

  dashboard.fetchResponses.push(response(200, { task_id: "task-1", status: "READY" }));
  await dashboard.click("trigger");

  assert.equal(dashboard.fetchCalls[1].url, "/api/enqueue/add/2/3");
  assert.equal(dashboard.fetchCalls[1].options.method, "POST");
  assert.equal(
    dashboard.fetchCalls[1].options.headers.get("Authorization"),
    "Bearer valid-dashboard-token",
  );

  dashboard.fetchResponses.push(response(200, stats));
  await dashboard.scheduled.shift()();
  assert.equal(dashboard.fetchCalls[2].url, "/api/executions/stats");
  assert.equal(
    dashboard.fetchCalls[2].options.headers.get("Authorization"),
    "Bearer valid-dashboard-token",
  );
});

test("a stale rejection cannot clear a newer verified credential", async () => {
  const dashboard = loadDashboard();
  const oldResponse = deferred();
  dashboard.fetchResponses.push(oldResponse.promise, response(200, stats));

  dashboard.elements.get("api-token").value = "old-dashboard-token";
  const oldAttempt = dashboard.click("use-token");
  dashboard.elements.get("api-token").value = "new-dashboard-token";
  await dashboard.click("use-token");

  oldResponse.resolve(response(401, { detail: "Unauthorized" }));
  await oldAttempt;
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this loaded page.",
  );

  dashboard.fetchResponses.push(response(200, { task_id: "task-2", status: "READY" }));
  await dashboard.click("trigger");
  assert.equal(
    dashboard.fetchCalls.at(-1).options.headers.get("Authorization"),
    "Bearer new-dashboard-token",
  );
});

test("forgetting a credential wins over an in-flight successful check", async () => {
  const dashboard = loadDashboard();
  const pendingResponse = deferred();
  dashboard.fetchResponses.push(pendingResponse.promise);
  dashboard.elements.get("api-token").value = "soon-forgotten-token";

  const attempt = dashboard.click("use-token");
  await dashboard.click("forget-token");
  pendingResponse.resolve(response(200, stats));
  await attempt;

  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Not authenticated. The in-memory API token was forgotten.",
  );
  assert.equal(
    dashboard.elements.get("credential-status").classList.contains("authenticated"),
    false,
  );
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.click("trigger");
  assert.equal(dashboard.fetchCalls.length, fetchCount);
});

test("forgetting a credential fences an in-flight protected response", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "protected-response-token";
  await dashboard.click("use-token");

  const pendingResponse = deferred();
  dashboard.fetchResponses.push(pendingResponse.promise);
  const metricsRequest = dashboard.click("view-metrics");
  await dashboard.click("forget-token");
  pendingResponse.resolve(response(200, "secret metrics payload"));
  await metricsRequest;

  assert.equal(dashboard.elements.get("protected-response").hidden, true);
  assert.equal(dashboard.elements.get("protected-response-body").textContent, "");
  assert.equal(dashboard.elements.get("output").textContent, "Browser API token forgotten.");
});

test("an unverifiable candidate is discarded after a server error", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(503, { detail: "Unavailable" }));
  dashboard.elements.get("api-token").value = "unverified-dashboard-token";

  await dashboard.click("use-token");

  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "The API token could not be verified. Paste it again to retry.",
  );
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.click("trigger");
  assert.equal(dashboard.fetchCalls.length, fetchCount);
});
