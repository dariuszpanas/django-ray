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
  let reject;
  const promise = new Promise((complete, fail) => {
    resolve = complete;
    reject = fail;
  });
  return { promise, resolve, reject };
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

  assert.equal(
    dashboard.elements.get("output").textContent,
    "Task task-1 enqueued with status READY.",
  );

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
  dashboard.fetchResponses.push(oldResponse.promise, response(200, { ...stats, total: 2 }));

  dashboard.elements.get("api-token").value = "old-success-token";
  const oldAttempt = dashboard.click("use-token");
  dashboard.elements.get("api-token").value = "new-success-token";
  await dashboard.click("use-token");

  oldResponse.resolve(response(200, { ...stats, total: 99 }));
  await oldAttempt;
  assert.equal(dashboard.elements.get("stat-total").textContent, 2);
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
  assert.equal(dashboard.elements.get("stat-total").textContent, "");
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.click("trigger");
  assert.equal(dashboard.fetchCalls.length, fetchCount);
});

test("credential changes while reading statistics fence the decoded body", async () => {
  const dashboard = loadDashboard();
  const oldBody = deferred();
  const bodyStarted = deferred();
  dashboard.fetchResponses.push({
    ...response(200, stats),
    json() {
      bodyStarted.resolve();
      return oldBody.promise;
    },
  });
  dashboard.elements.get("api-token").value = "old-body-token";
  const oldAttempt = dashboard.click("use-token");
  await bodyStarted.promise;

  dashboard.fetchResponses.push(response(200, { ...stats, total: 2 }));
  dashboard.elements.get("api-token").value = "new-body-token";
  await dashboard.click("use-token");
  oldBody.resolve({ ...stats, total: 99 });
  await oldAttempt;

  assert.equal(dashboard.elements.get("stat-total").textContent, 2);
});

test("a queued post-enqueue refresh does not use a replacement credential", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "enqueue-refresh-token";
  await dashboard.click("use-token");
  dashboard.fetchResponses.push(response(200, { task_id: "old-task", status: "READY" }));
  await dashboard.click("trigger");

  dashboard.fetchResponses.push(response(200, { ...stats, total: 2 }));
  dashboard.elements.get("api-token").value = "replacement-refresh-token";
  await dashboard.click("use-token");
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.scheduled.shift()();

  assert.equal(dashboard.fetchCalls.length, fetchCount);
  assert.equal(dashboard.elements.get("stat-total").textContent, 2);
});

test("a queued post-enqueue refresh cannot overwrite the forget message", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "forgotten-refresh-token";
  await dashboard.click("use-token");
  dashboard.fetchResponses.push(response(200, { task_id: "old-task", status: "READY" }));
  await dashboard.click("trigger");
  await dashboard.click("forget-token");
  const fetchCount = dashboard.fetchCalls.length;
  await dashboard.scheduled.shift()();

  assert.equal(dashboard.fetchCalls.length, fetchCount);
  assert.equal(
    dashboard.elements.get("output").textContent,
    "Browser API token forgotten for this loaded page.",
  );
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "Not authenticated. The page-memory API token was forgotten.",
  );
});

test("an in-flight post-enqueue refresh failure cannot overwrite a newer check", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "old-refresh-failure-token";
  await dashboard.click("use-token");
  dashboard.fetchResponses.push(response(200, { task_id: "old-task", status: "READY" }));
  await dashboard.click("trigger");
  const oldResponse = deferred();
  dashboard.fetchResponses.push(oldResponse.promise);
  const refresh = dashboard.scheduled.shift()();

  dashboard.fetchResponses.push(response(200, { ...stats, total: 2 }));
  dashboard.elements.get("api-token").value = "new-refresh-failure-token";
  await dashboard.click("use-token");
  oldResponse.resolve(response(503, {}));
  await refresh;

  assert.equal(dashboard.elements.get("stat-total").textContent, 2);
  assert.equal(
    dashboard.elements.get("output").textContent,
    "Authenticated task statistics refreshed.",
  );
});

