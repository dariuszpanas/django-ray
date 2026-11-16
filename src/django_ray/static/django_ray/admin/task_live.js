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
  const requestDeadlineMs = 15000;
  const terminalStates = new Set([
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "LOST",
    "EXPIRED",
  ]);
  let timer = null;
  let stopped = false;
  let inFlight = false;
  let lastAnnouncementKey = null;

  const schedule = () => {
    if (!stopped && !document.hidden) {
      timer = window.setTimeout(refresh, 3000);
    }
  };

  const renderWorkflow = (workflow, availability) => {
    if (!workflow) {
      return availability
        ? `Workflow detail: ${availability}.`
        : "No workflow progress available.";
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
    if (stopped || document.hidden || inFlight) {
      return;
    }
    let controller;
    try {
      controller = new window.AbortController();
    } catch (error) {
      stopped = true;
      statusNode.textContent =
        "Live updates unavailable in this browser; reload the page for current status.";
      return;
    }
    inFlight = true;
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
      stopped = terminalStates.has(state);
    } catch (error) {
      lastAnnouncementKey = null;
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
    if (!document.hidden && !stopped) {
      void refresh();
    }
  });

  void refresh();
})();
