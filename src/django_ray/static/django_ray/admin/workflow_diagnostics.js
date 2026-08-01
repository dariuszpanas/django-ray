"use strict";

(() => {
  const disclosure = document.getElementById("django-ray-workflow-diagnostics");
  if (!disclosure) {
    return;
  }

  const statusNode = disclosure.querySelector(
    "[data-workflow-diagnostics-status]",
  );
  const contentNode = disclosure.querySelector(
    "[data-workflow-diagnostics-content]",
  );
  if (!statusNode || !contentNode) {
    return;
  }

  const endpoints = {
    diagnostics: disclosure.dataset.diagnosticsUrl ?? "",
    graph: disclosure.dataset.graphUrl ?? "",
    planDownload: disclosure.dataset.planDownloadUrl ?? "",
    selectionDownload: disclosure.dataset.selectionDownloadUrl ?? "",
    topologyNodes: disclosure.dataset.topologyNodesUrl ?? "",
    topologyEdges: disclosure.dataset.topologyEdgesUrl ?? "",
    nodeDetails: disclosure.dataset.nodeDetailsUrl ?? "",
    nodeDetail: disclosure.dataset.nodeDetailUrl ?? "",
  };
  const pinnedAttemptNumber = disclosure.dataset.pinnedAttemptNumber ?? "";
  const pinnedAttemptLabel = /^[1-9][0-9]*$/.test(pinnedAttemptNumber)
    ? `Attempt ${pinnedAttemptNumber}`
    : "Page-rendered attempt";
  const newerAttemptGuidance =
    "Reload the task page to inspect a newer live attempt.";
  const graphResizeObservers = new WeakMap();
  let requested = false;

  const isRecord = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);

  const graphStatuses = new Set([
    "AVAILABLE",
    "NOT_REPORTED",
    "UNSUPPORTED",
    "TRUNCATED",
    "UNAVAILABLE",
    "LIMIT_EXCEEDED",
    "CORRUPT",
  ]);
  const graphNodeKinds = new Set(["task", "map"]);
  const graphNodeStates = new Set([
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
  ]);
  const graphStatePresentation = {
    PENDING: { symbol: "\u25cb", text: "Pending" },
    RUNNING: { symbol: "\u25b6", text: "Running" },
    SUCCEEDED: { symbol: "\u2713", text: "Succeeded" },
    FAILED: { symbol: "!", text: "Failed" },
  };
  const graphOutputPresentation = {
    PENDING: "Pending \u2014 node has not started",
    RUNNING: "Pending \u2014 node is still running",
    SUCCEEDED: "Completed \u2014 value not retained in workflow diagnostics",
    FAILED: "Unavailable \u2014 node failed",
  };
  const graphKindPresentation = {
    task: { symbol: "\u25a1", text: "Task" },
    map: { symbol: "\u25c7", text: "Aggregate map" },
  };
  const graphLimits = {
    nodes: 100,
    edges: 256,
    details: 100,
    response_bytes: 131072,
  };
  const graphTextLimits = {
    rootMessageBytes: 256,
    nodeIdBytes: 256,
    nodeLabelBytes: 512,
    nodeDetailBytes: 2048,
  };

  const hasExactKeys = (value, expectedKeys) => {
    if (!isRecord(value)) {
      return false;
    }
    const actual = Object.keys(value).sort();
    const expected = [...expectedKeys].sort();
    return (
      actual.length === expected.length &&
      actual.every((key, index) => key === expected[index])
    );
  };

  const isBoundedText = (
    value,
    maximumBytes,
    { nullable = false, nonempty = false } = {},
  ) =>
    (nullable && value === null) ||
    (typeof value === "string" &&
      (!nonempty || value.length > 0) &&
      new TextEncoder().encode(value).byteLength <= maximumBytes);

  const isBoundedInteger = (value, maximum) =>
    Number.isInteger(value) && value >= 0 && value <= maximum;

  const asText = (value, fallback = "\u2014") => {
    if (value === null || value === undefined || value === "") {
      return fallback;
    }
    return String(value);
  };

  const asIdentifier = (value, fallback = "\u2014") => {
    if (typeof value !== "string" || value.length === 0) {
      return fallback;
    }
    return value.replaceAll("_", " ");
  };

  const asBoolean = (value) => {
    if (value === true) {
      return "Yes";
    }
    if (value === false) {
      return "No";
    }
    return "\u2014";
  };

  const element = (tagName, className, text) => {
    const node = document.createElement(tagName);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  };

  const addFact = (grid, label, value, options = {}) => {
    const wrapper = element(
      "div",
      `django-ray-workflow__fact${
        options.wide ? " django-ray-workflow__fact--wide" : ""
      }`,
    );
    wrapper.append(
      element("dt", "", label),
      element("dd", options.valueClass ?? "", value),
    );
    grid.append(wrapper);
    return wrapper;
  };

  const addBadge = (header, value) => {
    const status = asText(value, "UNKNOWN");
    const badge = element(
      "span",
      "django-ray-workflow__badge",
      asIdentifier(status, "Unknown"),
    );
    badge.dataset.status = status;
    header.append(badge);
  };

  const section = (title, status) => {
    const wrapper = element("section", "django-ray-workflow__section");
    const header = element("div", "django-ray-workflow__section-header");
    header.append(element("h3", "", title));
    addBadge(header, status);
    wrapper.append(header);
    return wrapper;
  };

  const addChips = (wrapper, values) => {
    const safeValues = Array.isArray(values)
      ? values.filter(
          (value) =>
            typeof value === "string" ||
            typeof value === "number" ||
            typeof value === "boolean",
        )
      : [];
    if (safeValues.length === 0) {
      return;
    }
    const list = element("ul", "django-ray-workflow__chips");
    for (const value of safeValues) {
      list.append(
        element("li", "django-ray-workflow__chip", asIdentifier(value)),
      );
    }
    wrapper.append(list);
  };

  const addAction = (actions, label, href, kind) => {
    if (!href) {
      return;
    }
    const link = element("a", "", label);
    link.href = href;
    link.dataset.actionKind = kind;
    actions.append(link);
  };

  const setStatus = (message, state = "ready") => {
    statusNode.textContent = message;
    statusNode.dataset.state = state;
    statusNode.setAttribute("aria-busy", state === "loading" ? "true" : "false");
  };

  const progressMessage = (progress) => {
    if (typeof progress.message === "string" && progress.message.length > 0) {
      return progress.message;
    }
    const messages = {
      AVAILABLE: "Bounded workflow progress and topology are available.",
      TRUNCATED:
        "Bounded workflow progress is available, with some detail intentionally omitted.",
      LEGACY_ONLY:
        "Only a legacy workflow snapshot is retained; bounded topology is unavailable.",
      DISABLED: "Workflow progress reporting was disabled by policy.",
      NOT_REPORTED: "No workflow progress snapshot has been reported yet.",
      REQUESTED_NOT_REPORTED:
        "Workflow detail was requested, but no snapshot has been reported yet.",
      REQUESTED_MISSING:
        "Workflow detail was requested, but no usable snapshot was retained.",
      OMITTED_BY_POLICY:
        "Workflow progress detail was intentionally omitted by reporting policy.",
      TERMINAL_ONLY:
        "A terminal workflow summary is available; topology and node detail were omitted by policy.",
      TERMINAL_ONLY_PENDING:
        "Terminal-only reporting waits for workflow completion; no live node detail is collected.",
      TERMINAL_ONLY_MISSING:
        "Terminal-only reporting was selected, but its terminal summary was not captured.",
      EXPIRED: "The retained workflow detail has expired.",
      MISSING: "Workflow detail was requested but no usable snapshot was retained.",
      CORRUPT:
        "Retained workflow detail failed validation and cannot be displayed safely.",
    };
    return (
      messages[progress.state] ??
      messages[progress.availability] ??
      "No bounded workflow progress detail is available."
    );
  };

  const renderPlan = (plan) => {
    const wrapper = section("Verified plan and strategy", plan.status);
    if (plan.status !== "AVAILABLE") {
      const messages = {
        CORRUPT:
          "The stored workflow plan failed verification and structured values were not displayed.",
        NOT_RECORDED:
          "No workflow plan was recorded for this execution.",
        MISSING: "No stored workflow plan is available for this execution.",
        ABSENT: "This execution has no stored workflow plan.",
      };
      wrapper.append(
        element(
          "p",
          "django-ray-workflow__notice",
          messages[plan.status] ??
            "Verified workflow plan diagnostics are unavailable for this execution.",
        ),
      );
      return wrapper;
    }

    const facts = element("dl", "django-ray-workflow__facts");
    addFact(facts, "Definition", asText(plan.definition_name));
    addFact(facts, "Revision", asText(plan.definition_revision));
    addFact(facts, "Topology", asIdentifier(plan.topology_class));
    addFact(facts, "Declared nodes", asText(plan.declared_node_count));
    addFact(facts, "Retry safe", asBoolean(plan.retry_safe));
    addFact(facts, "Requested strategy", asIdentifier(plan.requested_policy));
    addFact(facts, "Selected strategy", asIdentifier(plan.selected_strategy));
    addFact(facts, "Reporting policy", asIdentifier(plan.reporting_policy));

    const fingerprintFact = addFact(
      facts,
      "Plan fingerprint",
      "",
      { wide: true },
    );
    const fingerprintValue = fingerprintFact.querySelector("dd");
    const fingerprint = asText(plan.fingerprint, "");
    const compactFingerprint = asText(
      plan.fingerprint_compact,
      fingerprint || "\u2014",
    );
    const fingerprintRow = element(
      "span",
      "django-ray-workflow__fingerprint",
    );
    fingerprintRow.append(element("code", "", compactFingerprint));
    if (fingerprint && fingerprint !== "[REDACTED]") {
      const copyButton = element(
        "button",
        "django-ray-workflow__copy",
        "Copy full fingerprint",
      );
      copyButton.type = "button";
      copyButton.addEventListener("click", async () => {
        copyButton.disabled = true;
        try {
          const clipboard = window.navigator?.clipboard;
          if (!clipboard || typeof clipboard.writeText !== "function") {
            throw new Error("Clipboard API unavailable");
          }
          await clipboard.writeText(fingerprint);
          setStatus("Full plan fingerprint copied.");
        } catch (error) {
          setStatus("The fingerprint could not be copied.", "error");
        } finally {
          copyButton.disabled = false;
        }
      });
      fingerprintRow.append(copyButton);
    }
    fingerprintValue.replaceChildren(fingerprintRow);
    wrapper.append(facts);

    const eligibleLabel = element(
      "p",
      "django-ray-workflow__notice",
      "Eligible strategies",
    );
    wrapper.append(eligibleLabel);
    addChips(wrapper, plan.eligible_strategies);

    const rejectionCounts = isRecord(plan.rejection_counts)
      ? Object.entries(plan.rejection_counts)
          .filter(
            ([code, count]) =>
              code.length > 0 &&
              (typeof count === "number" || typeof count === "string"),
          )
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([code, count]) => `${asIdentifier(code)}: ${asText(count)}`)
      : [];
    const totalRejections = asText(plan.total_rejections, "0");
    const retainedRejections = asText(plan.retained_rejections, "0");
    const unretainedRejections = asText(plan.unretained_rejections, "0");
    wrapper.append(
      element(
        "p",
        "django-ray-workflow__notice",
        `${totalRejections} strategy rejections; ${retainedRejections} retained and ${unretainedRejections} omitted from bounded diagnostics.`,
      ),
    );
    addChips(wrapper, rejectionCounts);

    const actions = element("nav", "django-ray-workflow__actions");
    actions.setAttribute("aria-label", "Verified workflow plan downloads");
    addAction(actions, "Download plan JSON", endpoints.planDownload, "download");
    addAction(
      actions,
      "Download selection JSON",
      endpoints.selectionDownload,
      "download",
    );
    if (actions.children.length > 0) {
      wrapper.append(actions);
    }
    return wrapper;
  };

  const validateGraphNode = (node) => {
    const baseKeys = [
      "id",
      "label",
      "kind",
      "state",
      "message",
      "error",
      "failure_path",
    ];
    const expectedKeys =
      isRecord(node) && node.kind === "map"
        ? [...baseKeys, "fanout"]
        : baseKeys;
    if (
      !hasExactKeys(node, expectedKeys) ||
      !isBoundedText(node.id, graphTextLimits.nodeIdBytes, {
        nonempty: true,
      }) ||
      !isBoundedText(node.label, graphTextLimits.nodeLabelBytes) ||
      !graphNodeKinds.has(node.kind) ||
      !graphNodeStates.has(node.state) ||
      !isBoundedText(node.message, graphTextLimits.nodeDetailBytes, {
        nullable: true,
      }) ||
      !isBoundedText(node.error, graphTextLimits.nodeDetailBytes, {
        nullable: true,
      }) ||
      typeof node.failure_path !== "boolean" ||
      (node.state === "FAILED") !== (node.error !== null)
    ) {
      throw new Error("Invalid workflow graph node");
    }
    if (node.kind !== "map") {
      return;
    }
    const fanout = node.fanout;
    if (
      !hasExactKeys(fanout, [
        "submitted_items",
        "completed_items",
        "in_flight_items",
        "input_exhausted",
      ]) ||
      !isBoundedInteger(fanout.submitted_items, Number.MAX_SAFE_INTEGER) ||
      !isBoundedInteger(fanout.completed_items, fanout.submitted_items) ||
      !isBoundedInteger(fanout.in_flight_items, Number.MAX_SAFE_INTEGER) ||
      fanout.in_flight_items !==
        fanout.submitted_items - fanout.completed_items ||
      typeof fanout.input_exhausted !== "boolean"
    ) {
      throw new Error("Invalid aggregate-map graph node");
    }
  };

  const validateGraphPayload = (payload) => {
    if (
      !hasExactKeys(payload, [
        "schema",
        "schema_version",
        "status",
        "message",
        "complete",
        "counts",
        "limits",
        "nodes",
        "edges",
      ]) ||
      payload.schema !== "django-ray.admin-workflow-graph" ||
      payload.schema_version !== 1 ||
      !graphStatuses.has(payload.status) ||
      !isBoundedText(
        payload.message,
        graphTextLimits.rootMessageBytes,
        { nonempty: true },
      ) ||
      typeof payload.complete !== "boolean" ||
      !hasExactKeys(payload.counts, ["nodes", "edges"]) ||
      !hasExactKeys(payload.limits, [
        "nodes",
        "edges",
        "details",
        "response_bytes",
      ]) ||
      Object.entries(graphLimits).some(
        ([key, value]) => payload.limits[key] !== value,
      ) ||
      !isBoundedInteger(payload.counts.nodes, graphLimits.nodes) ||
      !isBoundedInteger(payload.counts.edges, graphLimits.edges) ||
      !Array.isArray(payload.nodes) ||
      !Array.isArray(payload.edges) ||
      payload.nodes.length !== payload.counts.nodes ||
      payload.edges.length !== payload.counts.edges ||
      new TextEncoder().encode(JSON.stringify(payload)).byteLength >
        graphLimits.response_bytes
    ) {
      throw new Error("Invalid workflow graph payload");
    }

    if (payload.status !== "AVAILABLE") {
      if (
        payload.complete ||
        payload.counts.nodes !== 0 ||
        payload.counts.edges !== 0 ||
        payload.nodes.length !== 0 ||
        payload.edges.length !== 0
      ) {
        throw new Error("Partial workflow graph payload");
      }
      return {
        incoming: new Map(),
        layers: [],
        nodesById: new Map(),
        payload,
      };
    }
    if (!payload.complete) {
      throw new Error("Incomplete workflow graph payload");
    }
    if (payload.counts.nodes === 0) {
      throw new Error("Empty workflow graph payload");
    }

    const nodesById = new Map();
    const nodeIndexes = new Map();
    payload.nodes.forEach((node, index) => {
      validateGraphNode(node);
      if (nodesById.has(node.id)) {
        throw new Error("Duplicate workflow graph node");
      }
      nodesById.set(node.id, node);
      nodeIndexes.set(node.id, index);
    });

    const incoming = new Map(
      payload.nodes.map((node) => [node.id, []]),
    );
    const retainedEdges = new Set();
    for (const edge of payload.edges) {
      if (
        !hasExactKeys(edge, ["source", "target"]) ||
        !isBoundedText(edge.source, graphTextLimits.nodeIdBytes, {
          nonempty: true,
        }) ||
        !isBoundedText(edge.target, graphTextLimits.nodeIdBytes, {
          nonempty: true,
        }) ||
        !nodesById.has(edge.source) ||
        !nodesById.has(edge.target) ||
        nodeIndexes.get(edge.source) >= nodeIndexes.get(edge.target)
      ) {
        throw new Error("Invalid workflow graph edge");
      }
      const edgeKey = JSON.stringify([edge.source, edge.target]);
      if (retainedEdges.has(edgeKey)) {
        throw new Error("Duplicate workflow graph edge");
      }
      retainedEdges.add(edgeKey);
      incoming.get(edge.target).push(edge.source);
    }
    for (const sources of incoming.values()) {
      sources.sort(
        (left, right) => nodeIndexes.get(left) - nodeIndexes.get(right),
      );
    }
    const layerById = new Map();
    const layers = [];
    for (const node of payload.nodes) {
      const layer = incoming
        .get(node.id)
        .reduce(
          (maximum, sourceId) =>
            Math.max(maximum, layerById.get(sourceId) + 1),
          0,
        );
      layerById.set(node.id, layer);
      if (!layers[layer]) {
        layers[layer] = [];
      }
      layers[layer].push(node);
    }
    return { incoming, layers, nodesById, payload };
  };

  const nodeDetailUrl = (nodeId) => {
    if (!endpoints.nodeDetail) {
      throw new Error("Node detail endpoint unavailable");
    }
    const fragmentIndex = endpoints.nodeDetail.indexOf("#");
    const fragment =
      fragmentIndex === -1
        ? ""
        : endpoints.nodeDetail.slice(fragmentIndex);
    const withoutFragment =
      fragmentIndex === -1
        ? endpoints.nodeDetail
        : endpoints.nodeDetail.slice(0, fragmentIndex);
    const queryIndex = withoutFragment.indexOf("?");
    const path =
      queryIndex === -1
        ? withoutFragment
        : withoutFragment.slice(0, queryIndex);
    const query =
      queryIndex === -1 ? "" : withoutFragment.slice(queryIndex + 1);
    const parameters = new URLSearchParams(query);
    parameters.set("node_id", nodeId);
    return `${path}?${parameters.toString()}${fragment}`;
  };

  const graphFallbackActions = () => {
    const actions = element(
      "nav",
      "django-ray-workflow__actions django-ray-workflow-graph__fallbacks",
    );
    actions.setAttribute("aria-label", "Bounded workflow JSON fallback views");
    addAction(
      actions,
      "Topology nodes JSON",
      endpoints.topologyNodes,
      "topology",
    );
    addAction(
      actions,
      "Topology edges JSON",
      endpoints.topologyEdges,
      "topology",
    );
    addAction(
      actions,
      "Node details JSON",
      endpoints.nodeDetails,
      "topology",
    );
    return actions;
  };

  const graphConnectorOverlay = (payload) => {
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("class", "django-ray-workflow-graph__connectors");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("preserveAspectRatio", "none");

    const definitions = document.createElementNS(namespace, "defs");
    const marker = document.createElementNS(namespace, "marker");
    marker.setAttribute("id", "django-ray-workflow-graph-arrow");
    marker.setAttribute("markerHeight", "7");
    marker.setAttribute("markerWidth", "7");
    marker.setAttribute("orient", "auto");
    marker.setAttribute("refX", "6");
    marker.setAttribute("refY", "3.5");
    marker.setAttribute("viewBox", "0 0 7 7");
    const arrow = document.createElementNS(namespace, "path");
    arrow.setAttribute("class", "django-ray-workflow-graph__connector-arrow");
    arrow.setAttribute("d", "M0 0L7 3.5L0 7Z");
    marker.append(arrow);
    definitions.append(marker);
    svg.append(definitions);

    const paths = [];
    for (const edge of payload.edges) {
      const path = document.createElementNS(namespace, "path");
      path.setAttribute("class", "django-ray-workflow-graph__connector");
      path.dataset.source = edge.source;
      path.dataset.target = edge.target;
      path.setAttribute("marker-end", "url(#django-ray-workflow-graph-arrow)");
      svg.append(path);
      paths.push({ edge, path });
    }
    return { paths, svg };
  };

  const roundedConnectorCoordinate = (value) =>
    Math.round(value * 10) / 10;

  const drawGraphConnectors = (diagram, svg, paths, cardsById) => {
    const diagramBounds = diagram.getBoundingClientRect();
    if (
      !Number.isFinite(diagramBounds.width) ||
      !Number.isFinite(diagramBounds.height) ||
      diagramBounds.width <= 0 ||
      diagramBounds.height <= 0
    ) {
      return;
    }
    svg.setAttribute(
      "viewBox",
      `0 0 ${roundedConnectorCoordinate(
        diagramBounds.width,
      )} ${roundedConnectorCoordinate(diagramBounds.height)}`,
    );
    for (const { edge, path } of paths) {
      const sourceBounds = cardsById.get(edge.source).getBoundingClientRect();
      const targetBounds = cardsById.get(edge.target).getBoundingClientRect();
      const sourceX = roundedConnectorCoordinate(
        sourceBounds.left - diagramBounds.left + sourceBounds.width / 2,
      );
      const sourceY = roundedConnectorCoordinate(
        sourceBounds.bottom - diagramBounds.top,
      );
      const targetX = roundedConnectorCoordinate(
        targetBounds.left - diagramBounds.left + targetBounds.width / 2,
      );
      const targetY = roundedConnectorCoordinate(
        targetBounds.top - diagramBounds.top,
      );
      const middleY = roundedConnectorCoordinate(
        sourceY + (targetY - sourceY) / 2,
      );
      path.setAttribute(
        "d",
        `M${sourceX} ${sourceY}C${sourceX} ${middleY} ${targetX} ${middleY} ${targetX} ${targetY}`,
      );
    }
  };

  const observeGraphConnectors = (diagram, svg, paths, cardsById) => {
    let framePending = false;
    const scheduleDraw = () => {
      if (framePending) {
        return;
      }
      framePending = true;
      const draw = () => {
        framePending = false;
        drawGraphConnectors(diagram, svg, paths, cardsById);
      };
      if (typeof window.requestAnimationFrame === "function") {
        window.requestAnimationFrame(draw);
      } else {
        draw();
      }
    };
    scheduleDraw();
    if (typeof window.ResizeObserver === "function") {
      const resizeObserver = new window.ResizeObserver(scheduleDraw);
      resizeObserver.observe(diagram);
      graphResizeObservers.set(diagram, resizeObserver);
    } else if (typeof window.addEventListener === "function") {
      window.addEventListener("resize", scheduleDraw);
    }
  };

  const graphLegend = () => {
    const legend = element("ul", "django-ray-workflow-graph__legend");
    legend.setAttribute("aria-label", "Workflow node shapes");
    for (const presentation of Object.values(graphKindPresentation)) {
      const item = element(
        "li",
        "django-ray-workflow-graph__legend-item",
      );
      const symbol = element("span", "", presentation.symbol);
      symbol.setAttribute("aria-hidden", "true");
      item.append(symbol, element("span", "", presentation.text));
      legend.append(item);
    }
    return legend;
  };

  const graphNodeCard = (node, layer, position, descriptionId) => {
    const link = element("a", "django-ray-workflow-graph__node");
    link.href = nodeDetailUrl(node.id);
    link.dataset.kind = node.kind;
    link.dataset.layer = String(layer);
    link.dataset.position = String(position);
    link.dataset.state = node.state;
    link.setAttribute("aria-describedby", descriptionId);
    if (node.failure_path) {
      link.dataset.failurePath = "true";
    }
    if (node.state === "FAILED" && node.failure_path) {
      link.dataset.failureOrigin = "true";
    }

    const identity = element("span", "");
    identity.append(
      element(
        "span",
        "django-ray-workflow-graph__node-title",
        node.label || node.id,
      ),
      element(
        "span",
        "django-ray-workflow-graph__node-id",
        `Node ID: ${node.id}`,
      ),
    );

    const statePresentation = graphStatePresentation[node.state];
    const state = element("span", "django-ray-workflow-graph__state");
    state.dataset.state = node.state;
    const stateSymbol = element("span", "", statePresentation.symbol);
    stateSymbol.setAttribute("aria-hidden", "true");
    state.append(stateSymbol, element("span", "", statePresentation.text));
    link.append(identity, state);

    const kindPresentation = graphKindPresentation[node.kind];
    const metadata = element(
      "span",
      "django-ray-workflow-graph__node-meta",
    );
    const kind = element("span", "");
    const kindSymbol = element("span", "", kindPresentation.symbol);
    kindSymbol.setAttribute("aria-hidden", "true");
    kind.append(kindSymbol, element("span", "", ` ${kindPresentation.text}`));
    metadata.append(kind);
    if (node.failure_path) {
      metadata.append(
        element(
          "span",
          "",
          node.state === "FAILED" ? "Failure origin" : "Upstream of failure",
        ),
      );
    }
    if (node.kind === "map") {
      metadata.append(
        element(
          "span",
          "",
          `${node.fanout.completed_items} of ${node.fanout.submitted_items} items completed`,
        ),
        element(
          "span",
          "",
          `${node.fanout.in_flight_items} in flight`,
        ),
        element(
          "span",
          "",
          node.fanout.input_exhausted
            ? "Input exhausted"
            : "Input still open",
        ),
      );
    }
    link.append(metadata);

    const output = element(
      "span",
      "django-ray-workflow-graph__node-output",
    );
    output.append(
      element(
        "span",
        "django-ray-workflow-graph__node-output-label",
        "Output: ",
      ),
      element(
        "span",
        "django-ray-workflow-graph__node-output-value",
        graphOutputPresentation[node.state],
      ),
    );
    link.append(output);

    if (node.message !== null) {
      link.append(
        element(
          "span",
          "django-ray-workflow-graph__node-message",
          node.message,
        ),
      );
    }
    if (node.error !== null) {
      const error = element(
        "span",
        "django-ray-workflow-graph__node-message",
        `Failure: ${node.error}`,
      );
      error.dataset.failure = "true";
      link.append(error);
    }
    return link;
  };

  const renderGraph = (validated, content) => {
    const { incoming, layers, nodesById, payload } = validated;
    const diagram = element("div", "django-ray-workflow-graph__diagram");
    const list = element("ol", "django-ray-workflow-graph__nodes");
    list.setAttribute("aria-label", "Workflow stages in topological order");
    const cardsById = new Map();
    let nodePosition = 0;
    layers.forEach((layerNodes, layerIndex) => {
      const stage = element("li", "django-ray-workflow-graph__stage");
      stage.dataset.layer = String(layerIndex);
      const stageHeader = element(
        "div",
        "django-ray-workflow-graph__stage-header",
      );
      stageHeader.append(
        element(
          "h4",
          "django-ray-workflow-graph__stage-title",
          `Stage ${layerIndex + 1}`,
        ),
        element(
          "span",
          "django-ray-workflow-graph__stage-copy",
          `${layerNodes.length} ${
            layerNodes.length === 1 ? "node" : "parallel nodes"
          }`,
        ),
      );
      const stageNodes = element(
        "ul",
        "django-ray-workflow-graph__stage-nodes",
      );
      stageNodes.setAttribute(
        "aria-label",
        `Workflow stage ${layerIndex + 1}`,
      );
      layerNodes.forEach((node, layerPosition) => {
        const row = element("li", "django-ray-workflow-graph__node-row");
        const incomingNode = element(
          "div",
          "django-ray-workflow-graph__incoming",
        );
        const descriptionId = `django-ray-workflow-graph-predecessors-${nodePosition}`;
        incomingNode.setAttribute("id", descriptionId);
        const sources = incoming.get(node.id);
        if (sources.length === 0) {
          incomingNode.dataset.root = "true";
          incomingNode.append(element("p", "", "Workflow entry"));
        } else {
          const sourceLabels = sources.map((sourceId) => {
            const source = nodesById.get(sourceId);
            return source.label
              ? `${source.label} (${source.id})`
              : source.id;
          });
          incomingNode.append(
            element(
              "p",
              "",
              `Incoming from ${sourceLabels.join(", ")}`,
            ),
          );
        }
        const card = graphNodeCard(
          node,
          layerIndex,
          layerPosition,
          descriptionId,
        );
        cardsById.set(node.id, card);
        row.append(incomingNode, card);
        stageNodes.append(row);
        nodePosition += 1;
      });
      stage.append(stageHeader, stageNodes);
      list.append(stage);
    });
    const connectors = graphConnectorOverlay(payload);
    diagram.append(connectors.svg, list);
    content.replaceChildren(graphLegend(), diagram);
    content.hidden = false;
    observeGraphConnectors(
      diagram,
      connectors.svg,
      connectors.paths,
      cardsById,
    );
  };

  const graphDisclosure = () => {
    const graph = element("details", "django-ray-workflow-graph");
    const summary = element(
      "summary",
      "django-ray-workflow-graph__summary",
    );
    const summaryText = element("span", "");
    const summaryCopy = element(
      "span",
      "django-ray-workflow-graph__summary-copy",
    );
    const summaryCopyMessage = element(
      "span",
      "",
      "Open to load the bounded graph.",
    );
    summaryCopy.append(
      summaryCopyMessage,
      element("span", "", ` ${newerAttemptGuidance}`),
    );
    summaryText.append(
      element(
        "span",
        "django-ray-workflow-graph__summary-title",
        `Execution graph \u2014 ${pinnedAttemptLabel}`,
      ),
      summaryCopy,
    );
    const arrow = element(
      "span",
      "django-ray-workflow-graph__summary-arrow",
      "\u25b8",
    );
    arrow.setAttribute("aria-hidden", "true");
    summary.append(summaryText, arrow);

    const body = element("div", "django-ray-workflow-graph__body");
    const graphStatus = element(
      "p",
      "django-ray-workflow-graph__status",
    );
    graphStatus.setAttribute("role", "status");
    graphStatus.setAttribute("aria-live", "polite");
    graphStatus.setAttribute("aria-atomic", "true");
    const graphStatusMessage = element(
      "span",
      "",
      "Open this section to load the bounded workflow graph.",
    );
    graphStatus.append(
      element("span", "", `${pinnedAttemptLabel}. `),
      graphStatusMessage,
      element("span", "", ` ${newerAttemptGuidance}`),
    );
    const graphContent = element(
      "div",
      "django-ray-workflow-graph__content",
    );
    graphContent.hidden = true;
    body.append(graphStatus, graphContent);
    graph.append(summary, body);

    let graphRequested = false;
    let fallbackActions = null;
    const showFallbacks = () => {
      if (fallbackActions !== null) {
        return;
      }
      const fallbacks = graphFallbackActions();
      if (fallbacks.children.length > 0) {
        body.append(fallbacks);
        fallbackActions = fallbacks;
      }
    };
    const removeFallbacks = () => {
      if (fallbackActions === null) {
        return;
      }
      fallbackActions.remove();
      fallbackActions = null;
    };
    const setGraphStatus = (message, state = "ready") => {
      graphStatusMessage.textContent = message;
      graphStatus.dataset.state = state;
      graphStatus.setAttribute(
        "aria-busy",
        state === "loading" ? "true" : "false",
      );
    };
    const setGraphSummary = (message) => {
      summaryCopyMessage.textContent = message;
    };
    const recoverableFailure = (message) => {
      graphRequested = false;
      graphContent.hidden = true;
      showFallbacks();
      setGraphSummary("Unavailable; reopen to retry.");
      setGraphStatus(
        `${message} Close and reopen this graph to retry, or use the bounded JSON views below.`,
        "error",
      );
    };
    const loadGraph = async () => {
      if (!endpoints.graph) {
        showFallbacks();
        setGraphSummary("Unavailable.");
        setGraphStatus(
          "The workflow graph endpoint is unavailable. Use the bounded JSON views below.",
          "error",
        );
        return;
      }
      setGraphStatus("Loading the bounded workflow graph\u2026", "loading");
      try {
        const response = await window.fetch(endpoints.graph, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        const contentType = response.headers?.get("content-type") ?? "";
        if (response.redirected || [401, 403].includes(response.status)) {
          graphContent.hidden = true;
          showFallbacks();
          setGraphSummary("Authentication required.");
          setGraphStatus(
            "Workflow graph unavailable; reload after signing in again. The bounded JSON views remain available after authentication.",
            "error",
          );
          return;
        }
        if (response.status === 404) {
          graphContent.hidden = true;
          showFallbacks();
          setGraphSummary("No longer available.");
          setGraphStatus(
            "The workflow graph is no longer available for this execution. Use the bounded JSON views below.",
            "error",
          );
          return;
        }
        if (!contentType.includes("application/json")) {
          if (response.ok) {
            graphContent.hidden = true;
            showFallbacks();
            setGraphSummary("Authentication required.");
            setGraphStatus(
              "Workflow graph unavailable; reload after signing in again. No graph data was displayed.",
              "error",
            );
          } else {
            recoverableFailure("The workflow graph is temporarily unavailable.");
          }
          return;
        }

        const validated = validateGraphPayload(await response.json());
        const expectedStatusCode =
          validated.payload.status === "CORRUPT" ? 503 : 200;
        if (response.status !== expectedStatusCode) {
          throw new Error("Unexpected workflow graph response");
        }
        if (validated.payload.status !== "AVAILABLE") {
          graphContent.hidden = true;
          showFallbacks();
          setGraphSummary(`${asIdentifier(validated.payload.status)}.`);
          if (validated.payload.status === "NOT_REPORTED") {
            graphRequested = false;
            setGraphStatus(
              `${validated.payload.message} Close and reopen this graph after the workflow reaches a terminal state to try again, or use the bounded JSON views below.`,
              "warning",
            );
            return;
          }
          const presentationState = [
            "UNSUPPORTED",
            "TRUNCATED",
            "LIMIT_EXCEEDED",
          ].includes(validated.payload.status)
            ? "warning"
            : "error";
          setGraphStatus(
            `${validated.payload.message} Use the bounded JSON views below.`,
            presentationState,
          );
          return;
        }
        renderGraph(validated, graphContent);
        removeFallbacks();
        const { edges, nodes } = validated.payload.counts;
        setGraphSummary(
          `${nodes} ${nodes === 1 ? "node" : "nodes"}, ${edges} ${
            edges === 1 ? "connection" : "connections"
          }.`,
        );
        setGraphStatus(
          `${validated.payload.message} ${nodes} ${
            nodes === 1 ? "node" : "nodes"
          } and ${edges} ${edges === 1 ? "connection" : "connections"}.`,
        );
      } catch (error) {
        recoverableFailure("The workflow graph could not be displayed safely.");
      }
    };
    graph.addEventListener("toggle", () => {
      if (!graph.open || graphRequested) {
        return;
      }
      graphRequested = true;
      void loadGraph();
    });
    return graph;
  };

  const renderProgress = (progress, reportingPolicy, planStatus) => {
    const wrapper = section(
      "Progress and topology",
      progress.state ?? progress.availability,
    );
    wrapper.append(
      element(
        "p",
        "django-ray-workflow__notice",
        progressMessage(progress),
      ),
    );

    const facts = element("dl", "django-ray-workflow__facts");
    addFact(facts, "Progress status", asIdentifier(progress.state));
    if (
      typeof progress.workflow_state === "string" &&
      progress.workflow_state.length > 0
    ) {
      addFact(
        facts,
        progress.state === "TERMINAL_ONLY"
          ? "Terminal outcome"
          : "Workflow state",
        asIdentifier(progress.workflow_state),
      );
    }
    addFact(facts, "Detail availability", asIdentifier(progress.availability));
    addFact(facts, "Complete detail", asBoolean(progress.complete));
    wrapper.append(facts);

    if (
      Array.isArray(progress.truncation_reasons) &&
      progress.truncation_reasons.length > 0
    ) {
      wrapper.append(
        element(
          "p",
          "django-ray-workflow__notice",
          "Bounded detail was limited for these reasons",
        ),
      );
      addChips(wrapper, progress.truncation_reasons);
    }

    const terminalOnlyState =
      typeof progress.state === "string" &&
      progress.state.startsWith("TERMINAL_ONLY");
    const terminalOnlyPolicy =
      reportingPolicy === "terminal_only" ||
      progress.reporting_policy === "terminal_only";
    if (
      endpoints.graph &&
      planStatus === "AVAILABLE" &&
      !terminalOnlyPolicy &&
      !terminalOnlyState
    ) {
      wrapper.append(graphDisclosure());
      return wrapper;
    }

    const availableActions =
      !terminalOnlyPolicy && isRecord(progress.actions)
        ? progress.actions
        : {};
    const actions = element("nav", "django-ray-workflow__actions");
    actions.setAttribute("aria-label", "Available workflow topology views");
    if (availableActions.topology_nodes === true) {
      addAction(
        actions,
        "Topology nodes",
        endpoints.topologyNodes,
        "topology",
      );
    }
    if (availableActions.topology_edges === true) {
      addAction(
        actions,
        "Topology edges",
        endpoints.topologyEdges,
        "topology",
      );
    }
    if (availableActions.node_details === true) {
      addAction(actions, "Node details", endpoints.nodeDetails, "topology");
    }
    if (actions.children.length > 0) {
      wrapper.append(actions);
    } else {
      wrapper.append(
        element(
          "p",
          "django-ray-workflow__notice",
          "No bounded topology actions are available for this snapshot.",
        ),
      );
    }
    return wrapper;
  };

  const render = (payload) => {
    if (
      !isRecord(payload) ||
      payload.schema !== "django-ray.admin-workflow-diagnostics" ||
      payload.schema_version !== 1 ||
      !isRecord(payload.plan) ||
      !isRecord(payload.progress) ||
      typeof payload.plan.status !== "string"
    ) {
      throw new Error("Invalid workflow diagnostics payload");
    }
    contentNode.replaceChildren(
      renderPlan(payload.plan),
      renderProgress(
        payload.progress,
        payload.plan.reporting_policy,
        payload.plan.status,
      ),
    );
    contentNode.hidden = false;
    setStatus("Workflow diagnostics loaded.");
  };

  const load = async () => {
    if (!endpoints.diagnostics) {
      setStatus("Workflow diagnostics are unavailable for this execution.", "error");
      return;
    }
    setStatus("Loading workflow diagnostics\u2026", "loading");
    try {
      const response = await window.fetch(endpoints.diagnostics, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const contentType = response.headers?.get("content-type") ?? "";
      if (
        response.redirected ||
        [401, 403].includes(response.status)
      ) {
        setStatus(
          "Workflow diagnostics unavailable; reload after signing in again.",
          "error",
        );
        return;
      }
      if (response.status === 404) {
        setStatus(
          "Workflow diagnostics are no longer available for this execution.",
          "error",
        );
        return;
      }
      if (!response.ok) {
        requested = false;
        setStatus("Workflow diagnostics are temporarily unavailable.", "error");
        return;
      }
      if (!contentType.includes("application/json")) {
        setStatus(
          "Workflow diagnostics unavailable; reload after signing in again.",
          "error",
        );
        return;
      }
      render(await response.json());
    } catch (error) {
      requested = false;
      contentNode.hidden = true;
      setStatus("Workflow diagnostics could not be displayed safely.", "error");
    }
  };

  disclosure.addEventListener("toggle", () => {
    if (!disclosure.open || requested) {
      return;
    }
    requested = true;
    void load();
  });
})();