test("a current post-enqueue refresh still reports credential rejection", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "rejected-refresh-token";
  await dashboard.click("use-token");
  dashboard.fetchResponses.push(response(200, { task_id: "current-task", status: "READY" }));
  await dashboard.click("trigger");
  dashboard.fetchResponses.push(response(401, {}));
  await dashboard.scheduled.shift()();

  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "The API token was rejected. Paste a valid token and try again.",
  );
  assert.equal(
    dashboard.elements.get("output").textContent,
    "The browser API token was rejected.",
  );
});

for (const action of ["trigger", "view-executions"]) {
  for (const credentialChange of ["replace", "forget"]) {
    for (const failure of ["401", "503", "network", "body"]) {
      test(`stale ${action} ${failure} failure preserves ${credentialChange} feedback`, async () => {
        const dashboard = loadDashboard();
        dashboard.fetchResponses.push(response(200, stats));
        dashboard.elements.get("api-token").value = "old-action-token";
        await dashboard.click("use-token");
        const oldResponse = deferred();
        const oldBody = deferred();
        const bodyStarted = deferred();
        dashboard.fetchResponses.push(oldResponse.promise);
        const oldAction = dashboard.click(action);

        if (failure === "body") {
          oldResponse.resolve({
            ...response(200, {}),
            json() {
              bodyStarted.resolve();
              return oldBody.promise;
            },
          });
          await bodyStarted.promise;
        }

        if (credentialChange === "replace") {
          dashboard.fetchResponses.push(response(200, { ...stats, total: 2 }));
          dashboard.elements.get("api-token").value = "new-action-token";
          await dashboard.click("use-token");
        } else {
          await dashboard.click("forget-token");
        }
        const expectedOutput = dashboard.elements.get("output").textContent;
        const expectedCredential = dashboard.elements.get("credential-status").textContent;

        if (failure === "network") {
          oldResponse.reject(new Error("old request network failure"));
        } else if (failure === "body") {
          oldBody.reject(new Error("old response body failure"));
        } else {
          oldResponse.resolve(response(Number(failure), {}));
        }
        await oldAction;

        assert.equal(dashboard.elements.get("output").textContent, expectedOutput);
        assert.equal(
          dashboard.elements.get("credential-status").textContent,
          expectedCredential,
        );
        assert.equal(dashboard.elements.get("protected-response").hidden, true);
      });
    }
  }
}

