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
    planDownload: disclosure.dataset.planDownloadUrl ?? "",
    selectionDownload: disclosure.dataset.selectionDownloadUrl ?? "",
    topologyNodes: disclosure.dataset.topologyNodesUrl ?? "",
    topologyEdges: disclosure.dataset.topologyEdgesUrl ?? "",
    nodeDetails: disclosure.dataset.nodeDetailsUrl ?? "",
  };
  let requested = false;

  const isRecord = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);

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

  const renderProgress = (progress) => {
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
    addFact(facts, "Workflow state", asIdentifier(progress.state));
    addFact(facts, "Detail availability", asIdentifier(progress.availability));
    addFact(facts, "Complete snapshot", asBoolean(progress.complete));
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

    const availableActions = isRecord(progress.actions)
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
      renderProgress(payload.progress),
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
