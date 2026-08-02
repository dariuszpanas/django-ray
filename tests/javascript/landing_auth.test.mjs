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
const sessionCredentialKey = "django-ray.testproject.api-token.v1";

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
  "stat-expired",
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

class FakeSessionStorage {
  constructor(initial = {}) {
    this.values = new Map(Object.entries(initial));
    this.throwOnGet = false;
    this.throwOnGetAfterSet = false;
    this.throwOnSet = false;
    this.throwOnRemove = false;
    this.throwOnRemoveAfterSet = false;
    this.setCount = 0;
  }

  getItem(key) {
    if (this.throwOnGet || (this.throwOnGetAfterSet && this.setCount > 0)) {
      throw new Error("session storage get failed");
    }
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    if (this.throwOnSet) throw new Error("session storage set failed");
    this.values.set(key, String(value));
    this.setCount += 1;
  }

  removeItem(key) {
    if (this.throwOnRemove || (this.throwOnRemoveAfterSet && this.setCount > 0)) {
      throw new Error("session storage remove failed");
    }
    this.values.delete(key);
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

function loadDashboard({
  sessionStorage = new FakeSessionStorage(),
  initialFetchResponses = [],
  authorizationHeaderFailure = false,
} = {}) {
  const elements = new Map(elementIds.map((id) => [id, new FakeElement()]));
  elements.get("credential-status").textContent =
    "Not authenticated. Protected actions will ask for a token.";
  elements.get("output").textContent =
    "Enter the local demo API token to enable protected actions.";
  const fetchCalls = [];
  const fetchResponses = [...initialFetchResponses];
  const scheduled = [];

  globalThis.document = {
    querySelector(selector) {
      return elements.get(selector.slice(1));
    },
  };
  globalThis.Headers = class DashboardHeaders extends FakeHeaders {
    set(name, value) {
      if (authorizationHeaderFailure && name.toLowerCase() === "authorization") {
        throw new Error(`invalid header value: ${value}`);
      }
      super.set(name, value);
    }
  };
  globalThis.window = {
    sessionStorage,
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
    sessionStorage,
    async click(id) {
      return elements.get(id).listeners.get("click")({});
    },
  };
}

async function settleDashboard() {
  await new Promise((resolve) => setImmediate(resolve));
}

function assertCredentialNotRendered(dashboard, token) {
  for (const element of dashboard.elements.values()) {
    assert.equal(String(element.value).includes(token), false);
    assert.equal(String(element.textContent).includes(token), false);
  }
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
    "Authenticated for this browser session.",
  );
  assert.equal(
    dashboard.sessionStorage.getItem(sessionCredentialKey),
    "valid-dashboard-token",
  );
  assertCredentialNotRendered(dashboard, "valid-dashboard-token");

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

test("a stale 401 cannot clear a newer verified credential", async () => {
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
    "Authenticated for this browser session.",
  );
  assert.equal(
    dashboard.sessionStorage.getItem(sessionCredentialKey),
    "new-dashboard-token",
  );

  dashboard.fetchResponses.push(response(200, { task_id: "task-2", status: "READY" }));
  await dashboard.click("trigger");
  assert.equal(
    dashboard.fetchCalls.at(-1).options.headers.get("Authorization"),
    "Bearer new-dashboard-token",
  );
});

test("a stale successful check cannot overwrite a newer verified credential", async () => {
  const dashboard = loadDashboard();
  const oldResponse = deferred();
  dashboard.fetchResponses.push(oldResponse.promise, response(200, stats));

  dashboard.elements.get("api-token").value = "old-success-token";
  const oldAttempt = dashboard.click("use-token");
  dashboard.elements.get("api-token").value = "new-success-token";
  await dashboard.click("use-token");

  oldResponse.resolve(response(200, stats));
  await oldAttempt;
  assert.equal(dashboard.sessionStorage.getItem(sessionCredentialKey), "new-success-token");
  dashboard.fetchResponses.push(response(200, { task_id: "task-new", status: "READY" }));
  await dashboard.click("trigger");
  assert.equal(
    dashboard.fetchCalls.at(-1).options.headers.get("Authorization"),
    "Bearer new-success-token",
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
    "Not authenticated. The browser-session API token was forgotten.",
  );
  assert.equal(
    dashboard.elements.get("credential-status").classList.contains("authenticated"),
    false,
  );
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.click("trigger");
  assert.equal(dashboard.fetchCalls.length, fetchCount);
  assert.equal(dashboard.sessionStorage.getItem(sessionCredentialKey), null);

  const reloaded = loadDashboard({ sessionStorage: dashboard.sessionStorage });
  assert.equal(reloaded.fetchCalls.length, 0);
  assert.equal(
    reloaded.elements.get("credential-status").textContent,
    "Not authenticated. Protected actions will ask for a token.",
  );
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
  assert.equal(
    dashboard.elements.get("output").textContent,
    "Browser API token forgotten for this tab session.",
  );
  assert.equal(dashboard.sessionStorage.getItem(sessionCredentialKey), null);
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
  assert.equal(dashboard.sessionStorage.getItem(sessionCredentialKey), null);
});

test("starting a replacement removes the previously verified session token", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "previous-dashboard-token";
  await dashboard.click("use-token");

  const pendingResponse = deferred();
  dashboard.fetchResponses.push(pendingResponse.promise);
  dashboard.elements.get("api-token").value = "replacement-dashboard-token";
  const replacement = dashboard.click("use-token");

  assert.equal(dashboard.sessionStorage.getItem(sessionCredentialKey), null);
  pendingResponse.resolve(response(503, { detail: "Unavailable" }));
  await replacement;
  assert.equal(dashboard.sessionStorage.getItem(sessionCredentialKey), null);
});