test("a current protected-response rejection still clears the credential", async () => {
  const dashboard = loadDashboard();
  dashboard.fetchResponses.push(response(200, stats));
  dashboard.elements.get("api-token").value = "current-protected-token";
  await dashboard.click("use-token");
  dashboard.fetchResponses.push(response(401, {}));
  await dashboard.click("view-executions");

  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "The API token was rejected. Paste a valid token and try again.",
  );
  assert.equal(
    dashboard.elements.get("output").textContent,
    "The browser API token was rejected.",
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

  assert.equal(dashboard.elements.get("output").textContent, "Enqueue failed: 403");

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

for (const pendingPhase of ["headers", "body"]) {
  test(`closing the protected panel fences pending ${pendingPhase}`, async () => {
    const dashboard = loadDashboard();
    dashboard.elements.get("api-token").value = "same-page-token";
    dashboard.fetchResponses.push(response(200, stats));
    await dashboard.click("use-token");
    let scrolls = 0;
    dashboard.elements.get("protected-response").scrollIntoView = () => {
      scrolls += 1;
    };
    dashboard.fetchResponses.push(response(200, "initial metrics"));
    await dashboard.click("view-metrics");
    assert.equal(dashboard.elements.get("protected-response").hidden, false);
    assert.equal(scrolls, 1);

    const pending = deferred();
    const bodyStarted = deferred();
    if (pendingPhase === "headers") {
      dashboard.fetchResponses.push(pending.promise);
    } else {
      dashboard.fetchResponses.push({
        ...response(200, {}),
        json() {
          bodyStarted.resolve();
          return pending.promise;
        },
      });
    }
    const action = dashboard.click("view-executions");
    if (pendingPhase === "body") await bodyStarted.promise;
    assert.equal(dashboard.elements.get("view-executions").disabled, true);
    await dashboard.click("close-protected-response");
    const closeFeedback = dashboard.elements.get("output").textContent;
    pending.resolve(pendingPhase === "headers" ? response(200, { tasks: [] }) : { tasks: [] });
    await action;

    assert.equal(dashboard.elements.get("protected-response").hidden, true);
    assert.equal(dashboard.elements.get("protected-response-title").textContent, "");
    assert.equal(dashboard.elements.get("protected-response-body").textContent, "");
    assert.equal(dashboard.elements.get("output").textContent, closeFeedback);
    assert.equal(closeFeedback, "Protected response closed.");
    assert.equal(scrolls, 1);
    assert.equal(dashboard.elements.get("view-executions").disabled, false);

    dashboard.fetchResponses.push(response(200, { tasks: ["fresh explicit action"] }));
    await dashboard.click("view-executions");
    assert.equal(dashboard.elements.get("protected-response").hidden, false);
    assert.equal(dashboard.elements.get("protected-response-title").textContent, "Executions");
    assert.match(
      dashboard.elements.get("protected-response-body").textContent,
      /fresh explicit action/,
    );
    assert.equal(scrolls, 2);
  });

  test(`the latest protected selection wins over pending ${pendingPhase}`, async () => {
    const dashboard = loadDashboard();
    dashboard.elements.get("api-token").value = "same-page-token";
    dashboard.fetchResponses.push(response(200, stats));
    await dashboard.click("use-token");
    let scrolls = 0;
    dashboard.elements.get("protected-response").scrollIntoView = () => {
      scrolls += 1;
    };
    const pending = deferred();
    const bodyStarted = deferred();
    if (pendingPhase === "headers") {
      dashboard.fetchResponses.push(pending.promise);
    } else {
      dashboard.fetchResponses.push({
        ...response(200, ""),
        text() {
          bodyStarted.resolve();
          return pending.promise;
        },
      });
    }
    const oldAction = dashboard.click("view-metrics");
    if (pendingPhase === "body") await bodyStarted.promise;
    dashboard.fetchResponses.push(response(200, { tasks: ["latest selection"] }));
    await dashboard.click("view-executions");
    const expectedBody = dashboard.elements.get("protected-response-body").textContent;
    const expectedOutput = dashboard.elements.get("output").textContent;
    pending.resolve(pendingPhase === "headers" ? response(200, "older metrics") : "older metrics");
    await oldAction;

    assert.equal(dashboard.elements.get("protected-response").hidden, false);
    assert.equal(dashboard.elements.get("protected-response-title").textContent, "Executions");
    assert.equal(dashboard.elements.get("protected-response-body").textContent, expectedBody);
    assert.equal(dashboard.elements.get("output").textContent, expectedOutput);
    assert.equal(scrolls, 1);
    assert.equal(dashboard.elements.get("view-metrics").disabled, false);
  });
}

for (const failure of ["network", "body", "403"]) {
  test(`an older protected ${failure} error preserves the latest selection`, async () => {
    const dashboard = loadDashboard();
    dashboard.elements.get("api-token").value = "same-page-token";
    dashboard.fetchResponses.push(response(200, stats));
    await dashboard.click("use-token");
    const pending = deferred();
    const bodyStarted = deferred();
    if (failure === "body") {
      dashboard.fetchResponses.push({
        ...response(200, ""),
        text() {
          bodyStarted.resolve();
          return pending.promise;
        },
      });
    } else {
      dashboard.fetchResponses.push(pending.promise);
    }
    const oldAction = dashboard.click("view-metrics");
    if (failure === "body") await bodyStarted.promise;
    dashboard.fetchResponses.push(response(200, { tasks: ["latest selection"] }));
    await dashboard.click("view-executions");
    const expectedBody = dashboard.elements.get("protected-response-body").textContent;
    const expectedOutput = dashboard.elements.get("output").textContent;
    if (failure === "403") pending.resolve(response(403, {}));
    else pending.reject(new Error("older protected request failed"));
    await oldAction;

    assert.equal(dashboard.elements.get("protected-response").hidden, false);
    assert.equal(dashboard.elements.get("protected-response-title").textContent, "Executions");
    assert.equal(dashboard.elements.get("protected-response-body").textContent, expectedBody);
    assert.equal(dashboard.elements.get("output").textContent, expectedOutput);
    assert.equal(
      dashboard.elements.get("credential-status").textContent,
      "Authenticated for this loaded page. Reloading clears the token.",
    );
    assert.equal(dashboard.elements.get("view-metrics").disabled, false);
  });
}

test("a superseded protected 401 still rejects the current credential", async () => {
  const dashboard = loadDashboard();
  dashboard.elements.get("api-token").value = "same-page-token";
  dashboard.fetchResponses.push(response(200, stats));
  await dashboard.click("use-token");
  const pending = deferred();
  dashboard.fetchResponses.push(pending.promise);
  const oldAction = dashboard.click("view-metrics");
  dashboard.fetchResponses.push(response(200, { tasks: ["latest selection"] }));
  await dashboard.click("view-executions");
  pending.resolve(response(401, {}));
  await oldAction;

  assert.equal(dashboard.elements.get("protected-response").hidden, true);
  assert.equal(dashboard.elements.get("protected-response-title").textContent, "");
  assert.equal(dashboard.elements.get("protected-response-body").textContent, "");
  assert.equal(
    dashboard.elements.get("credential-status").textContent,
    "The API token was rejected. Paste a valid token and try again.",
  );
  assert.equal(
    dashboard.elements.get("output").textContent,
    "The browser API token was rejected.",
  );
  assert.equal(dashboard.elements.get("view-metrics").disabled, false);
});

for (const pendingPhase of ["headers", "body"]) {
  test(`newer same-credential statistics win over pending ${pendingPhase}`, async () => {
    const dashboard = loadDashboard();
    dashboard.fetchResponses.push(response(200, stats));
    dashboard.elements.get("api-token").value = "same-credential-stats-token";
    await dashboard.click("use-token");

    dashboard.fetchResponses.push(response(200, { task_id: "task-a", status: "READY" }));
    await dashboard.click("trigger");
    assert.equal(dashboard.elements.get("trigger").disabled, false);

    // R1 captures task A before task B exists, but its delivery stays pending.
    const olderStats = { total: 10, queued: 1, running: 2, succeeded: 3, failed: 1, expired: 3 };
    const olderResponse = deferred();
    const olderBodyStarted = deferred();
    if (pendingPhase === "headers") {
      dashboard.fetchResponses.push(olderResponse.promise);
    } else {
      dashboard.fetchResponses.push({
        ...response(200, olderStats),
        json() {
          olderBodyStarted.resolve();
          return olderResponse.promise;
        },
      });
    }
    const olderRefresh = dashboard.scheduled.shift()();
    if (pendingPhase === "body") await olderBodyStarted.promise;
    assert.equal(dashboard.fetchCalls.at(-1).url, "/api/executions/stats");

    // The first POST has completed, so the actual button allows task B.
    assert.equal(dashboard.elements.get("trigger").disabled, false);
    dashboard.fetchResponses.push(response(200, { task_id: "task-b", status: "READY" }));
    await dashboard.click("trigger");
    assert.equal(dashboard.elements.get("trigger").disabled, false);
    const newerStats = { total: 11, queued: 2, running: 1, succeeded: 4, failed: 2, expired: 2 };
    const newerResponse = deferred();
    dashboard.fetchResponses.push(newerResponse.promise);
    const newerRefresh = dashboard.scheduled.shift()();
    assert.equal(dashboard.fetchCalls.at(-1).url, "/api/executions/stats");
    newerResponse.resolve(response(200, newerStats));
    await newerRefresh;
    for (const [key, value] of Object.entries(newerStats)) {
      assert.equal(dashboard.elements.get(`stat-${key}`).textContent, value);
    }

    olderResponse.resolve(
      pendingPhase === "headers" ? response(200, olderStats) : olderStats,
    );
    await olderRefresh;
    for (const [key, value] of Object.entries(newerStats)) {
      assert.equal(dashboard.elements.get(`stat-${key}`).textContent, value);
    }
    assert.equal(dashboard.fetchCalls.length, 5);
    for (const call of dashboard.fetchCalls) {
      assert.equal(
        call.options.headers.get("Authorization"),
        "Bearer same-credential-stats-token",
      );
    }
    assert.equal(
      dashboard.elements.get("credential-status").textContent,
      "Authenticated for this loaded page. Reloading clears the token.",
    );
  });
}
