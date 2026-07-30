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
const taskLiveScript = fs.readFileSync(
  fileURLToPath(
    new URL(
      "../../src/django_ray/static/django_ray/admin/task_live.js",
      import.meta.url,
    ),
  ),
  "utf8",
);
const graphStyles = fs.readFileSync(
  fileURLToPath(
    new URL(
      "../../src/django_ray/static/django_ray/admin/task_live.css",
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

  remove() {
    if (this.parentNode === null) {
      return;
    }
    this.parentNode.children = this.parentNode.children.filter(
      (child) => child !== this,
    );
    this.parentNode = null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "class") {
      this.className = String(value);
    }
  }
}

function matches(element, selector) {
  const dataSelector = selector.match(
    /^\[data-([a-z-]+)(?:="([^"]*)")?\]$/,
  );
  if (dataSelector !== null) {
    const key = dataSelector[1]
      .replaceAll(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
    return (
      Object.hasOwn(element.dataset, key) &&
      (dataSelector[2] === undefined ||
        String(element.dataset[key]) === dataSelector[2])
    );
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

function validGraphPayload(overrides = {}) {
  const nodes = overrides.nodes ?? [
    {
      id: "prepare",
      label: "Prepare input",
      kind: "task",
      state: "SUCCEEDED",
      message: "Input is ready.",
      error: null,
      failure_path: false,
    },
    {
      id: "fanout",
      label: "Process items",
      kind: "map",
      state: "RUNNING",
      message: "Two items are still running.",
      error: null,
      failure_path: false,
      fanout: {
        submitted_items: 8,
        completed_items: 6,
        in_flight_items: 2,
        input_exhausted: true,
      },
    },
    {
      id: "finish",
      label: "Finalize result",
      kind: "task",
      state: "PENDING",
      message: null,
      error: null,
      failure_path: false,
    },
  ];
  const edges = overrides.edges ?? [
    { source: "prepare", target: "fanout" },
    { source: "fanout", target: "finish" },
  ];
  return {
    schema: "django-ray.admin-workflow-graph",
    schema_version: 1,
    status: "AVAILABLE",
    message: "Bounded workflow graph loaded.",
    complete: true,
    counts: {
      nodes: nodes.length,
      edges: edges.length,
    },
    limits: {
      nodes: 100,
      edges: 256,
      details: 100,
      response_bytes: 131072,
    },
    nodes,
    edges,
    ...overrides,
  };
}

function degradedGraphPayload(status, message) {
  return validGraphPayload({
    status,
    message,
    complete: false,
    counts: { nodes: 0, edges: 0 },
    nodes: [],
    edges: [],
  });
}

function loadDiagnostics({
  fetchResponses = [],
  clipboardFailure = null,
  dataset = {},
} = {}) {
  const details = new FakeElement("details");
  details.dataset = {
    diagnosticsUrl: "/admin/execution/1/workflow/diagnostics/",
    graphUrl: "/admin/execution/1/workflow/graph/",
    pinnedAttemptNumber: "2",
    planDownloadUrl: "/admin/execution/1/workflow/plan/",
    selectionDownloadUrl: "/admin/execution/1/workflow/selection/",
    topologyNodesUrl: "/admin/execution/1/workflow/topology/nodes/",
    topologyEdgesUrl: "/admin/execution/1/workflow/topology/edges/",
    nodeDetailsUrl: "/admin/execution/1/workflow/nodes/",
    nodeDetailUrl: "/admin/execution/1/workflow/node/",
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
    createElementNS(_namespace, tagName) {
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
  const context = vm.createContext({
    document,
    TextEncoder,
    URLSearchParams,
    window,
  });
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
    async toggleGraph(open) {
      const graph = findAll(
        content,
        (candidate) =>
          candidate.tagName === "DETAILS" &&
          candidate.className === "django-ray-workflow-graph",
      )[0];
      assert.ok(graph, "expected the nested workflow graph disclosure");
      graph.open = open;
      await graph.dispatch("toggle");
      await settle();
      return graph;
    },
  };
}

function loadTaskLive(payload) {
  const panel = new FakeElement("section");
  panel.dataset.observabilityUrl = "/admin/execution/1/observability/";
  const fields = Object.fromEntries(
    ["state", "attempt", "workflow", "status"].map((name) => {
      const field = new FakeElement("span");
      field.dataset.field = name;
      panel.append(field);
      return [name, field];
    }),
  );
  const fetchCalls = [];
  const document = {
    hidden: false,
    addEventListener() {},
    getElementById(id) {
      return id === "django-ray-live-observability" ? panel : null;
    },
  };
  const window = {
    async fetch(url, options) {
      fetchCalls.push({ options, url });
      return response(200, payload);
    },
    setTimeout() {
      throw new Error("terminal task live status must not schedule another poll");
    },
  };
  const context = vm.createContext({ document, window });
  vm.runInContext(taskLiveScript, context, { filename: "task_live.js" });
  return { fetchCalls, fields, panel };
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

function elementsByClass(app, className) {
  return findAll(
    app.content,
    (element) =>
      element.className.split(" ").filter(Boolean).includes(className),
  );
}

function graphNodeLinks(app) {
  return elementsByClass(app, "django-ray-workflow-graph__node");
}

function graphStatus(app) {
  return elementsByClass(app, "django-ray-workflow-graph__status")[0];
}

function graphContent(app) {
  return elementsByClass(app, "django-ray-workflow-graph__content")[0];
}

function graphFallbacks(app) {
  return elementsByClass(app, "django-ray-workflow-graph__fallbacks");
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

test("bounded JSON actions remain available without a graph endpoint", async () => {
  const app = loadDiagnostics({
    dataset: { graphUrl: "" },
    fetchResponses: [response(200, validPayload())],
  });

  await app.toggle(true);

  assert.deepEqual(linkLabels(app), [
    "Download plan JSON",
    "Download selection JSON",
    "Topology nodes",
    "Node details",
  ]);
  assert.equal(
    elementsByClass(app, "django-ray-workflow-graph").length,
    0,
  );
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
  assert.doesNotMatch(app.content.textContent, /Execution graph/);
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

test("terminal-only summaries never advertise or request workflow detail", async () => {
  const app = loadDiagnostics({
    fetchResponses: [
      response(
        200,
        validPayload({
          plan: {
            reporting_policy: "terminal_only",
          },
          progress: {
            state: "TERMINAL_ONLY",
            workflow_state: "SUCCEEDED",
            availability: "OMITTED_BY_POLICY",
            complete: false,
            message:
              "A terminal workflow summary is available; topology and node detail were omitted by the terminal-only reporting policy.",
            actions: {
              topology_nodes: true,
              topology_edges: true,
              node_details: true,
            },
          },
        }),
      ),
    ],
  });

  await app.toggle(true);

  assert.match(app.content.textContent, /terminal workflow summary is available/i);
  assert.match(app.content.textContent, /Terminal outcomeSUCCEEDED/);
  assert.match(app.content.textContent, /Detail availabilityOMITTED BY POLICY/);
  assert.match(
    app.content.textContent,
    /No bounded topology actions are available/,
  );
  assert.equal(
    elementsByClass(app, "django-ray-workflow-graph").length,
    0,
  );
  assert.deepEqual(linkLabels(app), [
    "Download plan JSON",
    "Download selection JSON",
  ]);
  assert.equal(app.fetchCalls.length, 1);
});

test("terminal-only graph suppression survives corrupt plan diagnostics", async () => {
  const app = loadDiagnostics({
    fetchResponses: [
      response(
        200,
        validPayload({
          plan: {
            status: "CORRUPT",
            reporting_policy: undefined,
          },
          progress: {
            state: "CORRUPT",
            availability: "CORRUPT",
            complete: false,
            reporting_policy: "terminal_only",
            message: "Workflow diagnostics failed verification.",
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

  assert.match(app.content.textContent, /failed verification/i);
  assert.equal(
    elementsByClass(app, "django-ray-workflow-graph").length,
    0,
  );
  assert.match(
    app.content.textContent,
    /No bounded topology actions are available/,
  );
  assert.equal(app.fetchCalls.length, 1);
});

test("terminal-only pending and missing states never expose the graph", async (context) => {
  for (const [state, availability] of [
    ["TERMINAL_ONLY_PENDING", "NOT_REPORTED"],
    ["TERMINAL_ONLY_MISSING", "MISSING"],
  ]) {
    await context.test(state, async () => {
      const app = loadDiagnostics({
        fetchResponses: [
          response(
            200,
            validPayload({
              plan: {
                reporting_policy: "terminal_only",
              },
              progress: {
                state,
                availability,
                complete: false,
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

      assert.match(app.content.textContent, /Terminal-only reporting/);
      assert.equal(
        elementsByClass(app, "django-ray-workflow-graph").length,
        0,
      );
      assert.deepEqual(linkLabels(app), [
        "Download plan JSON",
        "Download selection JSON",
      ]);
      assert.equal(app.fetchCalls.length, 1);
    });
  }
});

test("live task status labels terminal-only summary without fabricated node execution", async () => {
  const live = loadTaskLive({
    state: "SUCCEEDED",
    attempt_number: 1,
    execution_generation: 0,
    workflow_run_id: "00000000-0000-0000-0000-000000000001",
    workflow_availability: "OMITTED_BY_POLICY",
    workflow: {
      revision: 1,
      state: "SUCCEEDED",
      total_nodes: 0,
      completed_nodes: 0,
      progress_percent: 100,
      reporting_policy: "terminal_only",
      declared_nodes: 12,
      detail: {
        availability: "OMITTED_BY_POLICY",
        complete: false,
        truncation_reasons: [],
      },
    },
  });

  await settle();

  assert.equal(
    live.fields.workflow.textContent,
    "Terminal summary: SUCCEEDED. Detail OMITTED_BY_POLICY. " +
      "The pinned plan declares 12 nodes. No node execution detail was collected.",
  );
  assert.equal(live.fields.workflow.textContent.includes("0/0 nodes"), false);
  assert.match(live.fields.status.textContent, /Terminal summary: SUCCEEDED/);
  assert.equal(live.fetchCalls.length, 1);
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

test("the nested graph is lazy, cached, topological, and keyboard navigable", async () => {
  const app = loadDiagnostics({
    dataset: {
      graphUrl:
        "/admin/execution/1/workflow/graph/?attempt_number=2",
      nodeDetailUrl:
        "/admin/execution/1/workflow/node/?attempt_number=2#bounded-detail",
    },
    fetchResponses: [
      response(
        200,
        validPayload({
          progress: {
            actions: {
              topology_nodes: true,
              topology_edges: true,
              node_details: true,
            },
          },
        }),
      ),
      response(200, validGraphPayload()),
    ],
  });

  assert.equal(app.fetchCalls.length, 0);
  await app.toggle(true);
  assert.equal(app.fetchCalls.length, 1);
  assert.equal(graphNodeLinks(app).length, 0);
  assert.match(graphStatus(app).textContent, /Open this section/);
  assert.match(graphStatus(app).textContent, /Attempt 2/);
  assert.match(
    graphStatus(app).textContent,
    /Reload the task page to inspect a newer live attempt/,
  );
  assert.equal(
    elementsByClass(
      app,
      "django-ray-workflow-graph__summary-title",
    )[0].textContent,
    "Execution graph \u2014 Attempt 2",
  );
  assert.equal(
    elementsByClass(app, "django-ray-workflow-graph")[0].open,
    false,
  );

  await app.toggleGraph(true);

  assert.equal(app.fetchCalls.length, 2);
  assert.equal(
    app.fetchCalls[1].url,
    "/admin/execution/1/workflow/graph/?attempt_number=2",
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(app.fetchCalls[1].options)),
    {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    },
  );
  assert.equal(graphContent(app).hidden, false);
  assert.equal(
    elementsByClass(app, "django-ray-workflow-graph__nodes")[0].attributes.get(
      "aria-label",
    ),
    "Workflow nodes in topological order",
  );
  assert.equal(
    graphStatus(app).textContent,
    "Attempt 2. Bounded workflow graph loaded. 3 nodes and 2 connections. Reload the task page to inspect a newer live attempt.",
  );
  assert.equal(
    elementsByClass(
      app,
      "django-ray-workflow-graph__summary-copy",
    )[0].textContent,
    "3 nodes, 2 connections. Reload the task page to inspect a newer live attempt.",
  );

  const nodeLinks = graphNodeLinks(app);
  assert.deepEqual(
    nodeLinks.map((link) =>
      elementsByClass(
        { content: link },
        "django-ray-workflow-graph__node-id",
      )[0].textContent,
    ),
    ["prepare", "fanout", "finish"],
  );
  assert.ok(nodeLinks.every((link) => link.tagName === "A"));
  assert.deepEqual(
    nodeLinks.map((link) => link.href),
    [
      "/admin/execution/1/workflow/node/?attempt_number=2&node_id=prepare#bounded-detail",
      "/admin/execution/1/workflow/node/?attempt_number=2&node_id=fanout#bounded-detail",
      "/admin/execution/1/workflow/node/?attempt_number=2&node_id=finish#bounded-detail",
    ],
  );
  assert.match(app.content.textContent, /Incoming from Prepare input/);
  assert.match(app.content.textContent, /Incoming from Process items/);
  assert.match(app.content.textContent, /\u25a1 Task/);
  assert.match(app.content.textContent, /\u25c7 Aggregate map/);
  assert.match(app.content.textContent, /\u2713Succeeded/);
  assert.match(app.content.textContent, /\u25b6Running/);
  assert.match(app.content.textContent, /\u25cbPending/);
  assert.match(app.content.textContent, /6 of 8 items completed/);

  const connectors = elementsByClass(
    app,
    "django-ray-workflow-graph__connector",
  );
  assert.equal(connectors.length, 2);
  assert.ok(
    connectors.every(
      (connector) =>
        connector.tagName === "SVG" &&
        connector.attributes.get("aria-hidden") === "true" &&
        connector.attributes.get("focusable") === "false",
    ),
  );

  await app.toggleGraph(false);
  await app.toggleGraph(true);
  assert.equal(app.fetchCalls.length, 2);
});

test("parallel roots and a multi-parent join keep an explicit incoming path", async () => {
  const nodes = [
    {
      id: "root-a",
      label: "Load accounts",
      kind: "task",
      state: "SUCCEEDED",
      message: null,
      error: null,
      failure_path: false,
    },
    {
      id: "root-b",
      label: "Load invoices",
      kind: "task",
      state: "SUCCEEDED",
      message: null,
      error: null,
      failure_path: false,
    },
    {
      id: "join",
      label: "Reconcile",
      kind: "task",
      state: "SUCCEEDED",
      message: null,
      error: null,
      failure_path: false,
    },
  ];
  const app = loadDiagnostics({
    fetchResponses: [
      response(200, validPayload()),
      response(
        200,
        validGraphPayload({
          nodes,
          edges: [
            { source: "root-a", target: "join" },
            { source: "root-b", target: "join" },
          ],
        }),
      ),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  const incoming = elementsByClass(
    app,
    "django-ray-workflow-graph__incoming",
  );
  assert.deepEqual(
    incoming.map((item) => item.dataset.root ?? ""),
    ["true", "true", ""],
  );
  assert.equal(
    incoming[2].textContent,
    "Incoming from Load accounts (root-a), Load invoices (root-b)",
  );
  assert.deepEqual(
    graphNodeLinks(app).map((link) => link.href),
    [
      "/admin/execution/1/workflow/node/?node_id=root-a",
      "/admin/execution/1/workflow/node/?node_id=root-b",
      "/admin/execution/1/workflow/node/?node_id=join",
    ],
  );
});

test("failed paths and malicious graph text stay visible as plain text", async () => {
  const maliciousId = "failed/node?<script>steal()</script>";
  const maliciousLabel = "<img src=x onerror=steal-secret()>";
  const maliciousMessage = "<svg onload=steal-message()> waiting";
  const maliciousError = "<script>steal-error()</script>";
  const nodes = [
    {
      id: "entry",
      label: "",
      kind: "task",
      state: "SUCCEEDED",
      message: null,
      error: null,
      failure_path: true,
    },
    {
      id: maliciousId,
      label: maliciousLabel,
      kind: "task",
      state: "FAILED",
      message: maliciousMessage,
      error: maliciousError,
      failure_path: true,
    },
  ];
  const app = loadDiagnostics({
    fetchResponses: [
      response(200, validPayload()),
      response(
        200,
        validGraphPayload({
          nodes,
          edges: [{ source: "entry", target: maliciousId }],
        }),
      ),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  assert.equal(graphContent(app).hidden, false);
  assert.match(app.content.textContent, /Workflow entry/);
  assert.match(app.content.textContent, /Incoming from entry/);
  assert.match(app.content.textContent, /Failure path/);
  assert.match(app.content.textContent, /!Failed/);
  assert.ok(
    graphNodeLinks(app).every(
      (link) => link.dataset.failurePath === "true",
    ),
  );
  assert.equal(app.content.textContent.includes(maliciousLabel), true);
  assert.equal(app.content.textContent.includes(maliciousMessage), true);
  assert.equal(app.content.textContent.includes(maliciousError), true);
  assert.equal(
    findAll(app.content, (element) => element.tagName === "IMG").length,
    0,
  );
  assert.equal(
    findAll(app.content, (element) => element.tagName === "SCRIPT").length,
    0,
  );
  assert.equal(
    graphNodeLinks(app)[1].href,
    "/admin/execution/1/workflow/node/?node_id=failed%2Fnode%3F%3Cscript%3Esteal%28%29%3C%2Fscript%3E",
  );
  assert.equal(diagnosticsScript.includes("innerHTML"), false);
});

test("terminal degraded graph statuses show fallbacks without partial rendering", async (context) => {
  for (const status of [
    "UNSUPPORTED",
    "TRUNCATED",
    "UNAVAILABLE",
    "LIMIT_EXCEEDED",
  ]) {
    await context.test(status, async () => {
      const app = loadDiagnostics({
        fetchResponses: [
          response(200, validPayload()),
          response(
            200,
            degradedGraphPayload(
              status,
              `Graph status ${status} is intentionally unavailable.`,
            ),
          ),
        ],
      });

      await app.toggle(true);
      await app.toggleGraph(true);

      assert.equal(graphContent(app).hidden, true);
      assert.equal(graphNodeLinks(app).length, 0);
      assert.match(graphStatus(app).textContent, new RegExp(status));
      assert.equal(
        graphStatus(app).dataset.state,
        status === "UNAVAILABLE" ? "error" : "warning",
      );
      assert.deepEqual(linkLabels(app).slice(-3), [
        "Topology nodes JSON",
        "Topology edges JSON",
        "Node details JSON",
      ]);

      await app.toggleGraph(false);
      await app.toggleGraph(true);
      assert.equal(app.fetchCalls.length, 2);
    });
  }
});

test("a pre-terminal graph can be retried and removes stale fallbacks after success", async () => {
  const app = loadDiagnostics({
    fetchResponses: [
      response(200, validPayload()),
      response(
        200,
        degradedGraphPayload(
          "NOT_REPORTED",
          "A terminal workflow publication is not available yet.",
        ),
      ),
      response(200, validGraphPayload()),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  assert.equal(graphContent(app).hidden, true);
  assert.equal(graphNodeLinks(app).length, 0);
  assert.equal(graphFallbacks(app).length, 1);
  assert.match(graphStatus(app).textContent, /Close and reopen/);
  assert.match(graphStatus(app).textContent, /terminal state/);
  assert.equal(graphStatus(app).dataset.state, "warning");

  await app.toggleGraph(false);
  await app.toggleGraph(true);

  assert.equal(app.fetchCalls.length, 3);
  assert.equal(graphContent(app).hidden, false);
  assert.equal(graphNodeLinks(app).length, 3);
  assert.equal(graphFallbacks(app).length, 0);
  assert.equal(
    linkLabels(app).includes("Topology nodes JSON"),
    false,
  );

  await app.toggleGraph(false);
  await app.toggleGraph(true);
  assert.equal(app.fetchCalls.length, 3);
});

test("malformed, cyclic, partial, and unknown graph data fail closed", async (context) => {
  const baseNodes = validGraphPayload().nodes;
  const malformedCases = [
    {
      name: "wrong schema",
      payload: validGraphPayload({
        schema: "django-ray.untrusted-workflow-graph",
      }),
    },
    {
      name: "wrong schema version",
      payload: validGraphPayload({ schema_version: 2 }),
    },
    {
      name: "unknown graph status",
      payload: validGraphPayload({ status: "SECRET_INTERNAL_STATE" }),
    },
    {
      name: "corrupt status with a success response",
      payload: degradedGraphPayload(
        "CORRUPT",
        "Corrupt graph sent with the wrong response status.",
      ),
    },
    {
      name: "incomplete available graph",
      payload: validGraphPayload({ complete: false }),
    },
    {
      name: "empty available graph",
      payload: validGraphPayload({
        counts: { nodes: 0, edges: 0 },
        nodes: [],
        edges: [],
      }),
    },
    {
      name: "partial truncated graph",
      payload: validGraphPayload({
        status: "TRUNCATED",
        complete: false,
      }),
    },
    {
      name: "count mismatch",
      payload: validGraphPayload({
        counts: { nodes: 99, edges: 2 },
      }),
    },
    {
      name: "changed server limits",
      payload: validGraphPayload({
        limits: {
          nodes: 101,
          edges: 256,
          details: 100,
          response_bytes: 131072,
        },
      }),
    },
    {
      name: "unknown edge endpoint",
      payload: validGraphPayload({
        edges: [{ source: "prepare", target: "missing" }],
      }),
    },
    {
      name: "cycle and backward edge",
      payload: validGraphPayload({
        nodes: baseNodes.slice(0, 2),
        edges: [
          { source: "prepare", target: "fanout" },
          { source: "fanout", target: "prepare" },
        ],
      }),
    },
    {
      name: "unknown node state",
      payload: validGraphPayload({
        nodes: [
          { ...baseNodes[0], state: "CANCELLED" },
          ...baseNodes.slice(1),
        ],
      }),
    },
    {
      name: "error on a nonfailed node",
      payload: validGraphPayload({
        nodes: [
          { ...baseNodes[0], error: "incoherent error" },
          ...baseNodes.slice(1),
        ],
      }),
    },
    {
      name: "map without fanout",
      payload: validGraphPayload({
        nodes: [
          baseNodes[0],
          {
            id: "fanout",
            label: "Process items",
            kind: "map",
            state: "RUNNING",
            message: null,
            error: null,
            failure_path: false,
          },
          baseNodes[2],
        ],
      }),
    },
    {
      name: "unallowlisted node field",
      payload: validGraphPayload({
        nodes: [
          { ...baseNodes[0], private_runtime_value: "must-not-render" },
          ...baseNodes.slice(1),
        ],
      }),
    },
    {
      name: "oversized UTF-8 node id",
      payload: validGraphPayload({
        nodes: [
          { ...baseNodes[0], id: "\ud83d\ude00".repeat(65) },
          ...baseNodes.slice(1),
        ],
      }),
    },
  ];

  for (const { name, payload } of malformedCases) {
    await context.test(name, async () => {
      const app = loadDiagnostics({
        fetchResponses: [
          response(200, validPayload()),
          response(200, payload),
          response(200, validGraphPayload()),
        ],
      });

      await app.toggle(true);
      await app.toggleGraph(true);

      assert.equal(graphContent(app).hidden, true);
      assert.equal(graphNodeLinks(app).length, 0);
      assert.match(
        graphStatus(app).textContent,
        /could not be displayed safely/,
      );
      assert.equal(
        app.content.textContent.includes("private_runtime_value"),
        false,
      );
      assert.deepEqual(linkLabels(app).slice(-3), [
        "Topology nodes JSON",
        "Topology edges JSON",
        "Node details JSON",
      ]);

      await app.toggleGraph(false);
      await app.toggleGraph(true);
      assert.equal(app.fetchCalls.length, 3);
      assert.equal(graphContent(app).hidden, false);
      assert.equal(graphNodeLinks(app).length, 3);
      assert.equal(graphFallbacks(app).length, 0);

      await app.toggleGraph(false);
      await app.toggleGraph(true);
      assert.equal(app.fetchCalls.length, 3);
    });
  }
});

test("graph authentication failures are terminal and never expose response data", async () => {
  const secret = "graph-auth-private-secret";
  const app = loadDiagnostics({
    fetchResponses: [
      response(200, validPayload()),
      response(403, { detail: secret }),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  assert.equal(graphContent(app).hidden, true);
  assert.match(graphStatus(app).textContent, /signing in again/);
  assert.equal(graphStatus(app).textContent.includes(secret), false);
  assert.deepEqual(linkLabels(app).slice(-3), [
    "Topology nodes JSON",
    "Topology edges JSON",
    "Node details JSON",
  ]);

  await app.toggleGraph(false);
  await app.toggleGraph(true);
  assert.equal(app.fetchCalls.length, 2);
});

test("graph network failures retry only after a close and reopen", async () => {
  const app = loadDiagnostics({
    fetchResponses: [
      response(200, validPayload()),
      new Error("network failure included graph-private-secret"),
      response(200, validGraphPayload()),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  assert.equal(app.fetchCalls.length, 2);
  assert.equal(graphContent(app).hidden, true);
  assert.match(graphStatus(app).textContent, /Close and reopen/);
  assert.equal(
    graphStatus(app).textContent.includes("graph-private-secret"),
    false,
  );

  await app.toggleGraph(false);
  assert.equal(app.fetchCalls.length, 2);
  await app.toggleGraph(true);
  assert.equal(app.fetchCalls.length, 3);
  assert.equal(graphContent(app).hidden, false);
  assert.equal(graphFallbacks(app).length, 0);

  await app.toggleGraph(false);
  await app.toggleGraph(true);
  assert.equal(app.fetchCalls.length, 3);
});

test("a bounded corrupt response degrades without rendering or retrying", async () => {
  const app = loadDiagnostics({
    fetchResponses: [
      response(200, validPayload()),
      response(
        503,
        degradedGraphPayload(
          "CORRUPT",
          "The retained graph failed bounded validation.",
        ),
      ),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  assert.equal(graphContent(app).hidden, true);
  assert.equal(graphNodeLinks(app).length, 0);
  assert.match(graphStatus(app).textContent, /failed bounded validation/);
  assert.equal(graphStatus(app).dataset.state, "error");
  assert.deepEqual(linkLabels(app).slice(-3), [
    "Topology nodes JSON",
    "Topology edges JSON",
    "Node details JSON",
  ]);

  await app.toggleGraph(false);
  await app.toggleGraph(true);
  assert.equal(app.fetchCalls.length, 2);
});

test("graph styles cover neutral light, dark, state, shape, and narrow layouts", () => {
  assert.match(
    graphStyles,
    /:is\(html\.dark, html\[data-theme="dark"\]\) #django-ray-live-observability/,
  );
  assert.match(
    graphStyles,
    /\.django-ray-workflow-graph__node\[data-kind="map"\]/,
  );
  assert.match(
    graphStyles,
    /border-right: 5px double var\(--django-ray-live-accent-strong\)/,
  );
  assert.match(
    graphStyles,
    /django-ray-workflow-graph__status\[data-state="warning"\]/,
  );
  for (const state of ["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]) {
    assert.match(
      graphStyles,
      new RegExp(
        String.raw`django-ray-workflow-graph__state\[data-state="${state}"\]`,
      ),
    );
  }
  assert.match(graphStyles, /@media \(max-width: 640px\)/);
  assert.match(
    graphStyles,
    /\.django-ray-workflow-graph__node \{\s+grid-template-columns: minmax\(0, 1fr\)/,
  );
  assert.match(graphStyles, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(graphStyles, /django-ray-workflow-graph__summary-arrow/);
  assert.match(
    graphStyles,
    /outline: 3px solid var\(--django-ray-live-accent\)/,
  );
});
