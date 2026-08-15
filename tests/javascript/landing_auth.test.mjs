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
const legacySessionCredentialKey = "django-ray.testproject.api-token.v1";
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
    this.getCount = 0;
    this.setCount = 0;
    this.removedKeys = [];
    this.throwOnRemove = false;
  }

  getItem(key) {
    this.getCount += 1;
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.setCount += 1;
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.removedKeys.push(key);
    if (this.throwOnRemove) throw new Error("session storage remove failed");
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
  authorizationHeaderFailure = false,
  sessionStorage = new FakeSessionStorage(),
  sessionStorageAccessFailure = false,
} = {}) {
  const elements = new Map(elementIds.map((id) => [id, new FakeElement()]));
  elements.get("credential-status").textContent =
    "Not authenticated. Protected actions will ask for a token.";
  elements.get("output").textContent =
    "Enter the local demo API token to enable protected actions.";
  const fetchCalls = [];
  const fetchResponses = [];
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
  const windowObject = {
    async fetch(url, options) {
      fetchCalls.push({ url, options });
      assert.notEqual(fetchResponses.length, 0, `unexpected fetch for ${url}`);
      return fetchResponses.shift();
    },
    setTimeout(callback) {
      scheduled.push(callback);
    },
  };
  if (sessionStorageAccessFailure) {
    Object.defineProperty(windowObject, "sessionStorage", {
      get() {
        throw new Error("session storage property unavailable");
      },
    });
  } else {
    windowObject.sessionStorage = sessionStorage;
  }
  globalThis.window = windowObject;
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

test("initial load purges only the legacy session credential without reading it", () => {
  const legacyToken = "legacy-session-token-that-must-not-be-reused";
  const sessionStorage = new FakeSessionStorage({
    [legacySessionCredentialKey]: legacyToken,
    "unrelated.preference": "keep-me",
  });

  const dashboard = loadDashboard({ sessionStorage });

  assert.equal(sessionStorage.values.has(legacySessionCredentialKey), false);
  assert.equal(sessionStorage.values.get("unrelated.preference"), "keep-me");
  assert.deepEqual(sessionStorage.removedKeys, [legacySessionCredentialKey]);
  assert.equal(sessionStorage.getCount, 0);
  assert.equal(sessionStorage.setCount, 0);
  assert.equal(dashboard.fetchCalls.length, 0);
  assertCredentialNotRendered(dashboard, legacyToken);
});

test("unavailable session storage cannot block page-memory authentication", async () => {
  const sessionStorage = new FakeSessionStorage({
    [legacySessionCredentialKey]: "inaccessible-legacy-token",
  });
  sessionStorage.throwOnRemove = true;
  const dashboard = loadDashboard({ sessionStorage });
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "page-memory-token";

  await dashboard.click("use-token");

  assert.equal(sessionStorage.getCount, 0);
  assert.equal(sessionStorage.setCount, 0);
  assert.equal(
    dashboard.fetchCalls[0].options.headers.get("Authorization"),
    "Bearer page-memory-token",
  );
});

test("an unavailable session storage property cannot block initial load", () => {
  const dashboard = loadDashboard({ sessionStorageAccessFailure: true });

  assert.equal(dashboard.fetchCalls.length, 0);
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Not authenticated. Protected actions will ask for a token.",
  );
});

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
    "Authenticated for this loaded page. Reloading clears the token.",
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
    "Authenticated for this loaded page. Reloading clears the token.",
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
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this loaded page. Reloading clears the token.",
  );
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
  dashboard.sessionStorage.values.set(
    legacySessionCredentialKey,
    "legacy-token-reintroduced-before-forget",
  );
  await dashboard.click("forget-token");
  pendingResponse.resolve(response(200, stats));
  await attempt;

  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Not authenticated. The page-memory API token was forgotten.",
  );
  assert.equal(
    dashboard.elements.get("credential-status").classList.contains("authenticated"),
    false,
  );
  assert.equal(dashboard.sessionStorage.values.has(legacySessionCredentialKey), false);
  assert.equal(dashboard.sessionStorage.removedKeys.at(-1), legacySessionCredentialKey);
  assert.equal(dashboard.sessionStorage.getCount, 0);
  assert.equal(dashboard.sessionStorage.setCount, 0);
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
  assert.equal(
    dashboard.elements.get("output").textContent,
    "Browser API token forgotten for this loaded page.",
  );
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