test("a verified token survives reload and restores authenticated actions", async () => {
  const sessionStorage = new FakeSessionStorage();
  const initial = loadDashboard({ sessionStorage });
  initial.fetchResponses.push(response(200, stats));
  initial.elements.get("api-token").value = "reload-dashboard-token";
  await initial.click("use-token");

  const reloaded = loadDashboard({
    sessionStorage,
    initialFetchResponses: [response(200, stats)],
  });
  await settleDashboard();

  assert.equal(reloaded.elements.get("api-token").value, "");
  assert.equal(reloaded.fetchCalls[0].url, "/api/executions/stats");
  assert.equal(
    reloaded.fetchCalls[0].options.headers.get("Authorization"),
    "Bearer reload-dashboard-token",
  );
  assert.equal(
    reloaded.elements.get("credential-status").textContent,
    "Authenticated for this browser session.",
  );
  assert.equal(
    reloaded.elements.get("output").textContent,
    "Stored API token restored and task statistics refreshed.",
  );
  assertCredentialNotRendered(reloaded, "reload-dashboard-token");

  reloaded.fetchResponses.push(response(200, { task_id: "task-reload", status: "READY" }));
  await reloaded.click("trigger");
  assert.equal(
    reloaded.fetchCalls.at(-1).options.headers.get("Authorization"),
    "Bearer reload-dashboard-token",
  );
});

test("a restored credential rejected with 401 is removed", async () => {
  const sessionStorage = new FakeSessionStorage({
    [sessionCredentialKey]: "expired-dashboard-token",
  });
  const dashboard = loadDashboard({
    sessionStorage,
    initialFetchResponses: [response(401, { detail: "Unauthorized" })],
  });
  await settleDashboard();

  assert.equal(sessionStorage.getItem(sessionCredentialKey), null);
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "The API token was rejected. Paste a valid token and try again.",
  );
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.click("trigger");
  assert.equal(dashboard.fetchCalls.length, fetchCount);
});

test("a protected action denied with 403 retains the verified credential", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "permission-limited-token";
  await dashboard.click("use-token");

  dashboard.fetchResponses.push(response(403, { detail: "Forbidden" }));
  await dashboard.click("trigger");

  assert.equal(
    dashboard.sessionStorage.getItem(sessionCredentialKey),
    "permission-limited-token",
  );
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this browser session.",
  );
});

test("a transient restore failure retains the previously verified session token", async () => {
  const sessionStorage = new FakeSessionStorage({
    [sessionCredentialKey]: "temporarily-unverifiable-token",
  });
  const dashboard = loadDashboard({
    sessionStorage,
    initialFetchResponses: [response(503, { detail: "Unavailable" })],
  });
  await settleDashboard();

  assert.equal(
    sessionStorage.getItem(sessionCredentialKey),
    "temporarily-unverifiable-token",
  );
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "The stored API token could not be verified yet. Protected actions will retry it.",
  );

  dashboard.fetchResponses.push(response(200, { task_id: "task-retry", status: "READY" }));
  await dashboard.click("trigger");
  assert.equal(
    dashboard.fetchCalls.at(-1).options.headers.get("Authorization"),
    "Bearer temporarily-unverifiable-token",
  );
});

