"use strict";

(() => {
  const panel = document.getElementById("django-ray-live-observability");
  if (!panel) {
    return;
  }

  const endpoint = panel.dataset.observabilityUrl;
  const stateNode = panel.querySelector('[data-field="state"]');
  const attemptNode = panel.querySelector('[data-field="attempt"]');
  const workflowNode = panel.querySelector('[data-field="workflow"]');
  const statusNode = panel.querySelector('[data-field="status"]');
  const indicatorNode = panel.querySelector('[data-field="indicator"]');
  const reloadButton = panel.querySelector('[data-action="reload"]');
  const copyReplayButton = panel.querySelector('[data-action="copy-replay"]');
  const replayCommandNode = panel.querySelector('[data-field="replay-command"]');
  const requestDeadlineMs = 15000;
  const terminalStates = new Set([
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "LOST",
    "EXPIRED",
  ]);
  const initialState = panel.dataset.initialState ?? "";
  const hasWorkflow = panel.dataset.hasWorkflow !== "false";
  const openedState = initialState || (stateNode?.dataset?.state ?? "");
  let timer = null;
  let copyResetTimer = null;
  let stopped = false;
  let inFlight = false;
  let lastAnnouncementKey = null;

  const setPollingState = (pollingState, taskState) => {
    if (indicatorNode && indicatorNode.dataset) {
      indicatorNode.dataset.pollingState = pollingState;
      if (taskState !== undefined) {
        indicatorNode.dataset.state = taskState;
      }
    }
  };

  if (reloadButton && typeof reloadButton.addEventListener === "function") {
    reloadButton.addEventListener("click", () => {
      window.location?.reload?.();
    });
  }

  if (copyReplayButton && typeof copyReplayButton.addEventListener === "function") {
    copyReplayButton.addEventListener("click", async () => {
      const command =
        copyReplayButton.dataset?.replayCommand ||
        replayCommandNode?.textContent?.trim() ||
        "";
      if (!command) {
        return;
      }
      const setCopyLabel = (label) => {
        copyReplayButton.textContent = label;
        if (copyResetTimer !== null) {
          window.clearTimeout(copyResetTimer);
        }
        copyResetTimer = window.setTimeout(() => {
          copyReplayButton.textContent = "Copy command";
          copyResetTimer = null;
        }, 2000);
      };
      try {
        const clipboard =
          window.navigator?.clipboard ?? globalThis.navigator?.clipboard;
        if (clipboard && typeof clipboard.writeText === "function") {
          await clipboard.writeText(command);
          setCopyLabel("Copied");
          return;
        }
      } catch (error) {
        // Fall through to manual-copy guidance when clipboard access is blocked.
      }
      setCopyLabel("Select and copy");
    });
  }

  const schedule = () => {
    if (!stopped && !document.hidden && !panel.hidden) {
      timer = window.setTimeout(refresh, 3000);
    }
  };

  const renderWorkflow = (workflow, availability) => {
    if (!workflow) {
      if (availability) {
        return `Workflow detail: ${availability}.`;
      }
      return hasWorkflow
        ? "No workflow progress available."
        : "Not a workflow task.";
    }
    const completed = workflow.completed_nodes ?? 0;
    const total = workflow.total_nodes ?? 0;
    const percent = workflow.progress_percent ?? 0;
    const revision = workflow.revision ?? 0;
    const state = workflow.state ?? workflow.workflow_state ?? "UNKNOWN";
    const detailAvailability = workflow.detail?.availability ?? availability;
    if (workflow.reporting_policy === "terminal_only") {
      const declaredNodes = workflow.declared_nodes;
      const declared =
        Number.isInteger(declaredNodes) && declaredNodes >= 0
          ? ` The pinned plan declares ${declaredNodes} nodes.`
          : "";
      return (
        `Terminal summary: ${state}. Detail ${detailAvailability ?? "OMITTED_BY_POLICY"}.` +
        `${declared} No node execution detail was collected.`
      );
    }
    const detail = detailAvailability ? `, detail ${detailAvailability}` : "";
    return `${state}: ${completed}/${total} nodes (${percent}%), revision ${revision}${detail}`;
  };

  async function refresh() {
    if (stopped || document.hidden || panel.hidden || inFlight) {
      return;
    }
    let controller;
    try {
      controller = new window.AbortController();
    } catch (error) {
      stopped = true;
      setPollingState("error");
      statusNode.textContent =
        "Live updates unavailable in this browser; reload the page for current status.";
      return;
    }
    inFlight = true;
    setPollingState("live");
    let deadlineTimer = null;
    let expired = false;
    try {
      // Keep every response effect outside this operation: an expired request
      // may still settle after a replacement poll if cancellation is delayed.
      const readUpdate = (async () => {
        const response = await window.fetch(endpoint, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (expired) {
          return null;
        }
        const contentType = response.headers.get("content-type") ?? "";
        if (
          response.redirected ||
          [401, 403, 404].includes(response.status) ||
          (response.ok && !contentType.includes("application/json"))
        ) {
          return { stop: true };
        }
        if (!response.ok) {
          throw new Error(`status ${response.status}`);
        }
        return { stop: false, payload: await response.json() };
      })();
      // This scheduled deadline covers headers and body. Browser suspension
      // can delay timers; it is not a physical wall-clock guarantee.
      const deadline = new Promise((_, reject) => {
        deadlineTimer = window.setTimeout(() => {
          expired = true;
          reject(new Error("request deadline"));
          try {
            controller.abort();
          } catch (error) {
            // The deadline still releases polling if browser cancellation fails.
          }
        }, requestDeadlineMs);
      });
      const update = await Promise.race([readUpdate, deadline]);
      if (expired) {
        return;
      }
      if (update.stop) {
        stopped = true;
        setPollingState("error");
        statusNode.textContent = "Live updates stopped; reload after signing in again.";
        return;
      }
      const payload = update.payload;
      const task = payload.task ?? payload;
      const state = String(task.state ?? "UNKNOWN");
      stateNode.textContent = state;
      stateNode.dataset.state = state;
      attemptNode.textContent = String(task.attempt_number ?? "-");
      const workflowText = renderWorkflow(
        payload.workflow ?? null,
        payload.workflow_availability ?? null,
      );
      workflowNode.textContent = workflowText;
      const announcementKey = [
        state,
        task.attempt_number ?? "none",
        task.execution_generation ?? "none",
        task.workflow_run_id ?? "none",
        payload.workflow?.revision ?? "none",
      ].join(":");
      if (announcementKey !== lastAnnouncementKey) {
        statusNode.textContent = `Durable status updated: ${state}. ${workflowText}`;
        lastAnnouncementKey = announcementKey;
      }
      const isTerminal = terminalStates.has(state);
      stopped = isTerminal;
      setPollingState(
        isTerminal ? "terminal" : document.hidden ? "paused" : "live",
        state,
      );
      if (reloadButton && (isTerminal || (openedState && state !== openedState))) {
        reloadButton.hidden = false;
      }
    } catch (error) {
      lastAnnouncementKey = null;
      setPollingState("error");
      statusNode.textContent = "Status update unavailable; durable page data is unchanged.";
    } finally {
      if (deadlineTimer !== null) {
        window.clearTimeout(deadlineTimer);
      }
      inFlight = false;
      schedule();
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    if (stopped) {
      return;
    }
    if (document.hidden) {
      setPollingState("paused");
    } else {
      setPollingState("live");
      void refresh();
    }
  });

  if (terminalStates.has(initialState)) {
    stopped = true;
    setPollingState("terminal", initialState);
    return;
  }

  if (document.hidden) {
    setPollingState("paused");
  }

  void refresh();
})();
