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
  const terminalStates = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "LOST"]);
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
    inFlight = true;
    try {
      const response = await window.fetch(endpoint, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const contentType = response.headers.get("content-type") ?? "";
      if (
        response.redirected ||
        [401, 403, 404].includes(response.status) ||
        !contentType.includes("application/json")
      ) {
        stopped = true;
        statusNode.textContent = "Live updates stopped; reload after signing in again.";
        return;
      }
      if (!response.ok) {
        throw new Error(`status ${response.status}`);
      }
      const payload = await response.json();
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
      statusNode.textContent = "Status update unavailable; durable page data is unchanged.";
    } finally {
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
