import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const diagnosticsScript = fs.readFileSync(
  fileURLToPath(
    new URL(
      "../../src/django_ray/static/django_ray/admin/workflow_diagnostics.js",
      import.meta.url,
    ),
  ),
  "utf8",
);

class FakeElement {
  constructor(tagName = "div") {
    this.attributes = new Map();
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.disabled = false;
    this.hidden = false;
    this.href = "";
    this.listeners = new Map();
    this.open = false;
    this.parentNode = null;
    this.tagName = tagName.toUpperCase();
    this.type = "";
    this._textContent = "";
  }

  get textContent() {
    return (
      this._textContent +
      this.children.map((child) => child.textContent).join("")
    );
  }

  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  addEventListener(name, callback) {
    const listeners = this.listeners.get(name) ?? [];
    listeners.push(callback);
    this.listeners.set(name, listeners);
  }

  append(...children) {
    for (const child of children) {
      assert.ok(child instanceof FakeElement);
      child.parentNode = this;
      this.children.push(child);
    }
  }

  async dispatch(name) {
    const listeners = this.listeners.get(name) ?? [];
    await Promise.all(listeners.map((listener) => listener({ target: this })));
  }

  querySelector(selector) {
    return findAll(this, (element) => matches(element, selector))[0] ?? null;
  }

