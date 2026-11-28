(() => {
  "use strict";

  const legacySessionCredentialKey = "django-ray.testproject.api-token.v1";
  const SMOKE_POLL_INITIAL_DELAY_MS = 400;
  const SMOKE_POLL_INTERVAL_MS = 800;
  const SMOKE_POLL_MAX_ATTEMPTS = 12;
  const TERMINAL_TASK_STATUSES = new Set(["SUCCESSFUL", "FAILED"]);
  const TERMINAL_TASK_STATES = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"]);

  function purgeLegacySessionCredential() {
    try {
      window.sessionStorage.removeItem(legacySessionCredentialKey);
    } catch {
      // Storage may be disabled or inaccessible. Current credentials remain page-memory only.
    }
  }

  purgeLegacySessionCredential();

  const tokenInput = document.querySelector("#api-token");
  const useToken = document.querySelector("#use-token");
  const credentialStatus = document.querySelector("#credential-status");
  const forgetToken = document.querySelector("#forget-token");
  const output = document.querySelector("#output");
  const trigger = document.querySelector("#trigger");
  const refreshStatsButton = document.querySelector("#refresh-stats");
  const smokeTaskAdminWrap = document.querySelector("#smoke-task-admin-wrap");
  const viewMetrics = document.querySelector("#view-metrics");
  const viewExecutions = document.querySelector("#view-executions");
  const protectedResponse = document.querySelector("#protected-response");
  const protectedResponseTitle = document.querySelector("#protected-response-title");
  const protectedResponseBody = document.querySelector("#protected-response-body");
  const closeProtectedResponse = document.querySelector("#close-protected-response");
  const statOtherSummary = document.querySelector("#stat-other-summary");
  const statMap = {
    total: document.querySelector("#stat-total"),
    queued: document.querySelector("#stat-queued"),
    running: document.querySelector("#stat-running"),
    succeeded: document.querySelector("#stat-succeeded"),
    failed: document.querySelector("#stat-failed"),
    expired: document.querySelector("#stat-expired"),
  };

  let apiToken = "";
  let credentialGeneration = 0;
  let protectedResponseGeneration = 0;
  let statisticsRequestGeneration = 0;
  let smokePollGeneration = 0;
  let smokePollTimerId = null;

  class DashboardCredentialError extends Error {
    constructor(message, clearedCurrentCredential = false) {
      super(message);
      this.clearedCurrentCredential = clearedCurrentCredential;
    }
  }

  function prefersReducedMotion() {
    return (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function cancelSmokePolling() {
    smokePollGeneration += 1;
    if (smokePollTimerId !== null && typeof window.clearTimeout === "function") {
      window.clearTimeout(smokePollTimerId);
    }
    smokePollTimerId = null;
  }

  function setCredentialStatus(message, state = "") {
    credentialStatus.textContent = message;
    credentialStatus.classList.remove("authenticated", "danger");
    if (state) credentialStatus.classList.add(state);
  }

  function replaceCredential(token = "") {
    credentialGeneration += 1;
    apiToken = token;
    return credentialGeneration;
  }

  function credentialIsCurrent(generation, token) {
    return credentialGeneration === generation && apiToken === token;
  }

  function clearProtectedResponse() {
    protectedResponseGeneration += 1;
    protectedResponse.hidden = true;
    protectedResponseTitle.textContent = "";
    protectedResponseBody.textContent = "";
  }

  function clearCredential(message, state = "") {
    cancelSmokePolling();
    replaceCredential();
    purgeLegacySessionCredential();
    tokenInput.value = "";
    clearProtectedResponse();
    if (smokeTaskAdminWrap) smokeTaskAdminWrap.hidden = true;
    setCredentialStatus(message, state);
  }

  async function authenticatedRequest(url, options = {}) {
    if (!apiToken) {
      setCredentialStatus("Enter the API token before using protected actions.", "danger");
      tokenInput.focus();
      throw new DashboardCredentialError("A browser API token is required.");
    }

    const requestToken = apiToken;
    const requestGeneration = credentialGeneration;
    let response;
    try {
      const headers = new Headers(options.headers);
      headers.set("Authorization", `Bearer ${requestToken}`);
      response = await window.fetch(url, {
        cache: "no-store",
        ...options,
        headers,
      });
    } catch {
      throw new DashboardCredentialError("The authenticated request could not be sent.");
    }

    if (response.status === 401) {
      const rejectedCurrentCredential = credentialIsCurrent(requestGeneration, requestToken);
      if (rejectedCurrentCredential) {
        clearCredential("The API token was rejected. Paste a valid token and try again.", "danger");
        tokenInput.focus();
      }
      throw new DashboardCredentialError(
        "The browser API token was rejected.",
        rejectedCurrentCredential,
      );
    }
    return response;
  }

  function showActionError(error) {
    output.textContent = error instanceof Error ? error.message : "The request failed.";
  }

  function showCurrentActionError(error, generation, token) {
    if (
      credentialIsCurrent(generation, token) ||
      (error instanceof DashboardCredentialError && error.clearedCurrentCredential)
    ) {
      showActionError(error);
    }
  }

  function updateOtherStatsSummary(stats) {
    if (!statOtherSummary) return;
    const knownPrimary =
      (stats.queued ?? 0) +
      (stats.running ?? 0) +
      (stats.succeeded ?? 0) +
      (stats.failed ?? 0) +
      (stats.expired ?? 0);
    const otherCount = Math.max(0, (stats.total ?? 0) - knownPrimary);
    if (otherCount > 0) {
      const cancelled = stats.cancelled ?? 0;
      const cancelling = stats.cancelling ?? 0;
      const lost = stats.lost ?? 0;
      statOtherSummary.textContent =
        `Includes ${otherCount} other (${cancelled} cancelled, ${cancelling} cancelling, ${lost} lost)`;
      statOtherSummary.hidden = false;
    } else {
      statOtherSummary.textContent = "";
      statOtherSummary.hidden = true;
    }
  }

  async function refreshStats() {
    const requestGeneration = ++statisticsRequestGeneration;
    const refreshGeneration = credentialGeneration;
    const refreshToken = apiToken;
    const response = await authenticatedRequest("/api/executions/stats");
    if (!response.ok) throw new Error(`Stats request failed: ${response.status}`);
    const stats = await response.json();
    if (
      requestGeneration !== statisticsRequestGeneration ||
      !credentialIsCurrent(refreshGeneration, refreshToken)
    ) {
      return;
    }
    for (const [key, element] of Object.entries(statMap)) {
      if (element) element.textContent = stats[key] ?? 0;
    }
    updateOtherStatsSummary(stats);
    return stats;
  }

  async function useCredential() {
    const suppliedToken = tokenInput.value;
    tokenInput.value = "";
    if (!suppliedToken) {
      clearCredential("Enter the API token before connecting.", "danger");
      tokenInput.focus();
      output.textContent = "A browser API token is required.";
      return;
    }

    const suppliedGeneration = replaceCredential(suppliedToken);
    setCredentialStatus("Checking the API token...");
    output.textContent = "Refreshing authenticated task statistics...";
    try {
      await refreshStats();
      if (!credentialIsCurrent(suppliedGeneration, suppliedToken)) return;
      setCredentialStatus(
        "Authenticated for this loaded page. Reloading clears the token.",
        "authenticated",
      );
      output.textContent = "Authenticated task statistics refreshed.";
    } catch (error) {
      if (error instanceof DashboardCredentialError && error.clearedCurrentCredential) {
        showActionError(error);
        return;
      }
      if (!credentialIsCurrent(suppliedGeneration, suppliedToken)) return;
      clearCredential(
        "The API token could not be verified. Paste it again to retry.",
        "danger",
      );
      tokenInput.focus();
      showActionError(error);
    }
  }

  function scheduleSmokeTaskPoll(taskId, attempt, actionGeneration, actionToken, pollGeneration) {
    smokePollTimerId = window.setTimeout(async () => {
      smokePollTimerId = null;
      if (
        pollGeneration !== smokePollGeneration ||
        !credentialIsCurrent(actionGeneration, actionToken)
      ) {
        return;
      }
      try {
        const statusResponse = await authenticatedRequest(
          `/api/tasks/${encodeURIComponent(taskId)}`,
        );
        if (
          pollGeneration !== smokePollGeneration ||
          !credentialIsCurrent(actionGeneration, actionToken)
        ) {
          return;
        }
        if (!statusResponse.ok) return;
        const taskStatus = await statusResponse.json();
        if (
          pollGeneration !== smokePollGeneration ||
          !credentialIsCurrent(actionGeneration, actionToken)
        ) {
          return;
        }
        const statusLabel = taskStatus.state || taskStatus.status || "UNKNOWN";
        const isTerminal =
          TERMINAL_TASK_STATUSES.has(taskStatus.status) ||
          TERMINAL_TASK_STATES.has(taskStatus.state);
        if (isTerminal) {
          output.textContent = `Task ${taskId} finished with state ${statusLabel}.`;
          await refreshStats();
          return;
        }
        if (attempt < SMOKE_POLL_MAX_ATTEMPTS) {
          output.textContent = `Task ${taskId} is ${statusLabel} (poll ${attempt}/${SMOKE_POLL_MAX_ATTEMPTS}).`;
          scheduleSmokeTaskPoll(
            taskId,
            attempt + 1,
            actionGeneration,
            actionToken,
            pollGeneration,
          );
          return;
        }
        await refreshStats();
        if (
          pollGeneration === smokePollGeneration &&
          credentialIsCurrent(actionGeneration, actionToken)
        ) {
          output.textContent =
            `Task ${taskId} is still ${statusLabel}. Use Refresh stats or Django Admin to check progress.`;
        }
      } catch (error) {
        if (
          pollGeneration === smokePollGeneration ||
          (error instanceof DashboardCredentialError && error.clearedCurrentCredential)
        ) {
          showCurrentActionError(error, actionGeneration, actionToken);
        }
      }
    }, SMOKE_POLL_INTERVAL_MS);
  }

  async function triggerTask() {
    const actionGeneration = credentialGeneration;
    const actionToken = apiToken;
    trigger.disabled = true;
    output.textContent = "Enqueuing task...";
    try {
      const response = await authenticatedRequest("/api/enqueue/add/2/3", {
        method: "POST",
      });
      if (!response.ok) throw new Error(`Enqueue failed: ${response.status}`);
      const task = await response.json();
      if (!credentialIsCurrent(actionGeneration, actionToken)) return;
      cancelSmokePolling();
      const pollGeneration = smokePollGeneration;
      const taskId = typeof task.task_id === "string" ? task.task_id : "";
      output.textContent = `Task ${task.task_id} enqueued with status ${task.status}.`;
      if (smokeTaskAdminWrap) smokeTaskAdminWrap.hidden = false;
      smokePollTimerId = window.setTimeout(() => {
        smokePollTimerId = null;
        if (!credentialIsCurrent(actionGeneration, actionToken)) return;
        return refreshStats()
          .then((latestStats) => {
            if (
              !latestStats ||
              pollGeneration !== smokePollGeneration ||
              !credentialIsCurrent(actionGeneration, actionToken)
            ) {
              return;
            }
            if (taskId && !TERMINAL_TASK_STATUSES.has(task.status)) {
              scheduleSmokeTaskPoll(taskId, 1, actionGeneration, actionToken, pollGeneration);
            }
          })
          .catch((error) => {
            showCurrentActionError(error, actionGeneration, actionToken);
          });
      }, SMOKE_POLL_INITIAL_DELAY_MS);
    } catch (error) {
      showCurrentActionError(error, actionGeneration, actionToken);
    } finally {
      trigger.disabled = false;
    }
  }

  async function showProtectedEndpoint(button, url, title, parseResponse) {
    const actionGeneration = credentialGeneration;
    const actionToken = apiToken;
    const responseGeneration = ++protectedResponseGeneration;
    button.disabled = true;
    output.textContent = `Loading ${title.toLowerCase()}...`;
    try {
      const response = await authenticatedRequest(url);
      if (!response.ok) throw new Error(`${title} request failed: ${response.status}`);
      if (
        responseGeneration !== protectedResponseGeneration ||
        !credentialIsCurrent(actionGeneration, actionToken)
      ) return;
      const responseBody = await parseResponse(response);
      if (
        responseGeneration !== protectedResponseGeneration ||
        !credentialIsCurrent(actionGeneration, actionToken)
      ) return;
      protectedResponseTitle.textContent = title;
      protectedResponseBody.textContent = responseBody;
      protectedResponse.hidden = false;
      protectedResponse.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "nearest",
      });
      output.textContent = `${title} loaded through the authenticated API.`;
    } catch (error) {
      if (
        responseGeneration === protectedResponseGeneration ||
        (error instanceof DashboardCredentialError && error.clearedCurrentCredential)
      ) {
        showCurrentActionError(error, actionGeneration, actionToken);
      }
    } finally {
      button.disabled = false;
    }
  }

  useToken.addEventListener("click", useCredential);
  tokenInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      useCredential();
    }
  });
  forgetToken.addEventListener("click", () => {
    clearCredential("Not authenticated. The page-memory API token was forgotten.");
    output.textContent = "Browser API token forgotten for this loaded page.";
    tokenInput.focus();
  });
  trigger.addEventListener("click", triggerTask);
  if (refreshStatsButton) {
    refreshStatsButton.addEventListener("click", async () => {
      const actionGeneration = credentialGeneration;
      const actionToken = apiToken;
      refreshStatsButton.disabled = true;
      output.textContent = "Refreshing authenticated task statistics...";
      try {
        const updated = await refreshStats();
        if (updated && credentialIsCurrent(actionGeneration, actionToken)) {
          output.textContent = "Authenticated task statistics refreshed.";
        }
      } catch (error) {
        showCurrentActionError(error, actionGeneration, actionToken);
      } finally {
        refreshStatsButton.disabled = false;
      }
    });
  }
  viewMetrics.addEventListener("click", () =>
    showProtectedEndpoint(viewMetrics, "/api/metrics", "Metrics", (response) => response.text()),
  );
  viewExecutions.addEventListener("click", () =>
    showProtectedEndpoint(viewExecutions, "/api/executions", "Executions", async (response) =>
      JSON.stringify(await response.json(), null, 2),
    ),
  );
  closeProtectedResponse.addEventListener("click", () => {
    clearProtectedResponse();
    output.textContent = "Protected response closed.";
  });
})();
