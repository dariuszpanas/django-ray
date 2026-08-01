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

function cssHexToken(name) {
  const match = graphStyles.match(
    new RegExp(`${name}: (#[0-9a-fA-F]{6});`),
  );
  assert.ok(match, `${name} must define a six-digit light-theme color`);
  return match[1];
}

function contrastAgainstWhite(hexColor) {
  return contrastRatio(hexColor, "#ffffff");
}

function contrastRatio(firstHexColor, secondHexColor) {
  const luminance = (hexColor) => {
    const channels = [1, 3, 5].map((offset) =>
      Number.parseInt(hexColor.slice(offset, offset + 2), 16) / 255,
    );
    const [red, green, blue] = channels.map((channel) =>
      channel <= 0.04045
        ? channel / 12.92
        : ((channel + 0.055) / 1.055) ** 2.4,
    );
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  };
  const firstLuminance = luminance(firstHexColor);
  const secondLuminance = luminance(secondHexColor);
  const lighter = Math.max(firstLuminance, secondLuminance);
  const darker = Math.min(firstLuminance, secondLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

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

  getBoundingClientRect() {
    const classes = this.className.split(" ").filter(Boolean);
    if (classes.includes("django-ray-workflow-graph__diagram")) {
      const cards = findAll(this, (element) =>
        element.className
          .split(" ")
          .filter(Boolean)
          .includes("django-ray-workflow-graph__node"),
      );
      const maximumLayer = Math.max(
        0,
        ...cards.map((card) => Number(card.dataset.layer)),
      );
      const maximumPosition = Math.max(
        0,
        ...cards.map((card) => Number(card.dataset.position)),
      );
      return fakeRectangle(
        0,
        0,
        Math.max(640, (maximumPosition + 1) * 240 + 48),
        (maximumLayer + 1) * 180 + 40,
      );
    }
    if (classes.includes("django-ray-workflow-graph__node")) {
      const layer = Number(this.dataset.layer);
      const position = Number(this.dataset.position);
      return fakeRectangle(24 + position * 240, 40 + layer * 180, 208, 112);
    }
    return fakeRectangle(0, 0, 0, 0);
  }

  querySelector(selector) {
    return findAll(this, (element) => matches(element, selector))[0] ?? null;
  }

  querySelectorAll(selector) {
    return findAll(this, (element) => matches(element, selector));
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

function fakeRectangle(left, top, width, height) {
  return {
    bottom: top + height,
    height,
    left,
    right: left + width,
    top,
    width,
    x: left,
    y: top,
  };
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
  const rawNodes = overrides.nodes ?? [
    {
      id: "prepare",
      label: "Prepare input",
      kind: "task",
      state: "SUCCEEDED",
      message: "Input is ready.",
      error: null,
      failure_path: false,
      output_preview: {
        schema_version: 1,
        availability: "AVAILABLE",
        value: { batch: "ready", item_count: 8 },
      },
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
  const nodes = rawNodes.map((node) => ({
    output_preview: {
      schema_version: 1,
      availability: "NOT_REQUESTED",
      value: null,
    },
    ...node,
  }));
  const edges = overrides.edges ?? [
    { source: "prepare", target: "fanout" },
    { source: "fanout", target: "finish" },
  ];
  return {
    schema: "django-ray.admin-workflow-graph",
    schema_version: 2,
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
    ...overrides,
    nodes,
    edges,
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

function archivedGraphDisclosure(
  attemptNumber,
  { currentAttemptNumber = 3, dataset = {} } = {},
) {
  const attempt = String(attemptNumber);
  const details = new FakeElement("details");
  details.className =
    "django-ray-workflow-graph django-ray-workflow-attempt-graph";
  details.dataset = {
    workflowAttemptGraph: attempt,
    workflowGraphAttempt: attempt,
    hydrationState: "idle",
    pinnedAttemptNumber: attempt,
    currentAttemptNumber: String(currentAttemptNumber),
    graphUrl: `/admin/execution/1/workflow/graph/?attempt_number=${attempt}`,
    topologyNodesUrl: `/admin/execution/1/workflow/topology/nodes/?attempt_number=${attempt}`,
    topologyEdgesUrl: `/admin/execution/1/workflow/topology/edges/?attempt_number=${attempt}`,
    nodeDetailsUrl: `/admin/execution/1/workflow/nodes/?attempt_number=${attempt}`,
    nodeDetailUrl: `/admin/execution/1/workflow/node/?attempt_number=${attempt}`,
    ...dataset,
  };
  const titleId = `django-ray-workflow-attempt-${attempt}-title`;
  details.setAttribute("aria-labelledby", titleId);
  const summary = new FakeElement("summary");
  const title = new FakeElement("span");
  title.className = "django-ray-workflow-graph__summary-title";
  title.setAttribute("id", titleId);
  title.textContent = `Execution graph \u2014 Attempt ${attempt} (failed)`;
  const summaryMessage = new FakeElement("span");
  summaryMessage.dataset.workflowGraphSummaryMessage = "";
  summaryMessage.textContent =
    "Archived failure. Open to load this exact attempt.";
  summary.append(title, summaryMessage);
  const body = new FakeElement("div");
  const status = new FakeElement("p");
  status.className = "django-ray-workflow-graph__status";
  status.dataset.workflowGraphStatus = "";
  const statusMessage = new FakeElement("span");
  statusMessage.dataset.workflowGraphStatusMessage = "";
  statusMessage.textContent =
    "Open this panel to load its bounded archived graph.";
  status.append(statusMessage);
  const content = new FakeElement("div");
  content.className = "django-ray-workflow-graph__content";
  content.dataset.workflowGraphContent = "";
  content.hidden = true;
  body.append(status, content);
  details.append(summary, body);
  return details;
}

function loadDiagnostics({
  fetchResponses = [],
  clipboardFailure = null,
  dataset = {},
  archivedAttempts = [],
  currentAttemptState = "RUNNING",
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
  const archivedGraphs = archivedAttempts.map((configuration) =>
    typeof configuration === "number"
      ? archivedGraphDisclosure(configuration)
      : archivedGraphDisclosure(configuration.attemptNumber, configuration),
  );
  const attemptGraphStack = new FakeElement("section");
  attemptGraphStack.dataset.workflowAttemptGraphs = "";
  attemptGraphStack.hidden = archivedGraphs.length === 0;
  const attemptGraphHeading = new FakeElement("div");
  attemptGraphHeading.textContent = "Attempt execution graphs";
  const currentGraphMount = new FakeElement("div");
  currentGraphMount.dataset.workflowCurrentGraph = "";
  currentGraphMount.dataset.currentAttemptNumber =
    details.dataset.pinnedAttemptNumber;
  currentGraphMount.dataset.currentAttemptState = currentAttemptState;
  currentGraphMount.hidden = true;
  attemptGraphStack.append(
    attemptGraphHeading,
    ...archivedGraphs,
    currentGraphMount,
  );
  details.append(status, content, attemptGraphStack);

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
    ResizeObserver: class {
      constructor(callback) {
        this.callback = callback;
      }

      observe() {
        this.callback([]);
      }
    },
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
    requestAnimationFrame(callback) {
      callback();
      return 1;
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
    archivedGraphs,
    attemptGraphStack,
    fetchCalls,
    currentGraphMount,
    queuedResponses,
    status,
    async toggle(open) {
      details.open = open;
      await details.dispatch("toggle");
      await settle();
    },
    async toggleGraph(open) {
      const graph = findAll(
        currentGraphMount,
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
    async toggleArchivedGraph(index, open) {
      const graph = archivedGraphs[index];
      assert.ok(graph, `expected archived graph disclosure ${index}`);
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
  return findAll(app.details ?? app.content, (element) => element.tagName === "A");
}

function linkLabels(app) {
  return links(app).map((link) => link.textContent);
}

function elementsByClass(app, className) {
  const root = app.details ?? app.content;
  return findAll(
    root,
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

function graphNodeId(link) {
  return findAll(
    link,
    (element) =>
      element.className === "django-ray-workflow-graph__node-id",
  )[0].textContent.replace(/^Node ID: /, "");
}

function graphNodeOutput(link) {
  return findAll(
    link,
    (element) =>
      element.className === "django-ray-workflow-graph__node-output",
  )[0].textContent;
}

function graphLayerNodeIds(app) {
  return elementsByClass(app, "django-ray-workflow-graph__stage").map(
    (stage) =>
      findAll(
        stage,
        (element) =>
          element.className
            .split(" ")
            .filter(Boolean)
            .includes("django-ray-workflow-graph__node"),
      ).map(graphNodeId),
  );
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

test("archived attempt graphs are independently lazy, exact, and cached", async () => {
  const app = loadDiagnostics({
    archivedAttempts: [1, 2],
    fetchResponses: [
      response(200, validGraphPayload()),
      response(200, validGraphPayload()),
    ],
  });

  assert.equal(app.fetchCalls.length, 0);
  assert.deepEqual(
    app.archivedGraphs.map((graph) => graph.open),
    [false, false],
  );
  assert.deepEqual(
    app.archivedGraphs.map((graph) => graph.dataset.hydrationState),
    ["idle", "idle"],
  );

  const first = await app.toggleArchivedGraph(0, true);
  assert.equal(app.fetchCalls.length, 1);
  assert.equal(
    app.fetchCalls[0].url,
    "/admin/execution/1/workflow/graph/?attempt_number=1",
  );
  assert.equal(first.dataset.hydrationState, "ready");
  assert.ok(
    graphNodeLinks({ content: first }).every((link) =>
      link.href.includes("attempt_number=1&node_id="),
    ),
  );
  assert.equal(app.archivedGraphs[1].dataset.hydrationState, "idle");

  const second = await app.toggleArchivedGraph(1, true);
  assert.equal(app.fetchCalls.length, 2);
  assert.equal(
    app.fetchCalls[1].url,
    "/admin/execution/1/workflow/graph/?attempt_number=2",
  );
  assert.equal(second.dataset.hydrationState, "ready");
  assert.ok(
    graphNodeLinks({ content: second }).every((link) =>
      link.href.includes("attempt_number=2&node_id="),
    ),
  );
  const descriptionIds = app.archivedGraphs.flatMap((graph) =>
    graphNodeLinks({ content: graph }).map((link) =>
      link.attributes.get("aria-describedby"),
    ),
  );
  assert.equal(new Set(descriptionIds).size, descriptionIds.length);
  const markerEnds = app.archivedGraphs.flatMap((graph) =>
    elementsByClass(
      { content: graph },
      "django-ray-workflow-graph__connector",
    ).map((connector) => connector.attributes.get("marker-end")),
  );
  assert.deepEqual(new Set(markerEnds), new Set([
    "url(#django-ray-workflow-graph-arrow)",
    "url(#django-ray-workflow-graph-arrow-2)",
  ]));

  await app.toggleArchivedGraph(0, false);
  await app.toggleArchivedGraph(0, true);
  await app.toggleArchivedGraph(1, false);
  await app.toggleArchivedGraph(1, true);
  assert.equal(app.fetchCalls.length, 2);
});

test("failed and current attempts share one ordered accessible graph stack", async () => {
  const app = loadDiagnostics({
    archivedAttempts: [1, 2],
    currentAttemptState: "SUCCEEDED",
    dataset: {
      pinnedAttemptNumber: "3",
      graphUrl: "/admin/execution/1/workflow/graph/?attempt_number=3",
      topologyNodesUrl:
        "/admin/execution/1/workflow/topology/nodes/?attempt_number=3",
      topologyEdgesUrl:
        "/admin/execution/1/workflow/topology/edges/?attempt_number=3",
      nodeDetailsUrl: "/admin/execution/1/workflow/nodes/?attempt_number=3",
      nodeDetailUrl: "/admin/execution/1/workflow/node/?attempt_number=3",
    },
    fetchResponses: [
      response(200, validPayload()),
      response(200, validGraphPayload()),
      response(200, validGraphPayload()),
      response(200, validGraphPayload()),
    ],
  });

  assert.equal(app.attemptGraphStack.parentNode, app.details);
  assert.equal(app.attemptGraphStack.hidden, false);
  assert.equal(app.currentGraphMount.hidden, true);
  assert.deepEqual(
    app.archivedGraphs.map((graph) => graph.open),
    [false, false],
  );
  assert.equal(app.fetchCalls.length, 0);

  await app.toggle(true);

  const orderedPanels = findAll(
    app.attemptGraphStack,
    (element) =>
      element.tagName === "DETAILS" &&
      element.className.split(" ").includes("django-ray-workflow-graph"),
  );
  assert.equal(app.currentGraphMount.hidden, false);
  assert.equal(orderedPanels.length, 3);
  assert.deepEqual(
    orderedPanels.map((panel) => panel.dataset.workflowGraphAttempt),
    ["1", "2", "3"],
  );
  assert.deepEqual(
    orderedPanels.map(
      (panel) =>
        findAll(
          panel,
          (element) =>
            element.className === "django-ray-workflow-graph__summary-title",
        )[0].textContent,
    ),
    [
      "Execution graph \u2014 Attempt 1 (failed)",
      "Execution graph \u2014 Attempt 2 (failed)",
      "Execution graph \u2014 Attempt 3 (current, succeeded)",
    ],
  );
  assert.ok(orderedPanels.every((panel) => panel.open === false));
  assert.ok(
    orderedPanels.every((panel) => panel.children[0].tagName === "SUMMARY"),
  );
  const titleIds = orderedPanels.map((panel) =>
    findAll(
      panel,
      (element) =>
        element.className === "django-ray-workflow-graph__summary-title",
    )[0].attributes.get("id"),
  );
  assert.equal(new Set(titleIds).size, 3);
  assert.deepEqual(
    orderedPanels.map((panel) => panel.attributes.get("aria-labelledby")),
    titleIds,
  );

  await app.toggleArchivedGraph(0, true);
  await app.toggleArchivedGraph(1, true);
  await app.toggleGraph(true);
  assert.deepEqual(
    app.fetchCalls.map((call) => call.url),
    [
      "/admin/execution/1/workflow/diagnostics/",
      "/admin/execution/1/workflow/graph/?attempt_number=1",
      "/admin/execution/1/workflow/graph/?attempt_number=2",
      "/admin/execution/1/workflow/graph/?attempt_number=3",
    ],
  );
  assert.ok(
    graphNodeLinks({ content: orderedPanels[2] }).every((link) =>
      link.href.includes("attempt_number=3&node_id="),
    ),
  );
  const descriptionIds = orderedPanels.flatMap((panel) =>
    graphNodeLinks({ content: panel }).map((link) =>
      link.attributes.get("aria-describedby"),
    ),
  );
  assert.equal(new Set(descriptionIds).size, descriptionIds.length);
  const markerEnds = orderedPanels.flatMap((panel) =>
    elementsByClass(
      { content: panel },
      "django-ray-workflow-graph__connector",
    ).map((connector) => connector.attributes.get("marker-end")),
  );
  assert.deepEqual(
    new Set(markerEnds),
    new Set([
      "url(#django-ray-workflow-graph-arrow)",
      "url(#django-ray-workflow-graph-arrow-2)",
      "url(#django-ray-workflow-graph-arrow-3)",
    ]),
  );

  await app.toggleArchivedGraph(0, false);
  assert.equal(orderedPanels[0].open, false);
  assert.equal(orderedPanels[1].open, true);
  assert.equal(orderedPanels[2].open, true);
  await app.toggleArchivedGraph(0, true);
  await app.toggleGraph(false);
  await app.toggleGraph(true);
  await app.toggle(false);
  await app.toggle(true);
  assert.equal(app.fetchCalls.length, 4);
  assert.equal(
    findAll(
      app.currentGraphMount,
      (element) => element.dataset.workflowCurrentGraphPanel === "",
    ).length,
    1,
  );
});

test("an archived graph caches bounded failure without leaking response data", async () => {
  const secret = "archived-graph-private-secret";
  const app = loadDiagnostics({
    archivedAttempts: [1],
    fetchResponses: [
      response(200, validGraphPayload({ private_runtime_value: secret })),
    ],
  });

  const graph = await app.toggleArchivedGraph(0, true);
  assert.equal(app.fetchCalls.length, 1);
  assert.equal(graph.dataset.hydrationState, "error");
  assert.equal(graphNodeLinks({ content: graph }).length, 0);
  assert.equal(graph.textContent.includes(secret), false);
  assert.match(graph.textContent, /Reload this page to try again/);

  await app.toggleArchivedGraph(0, false);
  await app.toggleArchivedGraph(0, true);
  assert.equal(app.fetchCalls.length, 1);
});

test("an archived graph rejects mixed attempt endpoints before fetching", async () => {
  const app = loadDiagnostics({
    archivedAttempts: [
      {
        attemptNumber: 1,
        dataset: {
          graphUrl: "/admin/execution/1/workflow/graph/?attempt_number=2",
        },
      },
    ],
  });

  const graph = app.archivedGraphs[0];
  assert.equal(graph.dataset.hydrationState, "error");
  assert.match(graph.textContent, /could not be initialized safely/);
  await app.toggleArchivedGraph(0, true);
  assert.equal(app.fetchCalls.length, 0);
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
    "Execution graph \u2014 Attempt 2 (current, running)",
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
    "Workflow stages in topological order",
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
    ["Node ID: prepare", "Node ID: fanout", "Node ID: finish"],
  );
  assert.deepEqual(nodeLinks.map(graphNodeOutput), [
    'Output: Preview \u2014 {"batch":"ready","item_count":8}',
    "Output: Not requested \u2014 value not retained in workflow diagnostics",
    "Output: Not requested \u2014 value not retained in workflow diagnostics",
  ]);
  assert.ok(nodeLinks.every((link) => link.tagName === "A"));
  assert.deepEqual(
    nodeLinks.map((link) => link.href),
    [
      "/admin/execution/1/workflow/node/?attempt_number=2&node_id=prepare#bounded-detail",
      "/admin/execution/1/workflow/node/?attempt_number=2&node_id=fanout#bounded-detail",
      "/admin/execution/1/workflow/node/?attempt_number=2&node_id=finish#bounded-detail",
    ],
  );
  assert.match(app.details.textContent, /Incoming from Prepare input/);
  assert.match(app.details.textContent, /Incoming from Process items/);
  assert.match(app.details.textContent, /\u25a1 Task/);
  assert.match(app.details.textContent, /\u25c7 Aggregate map/);
  assert.match(app.details.textContent, /\u2713Succeeded/);
  assert.match(app.details.textContent, /\u25b6Running/);
  assert.match(app.details.textContent, /\u25cbPending/);
  assert.match(app.details.textContent, /6 of 8 items completed/);
  const outputs = elementsByClass(app, "django-ray-workflow-graph__node-output");
  assert.equal(outputs.length, 3);
  assert.deepEqual(
    outputs.map((output) => [
      output.dataset.availability,
      output.dataset.hasPreview ?? "",
    ]),
    [
      ["AVAILABLE", "true"],
      ["NOT_REQUESTED", ""],
      ["NOT_REQUESTED", ""],
    ],
  );

  const connectors = elementsByClass(
    app,
    "django-ray-workflow-graph__connector",
  );
  assert.equal(connectors.length, 2);
  assert.ok(
    connectors.every(
      (connector) =>
        connector.tagName === "PATH" &&
        connector.attributes.get("d").length > 0,
    ),
  );
  assert.deepEqual(
    connectors.map((connector) => [
      connector.dataset.source,
      connector.dataset.target,
    ]),
    [
      ["prepare", "fanout"],
      ["fanout", "finish"],
    ],
  );
  const connectorCanvas = elementsByClass(
    app,
    "django-ray-workflow-graph__connectors",
  );
  assert.equal(connectorCanvas.length, 1);
  assert.equal(connectorCanvas[0].tagName, "SVG");
  assert.equal(connectorCanvas[0].attributes.get("aria-hidden"), "true");
  assert.equal(connectorCanvas[0].attributes.get("focusable"), "false");
  assert.equal(connectorCanvas[0].attributes.get("viewBox"), "0 0 640 580");

  await app.toggleGraph(false);
  await app.toggleGraph(true);
  assert.equal(app.fetchCalls.length, 2);
});

test("canonical backend previews survive browser number normalization", async () => {
  const value = {
    numbers: Array(16).fill(0.000001),
    a: "x".repeat(200),
    b: "y".repeat(142),
  };
  const browserBytes = new TextEncoder().encode(JSON.stringify(value)).byteLength;
  assert.ok(browserBytes > 512);
  assert.ok(browserBytes <= 512 + 3 * 32);
  const app = loadDiagnostics({
    fetchResponses: [
      response(200, validPayload()),
      response(
        200,
        validGraphPayload({
          nodes: [
            {
              id: "normalized-preview",
              label: "Normalized preview",
              kind: "task",
              state: "SUCCEEDED",
              message: null,
              error: null,
              failure_path: false,
              output_preview: {
                schema_version: 1,
                availability: "AVAILABLE",
                value,
              },
            },
          ],
          edges: [],
        }),
      ),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  assert.equal(graphContent(app).hidden, false);
  assert.match(graphNodeOutput(graphNodeLinks(app)[0]), /0\.000001/);
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
  assert.deepEqual(graphLayerNodeIds(app), [
    ["root-a", "root-b"],
    ["join"],
  ]);
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

test("repeated split and join nodes render in deterministic longest-path stages", async () => {
  const taskNode = (id, label, overrides = {}) => ({
    id,
    label,
    kind: "task",
    state: "SUCCEEDED",
    message: null,
    error: null,
    failure_path: false,
    ...overrides,
  });
  const nodes = [
    taskNode("order", "Build order batch"),
    taskNode("profile", "Load customer profile"),
    taskNode("history", "Load customer history"),
    taskNode("validate", "Validate order items"),
    taskNode("inventory", "Read inventory snapshot"),
    taskNode("preflight", "Join fulfillment inputs"),
    taskNode("reserve", "Reserve inventory", {
      kind: "map",
      fanout: {
        submitted_items: 3,
        completed_items: 3,
        in_flight_items: 0,
        input_exhausted: true,
      },
    }),
    taskNode("price", "Calculate prices"),
    taskNode("risk", "Score risk"),
    taskNode("recommend", "Build recommendations"),
    taskNode("pricing", "Join decision inputs"),
    taskNode("decision", "Commit fulfillment decision"),
    taskNode("primary-write", "Write order"),
    taskNode("audit-write", "Write audit event"),
    taskNode("notification", "Send notification"),
    taskNode("final", "Finalize result"),
  ];
  const edges = [
    { source: "order", target: "profile" },
    { source: "profile", target: "history" },
    { source: "order", target: "validate" },
    { source: "order", target: "inventory" },
    { source: "validate", target: "preflight" },
    { source: "history", target: "preflight" },
    { source: "inventory", target: "preflight" },
    { source: "preflight", target: "reserve" },
    { source: "preflight", target: "price" },
    { source: "preflight", target: "risk" },
    { source: "preflight", target: "recommend" },
    { source: "price", target: "pricing" },
    { source: "risk", target: "pricing" },
    { source: "recommend", target: "pricing" },
    { source: "reserve", target: "decision" },
    { source: "pricing", target: "decision" },
    { source: "decision", target: "primary-write" },
    { source: "decision", target: "audit-write" },
    { source: "decision", target: "notification" },
    { source: "primary-write", target: "final" },
    { source: "audit-write", target: "final" },
    { source: "notification", target: "final" },
  ];
  const app = loadDiagnostics({
    fetchResponses: [
      response(200, validPayload()),
      response(200, validGraphPayload({ nodes, edges })),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  assert.deepEqual(graphLayerNodeIds(app), [
    ["order"],
    ["profile", "validate", "inventory"],
    ["history"],
    ["preflight"],
    ["reserve", "price", "risk", "recommend"],
    ["pricing"],
    ["decision"],
    ["primary-write", "audit-write", "notification"],
    ["final"],
  ]);
  const stages = elementsByClass(app, "django-ray-workflow-graph__stage");
  assert.deepEqual(
    stages.map((stage) => stage.dataset.layer),
    ["0", "1", "2", "3", "4", "5", "6", "7", "8"],
  );
  assert.deepEqual(
    elementsByClass(app, "django-ray-workflow-graph__stage-copy").map(
      (copy) => copy.textContent,
    ),
    [
      "1 node",
      "3 parallel nodes",
      "1 node",
      "1 node",
      "4 parallel nodes",
      "1 node",
      "1 node",
      "3 parallel nodes",
      "1 node",
    ],
  );

  const renderedNodes = graphNodeLinks(app);
  const renderedIndexes = new Map(
    renderedNodes.map((node, index) => [graphNodeId(node), index]),
  );
  for (const edge of edges) {
    assert.ok(
      renderedIndexes.get(edge.source) < renderedIndexes.get(edge.target),
      `${edge.source} must precede ${edge.target}`,
    );
  }
  const predecessorIds = new Set(
    elementsByClass(app, "django-ray-workflow-graph__incoming").map(
      (incoming) => incoming.attributes.get("id"),
    ),
  );
  assert.ok(
    renderedNodes.every((node) =>
      predecessorIds.has(node.attributes.get("aria-describedby")),
    ),
  );
  assert.match(
    app.details.textContent,
    /Incoming from Load customer history \(history\), Validate order items \(validate\), Read inventory snapshot \(inventory\)/,
  );

  const connectors = elementsByClass(
    app,
    "django-ray-workflow-graph__connector",
  );
  assert.equal(connectors.length, edges.length);
  const longConnector = connectors.find(
    (connector) =>
      connector.dataset.source === "validate" &&
      connector.dataset.target === "preflight",
  );
  assert.equal(
    longConnector.attributes.get("d"),
    "M368 332C368 456 128 456 128 580",
  );
  assert.equal(
    elementsByClass(app, "django-ray-workflow-graph__connectors")[0].attributes.get(
      "viewBox",
    ),
    "0 0 1008 1660",
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
      output_preview: {
        schema_version: 1,
        availability: "AVAILABLE",
        value: { status: "</code><script>steal-preview()</script>" },
      },
    },
    {
      id: maliciousId,
      label: maliciousLabel,
      kind: "task",
      state: "FAILED",
      message: maliciousMessage,
      error: maliciousError,
      failure_path: true,
      output_preview: {
        schema_version: 1,
        availability: "UNAVAILABLE",
        value: null,
      },
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
  assert.match(app.details.textContent, /Workflow entry/);
  assert.match(app.details.textContent, /Incoming from entry/);
  assert.match(app.details.textContent, /Upstream of failure/);
  assert.match(app.details.textContent, /Failure origin/);
  assert.match(app.details.textContent, /!Failed/);
  assert.match(
    app.details.textContent,
    /Output: Preview unavailable \u2014 no retained diagnostic value/,
  );
  assert.ok(
    graphNodeLinks(app).every(
      (link) => link.dataset.failurePath === "true",
    ),
  );
  assert.equal(app.details.textContent.includes(maliciousLabel), true);
  assert.equal(app.details.textContent.includes(maliciousMessage), true);
  assert.equal(app.details.textContent.includes(maliciousError), true);
  assert.equal(app.details.textContent.includes("steal-preview()"), true);
  assert.equal(
    findAll(app.details, (element) => element.tagName === "IMG").length,
    0,
  );
  assert.equal(
    findAll(app.details, (element) => element.tagName === "SCRIPT").length,
    0,
  );
  assert.equal(
    graphNodeLinks(app)[1].href,
    "/admin/execution/1/workflow/node/?node_id=failed%2Fnode%3F%3Cscript%3Esteal%28%29%3C%2Fscript%3E",
  );
  assert.equal(diagnosticsScript.includes("innerHTML"), false);
});

test("danger emphasis is confined to the originating failed node", async () => {
  const nodes = [
    {
      id: "entry",
      label: "Build order",
      kind: "task",
      state: "SUCCEEDED",
      message: null,
      error: null,
      failure_path: true,
    },
    {
      id: "validation",
      label: "Validate order",
      kind: "task",
      state: "SUCCEEDED",
      message: null,
      error: null,
      failure_path: true,
    },
    {
      id: "reservation",
      label: "Reserve inventory item 1",
      kind: "task",
      state: "FAILED",
      message: null,
      error: "Deliberate reservation failure.",
      failure_path: true,
    },
    {
      id: "decision",
      label: "Join fulfillment decision",
      kind: "task",
      state: "FAILED",
      message: null,
      error: "Dependency failed.",
      failure_path: false,
    },
    {
      id: "final",
      label: "Finalize order",
      kind: "task",
      state: "FAILED",
      message: null,
      error: "Dependency failed.",
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
            { source: "entry", target: "validation" },
            { source: "validation", target: "reservation" },
            { source: "reservation", target: "decision" },
            { source: "decision", target: "final" },
          ],
        }),
      ),
    ],
  });

  await app.toggle(true);
  await app.toggleGraph(true);

  assert.deepEqual(
    graphNodeLinks(app).map((node) => node.dataset.failurePath ?? ""),
    ["true", "true", "true", "", ""],
  );
  assert.deepEqual(
    graphNodeLinks(app).map((node) => node.dataset.failureOrigin ?? ""),
    ["", "", "true", "", ""],
  );
  const connectors = elementsByClass(
    app,
    "django-ray-workflow-graph__connector",
  );
  assert.ok(
    connectors.every(
      (connector) =>
        connector.dataset.failurePath === undefined &&
        connector.dataset.failureBoundary === undefined,
    ),
  );
  assert.deepEqual(
    connectors.map((connector) => connector.attributes.get("marker-end")),
    [
      "url(#django-ray-workflow-graph-arrow)",
      "url(#django-ray-workflow-graph-arrow)",
      "url(#django-ray-workflow-graph-arrow)",
      "url(#django-ray-workflow-graph-arrow)",
    ],
  );
  assert.equal(
    graphNodeLinks(app).filter((node) => node.dataset.state === "FAILED")
      .length,
    3,
  );
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
      payload: validGraphPayload({ schema_version: 1 }),
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
      name: "unsupported output preview value",
      payload: validGraphPayload({
        nodes: [
          {
            ...baseNodes[0],
            output_preview: {
              schema_version: 1,
              availability: "UNSUPPORTED",
              value: { leaked: true },
            },
          },
          ...baseNodes.slice(1),
        ],
      }),
    },
    {
      name: "over-deep output preview",
      payload: validGraphPayload({
        nodes: [
          {
            ...baseNodes[0],
            output_preview: {
              schema_version: 1,
              availability: "AVAILABLE",
              value: { a: { b: { c: { d: { e: true } } } } },
            },
          },
          ...baseNodes.slice(1),
        ],
      }),
    },
    {
      name: "available output preview containing a redaction marker",
      payload: validGraphPayload({
        nodes: [
          {
            ...baseNodes[0],
            output_preview: {
              schema_version: 1,
              availability: "AVAILABLE",
              value: { status: "[REDACTED]" },
            },
          },
          ...baseNodes.slice(1),
        ],
      }),
    },
    {
      name: "redacted output preview without redaction evidence",
      payload: validGraphPayload({
        nodes: [
          {
            ...baseNodes[0],
            output_preview: {
              schema_version: 1,
              availability: "REDACTED",
              value: { status: "safe" },
            },
          },
          ...baseNodes.slice(1),
        ],
      }),
    },
    {
      name: "unsafe integer output preview",
      payload: validGraphPayload({
        nodes: [
          {
            ...baseNodes[0],
            output_preview: {
              schema_version: 1,
              availability: "AVAILABLE",
              value: Number.MAX_SAFE_INTEGER + 1,
            },
          },
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
        app.details.textContent.includes("private_runtime_value"),
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

test("non-value and redacted preview wording is independent of node state", async () => {
  const presentations = new Map([
    [
      "NOT_REQUESTED",
      "Output: Not requested \u2014 value not retained in workflow diagnostics",
    ],
    ["PENDING", "Output: Preview pending \u2014 node has not reported a value"],
    [
      "UNAVAILABLE",
      "Output: Preview unavailable \u2014 no retained diagnostic value",
    ],
    [
      "REDACTED",
      "Output: Redacted preview \u2014 value withheld by redaction policy",
    ],
  ]);

  for (const [availability, expected] of presentations) {
    for (const state of ["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]) {
      const app = loadDiagnostics({
        fetchResponses: [
          response(200, validPayload()),
          response(
            200,
            validGraphPayload({
              nodes: [
                {
                  id: `${availability.toLowerCase()}-${state.toLowerCase()}`,
                  label: "Availability presentation",
                  kind: "task",
                  state,
                  message: null,
                  error: state === "FAILED" ? "bounded failure" : null,
                  failure_path: state === "FAILED",
                  output_preview: {
                    schema_version: 1,
                    availability,
                    value: availability === "REDACTED" ? "[REDACTED]" : null,
                  },
                },
              ],
              edges: [],
            }),
          ),
        ],
      });

      await app.toggle(true);
      await app.toggleGraph(true);

      assert.equal(graphNodeOutput(graphNodeLinks(app)[0]), expected);
    }
  }
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
  assert.match(
    graphStyles,
    /\.django-ray-workflow-graph__stage-nodes \{\s+grid-template-columns: minmax\(0, 1fr\)/,
  );
  assert.match(
    graphStyles,
    /\.django-ray-workflow-graph__connectors \{\s+display: none;/,
  );
  assert.match(
    graphStyles,
    /minmax\(min\(15rem, 100%\), 1fr\)/,
  );
  assert.match(
    graphStyles,
    /\.django-ray-workflow-graph__diagram \{[\s\S]*?overflow: hidden;[\s\S]*?position: relative;/,
  );
  assert.match(
    graphStyles,
    /node\[data-failure-origin="true"\] \{\s+border-left-color: var\(--django-ray-live-danger-border\);\s+box-shadow:/,
  );
  assert.match(
    graphStyles,
    /node\[data-failure-origin="true"\]\s+\.django-ray-workflow-graph__state\[data-state="FAILED"\]/,
  );
  assert.match(
    graphStyles,
    /django-ray-workflow-graph__node-output \{[\s\S]*?grid-column: 1 \/ -1;/,
  );
  assert.match(
    graphStyles,
    /django-ray-workflow-graph__node-output-label \{[\s\S]*?font-weight: 700;/,
  );
  assert.match(
    graphStyles,
    /node-output\[data-has-preview="true"\][\s\S]*?font-family: var\(--font-family-monospace/,
  );
  assert.equal(graphStyles.includes("failure-boundary"), false);
  assert.equal(graphStyles.includes("failure-arrow"), false);
  assert.equal(graphStyles.includes("danger-connector"), false);
  assert.ok(
    contrastAgainstWhite(cssHexToken("--django-ray-live-danger-fg")) >= 3,
  );
  assert.ok(
    contrastAgainstWhite(cssHexToken("--django-ray-live-action-fg")) >= 4.5,
  );
  const defaultFocusIndicator = cssHexToken(
    "--django-ray-live-accent-strong",
  );
  assert.ok(contrastRatio(defaultFocusIndicator, "#ffffff") >= 3);
  assert.ok(contrastRatio(defaultFocusIndicator, "#16171a") >= 3);
  assert.match(graphStyles, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(graphStyles, /django-ray-workflow-graph__summary-arrow/);
  assert.match(
    graphStyles,
    /outline: 3px solid var\(--django-ray-live-accent-strong\)/,
  );
  assert.match(
    graphStyles,
    /\.django-ray-workflow-graph__node:focus-visible \{[\s\S]*?outline-offset: -3px;/,
  );
});

test("graph styles remain usable without the testproject Unfold theme", () => {
  assert.doesNotMatch(graphStyles, /\.unfold(?:\b|[-_])/i);
  assert.match(
    graphStyles,
    /--django-ray-live-bg: var\(--body-bg, #fff\);/,
  );
  assert.match(
    graphStyles,
    /--django-ray-live-heading: var\(--body-fg, #0f172a\);/,
  );
  assert.match(
    graphStyles,
    /--django-ray-live-border-strong: var\(--hairline-color, #e2e8f0\);/,
  );
  assert.match(
    graphStyles,
    /incoming\[data-root="true"\] \{[\s\S]*?background: var\(--django-ray-live-action-bg\);[\s\S]*?border: 1px solid var\(--django-ray-live-action-border\);[\s\S]*?color: var\(--django-ray-live-action-fg\);/,
  );
  assert.ok(
    contrastRatio(
      cssHexToken("--django-ray-live-action-fg"),
      cssHexToken("--django-ray-live-action-bg"),
    ) >= 4.5,
  );
});