test("session storage read failures force page-memory fallback after verification", async () => {
  const sessionStorage = new FakeSessionStorage();
  sessionStorage.throwOnGet = true;

  const dashboard = loadDashboard({ sessionStorage });

  assert.equal(dashboard.fetchCalls.length, 0);
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Not authenticated. Session storage is unavailable; a verified token will last only until reload.",
  );
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "read-failure-token";
  await dashboard.click("use-token");
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this loaded page; session storage is unavailable.",
  );
  sessionStorage.throwOnGet = false;
  assert.equal(sessionStorage.getItem(sessionCredentialKey), null);
});

test("header construction failures never render the bearer token", async () => {
  const dashboard = loadDashboard({ authorizationHeaderFailure: true });
  const token = "header-error-token-that-must-not-render";
  dashboard.elements.get("api-token").value = token;

  await dashboard.click("use-token");

  assertCredentialNotRendered(dashboard, token);
  assert.equal(
    dashboard.elements.get("output").textContent,
    "The authenticated request could not be sent.",
  );
  assert.equal(dashboard.sessionStorage.getItem(sessionCredentialKey), null);
});

test("session storage write failures fall back to loaded-page memory", async () => {
  const sessionStorage = new FakeSessionStorage();
  sessionStorage.throwOnSet = true;
  const dashboard = loadDashboard({ sessionStorage });
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "memory-fallback-token";

  await dashboard.click("use-token");

  assert.equal(sessionStorage.getItem(sessionCredentialKey), null);
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this loaded page; session storage is unavailable.",
  );
  dashboard.fetchResponses.push(response(200, { task_id: "task-memory", status: "READY" }));
  await dashboard.click("trigger");
  assert.equal(
    dashboard.fetchCalls.at(-1).options.headers.get("Authorization"),
    "Bearer memory-fallback-token",
  );
});

test("failed persistence verification reports uncertain residual session state", async () => {
  const sessionStorage = new FakeSessionStorage();
  sessionStorage.throwOnGetAfterSet = true;
  sessionStorage.throwOnRemoveAfterSet = true;
  const dashboard = loadDashboard({ sessionStorage });
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "uncertain-persistence-token";

  await dashboard.click("use-token");

  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this loaded page, but session storage could not be updated. Close this tab or clear its site data.",
  );
  sessionStorage.throwOnGetAfterSet = false;
  assert.equal(sessionStorage.getItem(sessionCredentialKey), "uncertain-persistence-token");
});

test("a replacement warns when prior session state cannot be removed or updated", async () => {
  const sessionStorage = new FakeSessionStorage();
  const dashboard = loadDashboard({ sessionStorage });
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "old-persisted-token";
  await dashboard.click("use-token");
  sessionStorage.throwOnRemove = true;
  sessionStorage.throwOnSet = true;

  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "new-page-token";
  await dashboard.click("use-token");

  assert.equal(sessionStorage.getItem(sessionCredentialKey), "old-persisted-token");
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this loaded page, but session storage could not be updated. Close this tab or clear its site data.",
  );
  dashboard.fetchResponses.push(response(200, { task_id: "task-new", status: "READY" }));
  await dashboard.click("trigger");
  assert.equal(
    dashboard.fetchCalls.at(-1).options.headers.get("Authorization"),
    "Bearer new-page-token",
  );
});

test("session storage removal failures are reported without retaining page access", async () => {
  const sessionStorage = new FakeSessionStorage();
  const dashboard = loadDashboard({ sessionStorage });
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "removal-failure-token";
  await dashboard.click("use-token");
  sessionStorage.throwOnRemove = true;

  await dashboard.click("forget-token");

  assert.equal(sessionStorage.getItem(sessionCredentialKey), "removal-failure-token");
  assert.match(
    dashboard.elements.get("credential-status").textContent,
    /Session storage could not be cleared/,
  );
  assert.equal(
    dashboard.elements.get("output").textContent,
    "Token cleared from this page, but session storage could not be cleared.",
  );
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.click("trigger");
  assert.equal(dashboard.fetchCalls.length, fetchCount);
});
