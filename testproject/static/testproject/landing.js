(() => {
  "use strict";

  const tokenInput = document.querySelector("#api-token");
  const useToken = document.querySelector("#use-token");
  const credentialStatus = document.querySelector("#credential-status");
  const forgetToken = document.querySelector("#forget-token");
  const output = document.querySelector("#output");
  const trigger = document.querySelector("#trigger");
  const viewMetrics = document.querySelector("#view-metrics");
  const viewExecutions = document.querySelector("#view-executions");
  const protectedResponse = document.querySelector("#protected-response");
  const protectedResponseTitle = document.querySelector("#protected-response-title");
  const protectedResponseBody = document.querySelector("#protected-response-body");
  const closeProtectedResponse = document.querySelector("#close-protected-response");
  const statMap = {
    total: document.querySelector("#stat-total"),
    queued: document.querySelector("#stat-queued"),
    running: document.querySelector("#stat-running"),
    succeeded: document.querySelector("#stat-succeeded"),
    failed: document.querySelector("#stat-failed"),
  };

  let apiToken = "";
  let credentialGeneration = 0;

  class DashboardCredentialError extends Error {
    constructor(message, clearedCurrentCredential = false) {
      super(message);
      this.clearedCurrentCredential = clearedCurrentCredential;
    }
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

  function clearCredential(message) {
    replaceCredential();
    tokenInput.value = "";
    setCredentialStatus(message);
  }

  async function authenticatedRequest(url, options = {}) {
    if (!apiToken) {
      setCredentialStatus("Enter the API token before using protected actions.", "danger");
      tokenInput.focus();
      throw new DashboardCredentialError("A browser API token is required.");
    }

    const requestToken = apiToken;
    const requestGeneration = credentialGeneration;
    const headers = new Headers(options.headers);
    headers.set("Authorization", `Bearer ${requestToken}`);
    const response = await window.fetch(url, {
      cache: "no-store",
      ...options,
      headers,
    });

    if (response.status === 401) {
      const rejectedCurrentCredential = credentialIsCurrent(requestGeneration, requestToken);
      if (rejectedCurrentCredential) {
        clearCredential("The API token was rejected. Paste a valid token and try again.");
        credentialStatus.classList.add("danger");
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

  async function refreshStats() {
    const response = await authenticatedRequest("/api/executions/stats");
    if (!response.ok) throw new Error(`Stats request failed: ${response.status}`);
    const stats = await response.json();
    for (const [key, element] of Object.entries(statMap)) {
      element.textContent = stats[key] ?? 0;
    }
    return stats;
  }

  async function useCredential() {
    const suppliedToken = tokenInput.value;
    tokenInput.value = "";
    if (!suppliedToken) {
      clearCredential("Enter the API token before connecting.");
      credentialStatus.classList.add("danger");
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
      setCredentialStatus("Authenticated for this loaded page.", "authenticated");
      output.textContent = "Authenticated task statistics refreshed.";
    } catch (error) {
      if (error instanceof DashboardCredentialError && error.clearedCurrentCredential) {
        showActionError(error);
        return;
      }
      if (!credentialIsCurrent(suppliedGeneration, suppliedToken)) return;
      clearCredential("The API token could not be verified. Paste it again to retry.");
      credentialStatus.classList.add("danger");
      tokenInput.focus();
      showActionError(error);
    }
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
      output.textContent = `Task ${task.task_id} enqueued with status ${task.status}.`;
      window.setTimeout(() => refreshStats().catch(showActionError), 700);
    } catch (error) {
      showActionError(error);
    } finally {
      trigger.disabled = false;
    }
  }

  async function showProtectedEndpoint(button, url, title, parseResponse) {
    const actionGeneration = credentialGeneration;
    const actionToken = apiToken;
    button.disabled = true;
    output.textContent = `Loading ${title.toLowerCase()}...`;
    try {
      const response = await authenticatedRequest(url);
      if (!response.ok) throw new Error(`${title} request failed: ${response.status}`);
      if (!credentialIsCurrent(actionGeneration, actionToken)) return;
      const responseBody = await parseResponse(response);
      if (!credentialIsCurrent(actionGeneration, actionToken)) return;
      protectedResponseTitle.textContent = title;
      protectedResponseBody.textContent = responseBody;
      protectedResponse.hidden = false;
      protectedResponse.scrollIntoView({ behavior: "smooth", block: "nearest" });
      output.textContent = `${title} loaded through the authenticated API.`;
    } catch (error) {
      showActionError(error);
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
    clearCredential("Not authenticated. The in-memory API token was forgotten.");
    protectedResponse.hidden = true;
    protectedResponseBody.textContent = "";
    output.textContent = "Browser API token forgotten.";
    tokenInput.focus();
  });
  trigger.addEventListener("click", triggerTask);
  viewMetrics.addEventListener("click", () =>
    showProtectedEndpoint(viewMetrics, "/api/metrics", "Metrics", (response) => response.text()),
  );
  viewExecutions.addEventListener("click", () =>
    showProtectedEndpoint(viewExecutions, "/api/executions", "Executions", async (response) =>
      JSON.stringify(await response.json(), null, 2),
    ),
  );
  closeProtectedResponse.addEventListener("click", () => {
    protectedResponse.hidden = true;
    protectedResponseBody.textContent = "";
  });
})();