  replaceChildren(...children) {
    this.children = [];
    this._textContent = "";
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
}

function matches(element, selector) {
  if (selector.startsWith("[data-") && selector.endsWith("]")) {
    const key = selector
      .slice(6, -1)
      .replaceAll(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
    return Object.hasOwn(element.dataset, key);
  }
  return element.tagName === selector.toUpperCase();
}

function findAll(root, predicate) {
  const matchesFound = [];
  for (const child of root.children) {
    if (predicate(child)) {
      matchesFound.push(child);
    }
    matchesFound.push(...findAll(child, predicate));
  }
  return matchesFound;
}

class FakeHeaders {
  constructor(contentType) {
    this.contentType = contentType;
  }

  get(name) {
    return name.toLowerCase() === "content-type" ? this.contentType : null;
  }
}

function response(
  status,
  payload,
  { contentType = "application/json", redirected = false } = {},
) {
  return {
    headers: new FakeHeaders(contentType),
    ok: status >= 200 && status < 300,
    redirected,
    status,
    async json() {
      return payload;
    },
  };
}

function validPayload(overrides = {}) {
  return {
    schema: "django-ray.admin-workflow-diagnostics",
    schema_version: 1,
    ...overrides,
    plan: {
      status: "AVAILABLE",
      definition_name: "billing.pipeline",
      definition_revision: 7,
      topology_class: "fan_out",
      declared_node_count: 12,
      retry_safe: true,
      fingerprint: "sha256:full-workflow-fingerprint",
      fingerprint_compact: "sha256:full\u2026print",
      requested_policy: "auto",
      selected_strategy: "dynamic_tasks",
      reporting_policy: "full",
      eligible_strategies: ["dynamic_tasks", "local"],
      rejection_counts: {
        INCOMPATIBLE_PLATFORM: 2,
        OWNER_LIFETIME_MISMATCH: 1,
      },
      retained_rejections: 2,
      total_rejections: 4,
      unretained_rejections: 2,
      ...overrides.plan,
    },
    progress: {
      state: "RUNNING",
      message: "Bounded topology is available.",
      availability: "AVAILABLE",
      complete: true,
      truncation_reasons: [],
      actions: {
        topology_nodes: true,
        topology_edges: false,
        node_details: true,
      },
      ...overrides.progress,
    },
  };
}

function loadDiagnostics({
  fetchResponses = [],
  clipboardFailure = null,
  dataset = {},
} = {}) {
  const details = new FakeElement("details");
  details.dataset = {
    diagnosticsUrl: "/admin/execution/1/workflow/diagnostics/",
    planDownloadUrl: "/admin/execution/1/workflow/plan/",
    selectionDownloadUrl: "/admin/execution/1/workflow/selection/",
    topologyNodesUrl: "/admin/execution/1/workflow/topology/nodes/",
    topologyEdgesUrl: "/admin/execution/1/workflow/topology/edges/",
    nodeDetailsUrl: "/admin/execution/1/workflow/nodes/",
    ...dataset,
  };
  const status = new FakeElement("p");
  status.dataset.workflowDiagnosticsStatus = "";
  status.textContent = "Open this section to load workflow diagnostics.";
  const content = new FakeElement("div");
  content.dataset.workflowDiagnosticsContent = "";
  content.hidden = true;
  details.append(status, content);

  const fetchCalls = [];
  const queuedResponses = [...fetchResponses];
  const copied = [];
  const document = {
    createElement(tagName) {
      return new FakeElement(tagName);
    },
    getElementById(id) {
      return id === "django-ray-workflow-diagnostics" ? details : null;
    },
  };
  const window = {
    async fetch(url, options) {
      fetchCalls.push({ options, url });
      assert.notEqual(queuedResponses.length, 0, `unexpected fetch for ${url}`);
      const next = queuedResponses.shift();
      if (next instanceof Error) {
        throw next;
      }
      return next;
    },
    navigator: {
      clipboard: {
        async writeText(value) {
          if (clipboardFailure) {
            throw clipboardFailure;
          }
          copied.push(value);
        },
      },
    },
  };
  const context = vm.createContext({ document, window });
  vm.runInContext(diagnosticsScript, context, {
    filename: "workflow_diagnostics.js",
  });

  return {
    content,
    copied,
    details,
    fetchCalls,
    queuedResponses,
    status,
    async toggle(open) {
      details.open = open;
      await details.dispatch("toggle");
      await settle();
    },
  };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function links(app) {
  return findAll(app.content, (element) => element.tagName === "A");
}

function linkLabels(app) {
  return links(app).map((link) => link.textContent);
}

test("the closed disclosure lazily renders safe, capability-gated actions", async () => {
  const maliciousDefinition = "<img src=x onerror=steal-secret()>";
  const app = loadDiagnostics({
    fetchResponses: [
      response(
        200,
        validPayload({
          plan: {
            definition_name: maliciousDefinition,
            eligible_strategies: ["dynamic_tasks", "<script>steal()</script>"],
          },
          progress: {
            message: "<svg onload=steal-secret()> is plain diagnostic text",
          },
        }),
      ),
    ],
  });

  assert.equal(app.details.open, false);
  assert.equal(app.fetchCalls.length, 0);
  assert.equal(app.content.hidden, true);

  await app.toggle(true);

  assert.equal(app.fetchCalls.length, 1);
  assert.equal(
    app.fetchCalls[0].url,
    "/admin/execution/1/workflow/diagnostics/",
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(app.fetchCalls[0].options)),
    {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    },
  );
  assert.equal(app.content.hidden, false);
  assert.equal(app.status.textContent, "Workflow diagnostics loaded.");
  assert.equal(app.content.textContent.includes(maliciousDefinition), true);
  assert.equal(
    app.content.textContent.includes(
      "<svg onload=steal-secret()> is plain diagnostic text",
    ),
    true,
  );
  assert.equal(
    findAll(app.content, (element) => element.tagName === "IMG").length,
    0,
  );
  assert.equal(
    findAll(app.content, (element) => element.tagName === "SCRIPT").length,
    0,
  );
  assert.match(app.content.textContent, /4 strategy rejections/);
  assert.match(app.content.textContent, /incompatible platform: 2/i);
  assert.equal(
    app.content.textContent.includes("sha256:full-workflow-fingerprint"),
    false,
  );
  assert.deepEqual(linkLabels(app), [
    "Download plan JSON",
    "Download selection JSON",
    "Topology nodes",
    "Node details",
  ]);
  assert.equal(
    links(app).some(
      (link) =>
        link.href === "/admin/execution/1/workflow/topology/edges/",
    ),
    false,
  );

  const copyButton = findAll(
    app.content,
    (element) =>
      element.tagName === "BUTTON" &&
      element.textContent === "Copy full fingerprint",
  )[0];
  assert.ok(copyButton);
  await copyButton.dispatch("click");
  assert.deepEqual(app.copied, ["sha256:full-workflow-fingerprint"]);
  assert.equal(app.status.textContent, "Full plan fingerprint copied.");
  assert.equal(copyButton.disabled, false);

  await app.toggle(false);
  await app.toggle(true);
  assert.equal(app.fetchCalls.length, 1);
});

test("unavailable plans and progress do not expose misleading actions", async () => {
  const app = loadDiagnostics({
    fetchResponses: [
      response(
        200,
        validPayload({
          plan: {
            status: "CORRUPT",
          },
          progress: {
            availability: "MISSING",
            complete: false,
            message:
              "Full reporting was requested but no usable snapshot was retained.",
            actions: {
              topology_nodes: false,
              topology_edges: false,
              node_details: false,
            },
          },
        }),
      ),
    ],
  });

  await app.toggle(true);

  assert.match(app.content.textContent, /failed verification/);
  assert.match(app.content.textContent, /no usable snapshot was retained/);
  assert.match(app.content.textContent, /No bounded topology actions/);
  assert.deepEqual(linkLabels(app), []);
  assert.equal(
    app.content.textContent.includes("billing.pipeline"),
    false,
  );
  assert.equal(
    findAll(app.content, (element) => element.tagName === "BUTTON").length,
    0,
  );
});

test("a redacted fingerprint placeholder is never offered to the clipboard", async () => {
  const app = loadDiagnostics({
    fetchResponses: [
      response(
        200,
        validPayload({
          plan: {
            fingerprint: "[REDACTED]",
            fingerprint_compact: "[REDACTED]",
          },
        }),
      ),
    ],
  });

  await app.toggle(true);

  const copyButtons = findAll(
    app.content,
    (element) =>
      element.tagName === "BUTTON" &&
      element.textContent === "Copy full fingerprint",
  );
  assert.deepEqual(copyButtons, []);
  assert.deepEqual(app.copied, []);
  assert.match(app.content.textContent, /\[REDACTED\]/);
});

test("an execution without a recorded plan has a concise empty state", async () => {
  const app = loadDiagnostics({
    fetchResponses: [
      response(
        200,
        validPayload({
          plan: {
            status: "NOT_RECORDED",
          },
          progress: {
            state: "REQUESTED_NOT_REPORTED",
            availability: "NOT_REPORTED",
            message: "",
            actions: {
              topology_nodes: false,
              topology_edges: false,
              node_details: false,
            },
          },
        }),
      ),
    ],
  });

  await app.toggle(true);

  assert.match(app.content.textContent, /No workflow plan was recorded/);
  assert.match(app.content.textContent, /no snapshot has been reported yet/i);
  assert.deepEqual(linkLabels(app), []);
});

test("authentication responses fail closed without parsing or rendering content", async () => {
  const secret = "server-secret-that-must-not-render";
  const app = loadDiagnostics({
    fetchResponses: [response(401, { detail: secret })],
  });

  await app.toggle(true);

  assert.equal(
    app.status.textContent,
    "Workflow diagnostics unavailable; reload after signing in again.",
  );
  assert.equal(app.status.dataset.state, "error");
  assert.equal(app.status.textContent.includes(secret), false);
  assert.equal(app.content.hidden, true);
  assert.equal(app.fetchCalls.length, 1);

  await app.toggle(false);
  await app.toggle(true);
  assert.equal(app.fetchCalls.length, 1);
});

test("redirected HTML and network failures are contained", async (context) => {
  const redirected = loadDiagnostics({
    fetchResponses: [
      response(200, "login-secret", {
        contentType: "text/html",
        redirected: true,
      }),
    ],
  });
  await redirected.toggle(true);
  assert.equal(
    redirected.status.textContent,
    "Workflow diagnostics unavailable; reload after signing in again.",
  );
  assert.equal(redirected.content.hidden, true);
  await redirected.toggle(false);
  await redirected.toggle(true);
  assert.equal(redirected.fetchCalls.length, 1);

  await context.test("network details never reach the page", async () => {
    const failed = loadDiagnostics({
      fetchResponses: [
        new Error("network failed while carrying private-runtime-secret"),
      ],
    });
    await failed.toggle(true);
    assert.equal(
      failed.status.textContent,
      "Workflow diagnostics could not be displayed safely.",
    );
    assert.equal(
      failed.status.textContent.includes("private-runtime-secret"),
      false,
    );
    assert.equal(failed.content.hidden, true);
  });

  await context.test("server error payloads are not rendered", async () => {
    const unavailable = loadDiagnostics({
      fetchResponses: [
        response(
          503,
          { detail: "database error included private-runtime-secret" },
          { contentType: "text/html" },
        ),
      ],
    });
    await unavailable.toggle(true);
    assert.equal(
      unavailable.status.textContent,
      "Workflow diagnostics are temporarily unavailable.",
    );
    assert.equal(
      unavailable.status.textContent.includes("private-runtime-secret"),
      false,
    );
    assert.equal(unavailable.content.hidden, true);
  });

  await context.test("malformed JSON shapes fail closed", async () => {
    const malformed = loadDiagnostics({
      fetchResponses: [
        response(200, {
          plan: { status: "AVAILABLE", fingerprint: "private-fingerprint" },
          progress: null,
        }),
      ],
    });
    await malformed.toggle(true);
    assert.equal(
      malformed.status.textContent,
      "Workflow diagnostics could not be displayed safely.",
    );
    assert.equal(malformed.status.textContent.includes("private-fingerprint"), false);
    assert.equal(malformed.content.hidden, true);
  });
});

test("recoverable failures allow exactly one fresh request after reopening", async (context) => {
  const absentVersion = validPayload();
  delete absentVersion.schema_version;
  const cases = [
    {
      name: "network failure",
      firstResponse: new Error("private network failure"),
      expectedStatus: "Workflow diagnostics could not be displayed safely.",
    },
    {
      name: "server failure",
      firstResponse: response(
        503,
        { detail: "private server failure" },
        { contentType: "text/html" },
      ),
      expectedStatus: "Workflow diagnostics are temporarily unavailable.",
    },
    {
      name: "malformed payload",
      firstResponse: response(200, {
        plan: { status: "AVAILABLE", fingerprint: "private-fingerprint" },
        progress: null,
      }),
      expectedStatus: "Workflow diagnostics could not be displayed safely.",
    },
    {
      name: "wrong envelope schema",
      firstResponse: response(
        200,
        validPayload({ schema: "django-ray.untrusted-diagnostics" }),
      ),
      expectedStatus: "Workflow diagnostics could not be displayed safely.",
    },
    {
      name: "wrong envelope version",
      firstResponse: response(200, validPayload({ schema_version: 2 })),
      expectedStatus: "Workflow diagnostics could not be displayed safely.",
    },
    {
      name: "absent envelope version",
      firstResponse: response(200, absentVersion),
      expectedStatus: "Workflow diagnostics could not be displayed safely.",
    },
  ];

  for (const { name, firstResponse, expectedStatus } of cases) {
    await context.test(name, async () => {
      const app = loadDiagnostics({
        fetchResponses: [firstResponse, response(200, validPayload())],
      });

      await app.toggle(true);
      assert.equal(app.fetchCalls.length, 1);
      assert.equal(app.status.textContent, expectedStatus);
      assert.equal(app.content.hidden, true);

      await app.toggle(false);
      assert.equal(app.fetchCalls.length, 1);
      await app.toggle(true);

      assert.equal(app.fetchCalls.length, 2);
      assert.equal(app.status.textContent, "Workflow diagnostics loaded.");
      assert.equal(app.content.hidden, false);

      await app.toggle(false);
      await app.toggle(true);
      assert.equal(app.fetchCalls.length, 2);
    });
  }
});

test("clipboard failures are announced without exposing the fingerprint", async () => {
  const app = loadDiagnostics({
    clipboardFailure: new Error("clipboard included private-fingerprint"),
    fetchResponses: [response(200, validPayload())],
  });
  await app.toggle(true);
  const copyButton = findAll(
    app.content,
    (element) => element.tagName === "BUTTON",
  )[0];

  await copyButton.dispatch("click");

  assert.equal(app.status.textContent, "The fingerprint could not be copied.");
  assert.equal(
    app.status.textContent.includes("sha256:full-workflow-fingerprint"),
    false,
  );
  assert.equal(copyButton.disabled, false);
});