test("a failed replacement does not restore the previously verified page token", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "previous-dashboard-token";
  await dashboard.click("use-token");

  const pendingResponse = deferred();
  dashboard.fetchResponses.push(pendingResponse.promise);
  dashboard.elements.get("api-token").value = "replacement-dashboard-token";
  const replacement = dashboard.click("use-token");

  pendingResponse.resolve(response(503, { detail: "Unavailable" }));
  await replacement;
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.click("trigger");
  assert.equal(dashboard.fetchCalls.length, fetchCount);
});

test("a verified token is dropped when the dashboard reloads", async () => {
  const initial = loadDashboard();
  initial.fetchResponses.push(response(200, stats));
  initial.elements.get("api-token").value = "reload-dashboard-token";
  await initial.click("use-token");

  const reloaded = loadDashboard();

  assert.equal(reloaded.elements.get("api-token").value, "");
  assert.equal(reloaded.fetchCalls.length, 0);
  assert.equal(
    reloaded.elements.get("credential-status").textContent,
    "Not authenticated. Protected actions will ask for a token.",
  );
  assert.equal(
    reloaded.elements.get("output").textContent,
    "Enter the local demo API token to enable protected actions.",
  );
  assertCredentialNotRendered(reloaded, "reload-dashboard-token");

  await reloaded.click("trigger");
  assert.equal(reloaded.fetchCalls.length, 0);
  assert.equal(reloaded.elements.get("output").textContent, "A browser API token is required.");
});

test("a current 401 clears the credential and protected response", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "expired-dashboard-token";
  await dashboard.click("use-token");

  dashboard.fetchResponses.push(response(200, "sensitive metrics payload"));
  await dashboard.click("view-metrics");
  assert.equal(dashboard.elements.get("protected-response").hidden, false);

  dashboard.sessionStorage.values.set(
    legacySessionCredentialKey,
    "legacy-token-reintroduced-before-401",
  );
  dashboard.fetchResponses.push(response(401, { detail: "Unauthorized" }));
  await dashboard.click("trigger");

  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "The API token was rejected. Paste a valid token and try again.",
  );
  assert.equal(dashboard.elements.get("protected-response").hidden, true);
  assert.equal(dashboard.elements.get("protected-response-title").textContent, "");
  assert.equal(dashboard.elements.get("protected-response-body").textContent, "");
  assert.equal(dashboard.sessionStorage.values.has(legacySessionCredentialKey), false);
  assert.equal(dashboard.sessionStorage.removedKeys.at(-1), legacySessionCredentialKey);
  assert.equal(dashboard.sessionStorage.getCount, 0);
  assert.equal(dashboard.sessionStorage.setCount, 0);
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
    dashboard.elements.get("credential-status").textContent,
    "Authenticated for this loaded page. Reloading clears the token.",
  );
  dashboard.fetchResponses.push(response(200, "metrics after forbidden enqueue"));
  await dashboard.click("view-metrics");
  assert.equal(
    dashboard.fetchCalls.at(-1).options.headers.get("Authorization"),
    "Bearer permission-limited-token",
  );
});

test("the dashboard never persists or routes bearer credentials", () => {
  for (const persistenceOrRoute of [
    "localStorage",
    "indexedDB",
    "document.cookie",
    "window.name",
    "window.location",
    "URLSearchParams",
  ]) {
    assert.equal(dashboardScript.includes(persistenceOrRoute), false);
  }
  assert.equal(dashboardScript.includes('let apiToken = "";'), true);
  assert.equal(dashboardScript.includes("restoreCredential"), false);
  assert.equal(
    dashboardScript.includes(
      "window.sessionStorage.removeItem(legacySessionCredentialKey)",
    ),
    true,
  );
  assert.equal(dashboardScript.includes("window.sessionStorage.getItem"), false);
  assert.equal(dashboardScript.includes("window.sessionStorage.setItem"), false);
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
});
