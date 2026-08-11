# Bundled testproject quickstart

The bundled project is a local evaluation boundary for django-ray. The tracked
[`compose.yaml`](../compose.yaml) starts PostgreSQL, applies migrations once, and only then starts
the web and task-manager worker services. Web, worker, migration, and smoke containers receive the
same PostgreSQL settings. The topology and generated credentials below are for a trusted local
machine, not a production deployment.

Only `web` and the opt-in `smoke` service receive `DJANGO_API_TOKEN`. The migration service and
task-manager worker receive the shared database configuration but not the bearer credential, so
local Ray worker processes cannot inherit the operator token.

## Prerequisites

- Docker with the Compose v2 plugin
- [`uv`](https://docs.astral.sh/uv/) for generating disposable credentials with the repository's
  managed Python environment

Keep the same shell open for the complete quickstart. Compose requires both environment variables
and fails before creating containers if either is missing.

### POSIX

```bash
export DJANGO_API_TOKEN="$(
  uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
export POSTGRES_PASSWORD="$(
  uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"

docker compose up --build --detach web worker
docker compose --profile smoke run --rm --no-deps smoke
```

### PowerShell

```powershell
$env:DJANGO_API_TOKEN = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:POSTGRES_PASSWORD = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"

docker compose up --build --detach web worker
docker compose --profile smoke run --rm --no-deps smoke
```

`migrate` is an idempotent one-shot service. PostgreSQL must first be healthy, and Compose requires
that migration service to exit successfully before starting either `web` or `worker`; replicas do
not race migrations during startup. The smoke is bounded to three minutes and proves:

- PostgreSQL is the configured database and has no unapplied migrations;
- missing and invalid bearer tokens fail closed;
- the documented bearer token can enqueue a task;
- an active task-manager worker lease is visible through the same database;
- the worker executes the task, the bounded status API reaches `SUCCESSFUL`, and the
  separately bounded execution projection plus PostgreSQL return `42`.

Inspect startup state without printing container environments:

```bash
docker compose ps --all
docker compose logs migrate
```

## Use the authenticated application

Open [the landing page](http://127.0.0.1:8000/) and paste the current
`DJANGO_API_TOKEN` value into **Browser API access**. The page retains a verified token only in the
current tab's `sessionStorage`.

For Swagger, open [the API docs](http://127.0.0.1:8000/api/docs), select **Authorize**, and paste the
token value. Swagger adds the `Bearer` scheme; do not paste the word `Bearer` into that dialog.

The following direct requests enqueue `20 + 22`, poll the bounded task-status adapter,
and read the durable execution record. The token is carried in the authorization header,
never in the URL.

### POSIX

```bash
task_json="$(
  curl --fail --silent --show-error \
    --request POST \
    --header "Authorization: Bearer ${DJANGO_API_TOKEN}" \
    http://127.0.0.1:8000/api/enqueue/add/20/22
)"
task_id="$(
  printf '%s' "$task_json" |
    uv run python -c 'import json, sys; print(json.load(sys.stdin)["task_id"])'
)"

for attempt in $(seq 1 60); do
  status_json="$(
    curl --fail --silent --show-error \
      --header "Authorization: Bearer ${DJANGO_API_TOKEN}" \
      "http://127.0.0.1:8000/api/tasks/${task_id}"
  )"
  status="$(
    printf '%s' "$status_json" |
      uv run python -c '
import json
import sys

raw = sys.stdin.buffer.read()
if len(raw) > 65_536:
    raise SystemExit("Task status response exceeded 65,536 bytes")
payload = json.loads(raw)
allowed = {
    None,
    "external_input_not_loaded",
    "stored_input_exceeds_status_limit",
    "malformed_inline_input",
    "encoded_response_limit",
}
if payload.get("input_omission_reason") not in allowed:
    raise SystemExit("Task status returned an unknown input omission reason")
if payload.get("input_max_bytes") != 16_384:
    raise SystemExit("Task status changed its input bound")
if payload.get("response_max_bytes") != 65_536:
    raise SystemExit("Task status changed its response bound")
print(payload["status"])
'
  )"
  printf 'Task status: %s\n' "$status"
  [ "$status" = "SUCCESSFUL" ] && break
  [ "$status" = "FAILED" ] && exit 1
  sleep 2
done
[ "$status" = "SUCCESSFUL" ] || exit 1

curl --fail --silent --show-error \
  --header "Authorization: Bearer ${DJANGO_API_TOKEN}" \
  "http://127.0.0.1:8000/api/executions?task_id=${task_id}&limit=1"
```

### PowerShell

```powershell
$headers = @{ Authorization = "Bearer $env:DJANGO_API_TOKEN" }
$task = Invoke-RestMethod `
  -Method Post `
  -Headers $headers `
  -Uri "http://127.0.0.1:8000/api/enqueue/add/20/22"

foreach ($attempt in 1..60) {
    $response = Invoke-WebRequest `
      -UseBasicParsing `
      -Headers $headers `
      -Uri "http://127.0.0.1:8000/api/tasks/$($task.task_id)"
    if ([Text.Encoding]::UTF8.GetByteCount($response.Content) -gt 65536) {
        throw "Task status response exceeded 65,536 bytes"
    }
    $taskStatus = $response.Content | ConvertFrom-Json
    $allowedOmissionReasons = @(
        $null,
        "external_input_not_loaded",
        "stored_input_exceeds_status_limit",
        "malformed_inline_input",
        "encoded_response_limit"
    )
    if ($taskStatus.input_omission_reason -notin $allowedOmissionReasons) {
        throw "Task status returned an unknown input omission reason"
    }
    if ($taskStatus.input_max_bytes -ne 16384) {
        throw "Task status changed its input bound"
    }
    if ($taskStatus.response_max_bytes -ne 65536) {
        throw "Task status changed its response bound"
    }
    Write-Host "Task status: $($taskStatus.status)"
    if ($taskStatus.status -eq "SUCCESSFUL") { break }
    if ($taskStatus.status -eq "FAILED") { throw "Task execution failed" }
    Start-Sleep -Seconds 2
}
if ($taskStatus.status -ne "SUCCESSFUL") { throw "Task execution timed out" }

Invoke-RestMethod `
  -Headers $headers `
  -Uri "http://127.0.0.1:8000/api/executions?task_id=$($task.task_id)&limit=1"
```

The automated smoke fails if the success state is not reached by its deadline. When using the manual
commands, confirm the final status is `SUCCESSFUL`; a task still in progress after the bounded loop
needs investigation.

`GET /api/tasks/{task_id}` is deliberately a monitoring projection, not the package's
full Python `TaskResult`. It returns exact durable state and attempt identity, guards the
combined inline `args` and `kwargs` at 16,384 bytes, never loads external input or result
storage, and caps the full response at 65,536 bytes. Nullable inputs carry one of the
fixed omission reasons validated above. Application code can still use `TaskResult` for
full arguments, keyword arguments, and successful return data under its own trust
boundary.

## Observe a low-resource mixed workload

The guarded local KubeRay stack exposes the application on
[localhost:30080](http://localhost:30080) and includes consumers for the default, priority,
`sync`, `ml`, and `ray-data` queues. Its direct exploratory profile uses one default/priority
Ray Core task manager, one `ray-data` Ray Job task manager, and two fixed two-CPU Ray workers;
the heavier Kong profile is for capacity and backlog exercises. The tracked
`ObservabilityDemoUser` submits only one task at a time, waits for its terminal result, pauses for
two to four seconds, and then moves to the next task family. One cycle covers:

- basic and deliberately slow default-queue tasks;
- a high-priority task;
- a synchronous task;
- a small distributed search and three tiny nested workflows that compare full,
  terminal-only, and disabled reporting;
- a lightweight `thin` RuntimeEnv probe;
- a small ML inference task;
- authenticated execution statistics and Prometheus metrics.

It intentionally excludes failure injection, CPU benchmarks, NumPy RuntimeEnv installation,
bursts, and stress workloads. Sync tasks are visible in the task worker logs and Django admin but
do not appear in Ray. The default, priority, cluster/workflow, RuntimeEnv, and ML tasks pass through
Ray and can be inspected on the [local Ray dashboard](http://localhost:30265).
The three workflow scenarios use separate stable Locust labels for enqueue, terminal
polling, and bounded-summary reads. Full reporting must expose complete pilot detail,
terminal-only must expose its one summary with detail omitted by policy, and disabled
must report `DISABLED` without fabricating a summary. The demo stops instead of moving
to another task when any of those contracts is missing or malformed. The workflow and
RuntimeEnv poll endpoints are capped at 65,536 bytes, guard current inline result and
error values at 16,384 bytes each without loading external result storage, and expose
the documented fixed omission vocabulary. Workflow progress in those pollers uses a
bounded aggregate summary envelope rather than a legacy complete progress graph. A
published schema-v3 summary is preferred; supported older stored progress can supply
only sanitized aggregate counts through that envelope. The
tour uses ordinary small diagnostics; focused adapter tests cover the omission branches.

Load the current Kubernetes secret into the Locust process without printing it, run the five-minute
one-user demo, and remove the shell variable afterwards. At five minutes Locust stops scheduling
new scenarios, then waits for at most 150 additional seconds for the active scenario's bounded
terminal and workflow-summary validation. This keeps the reported run from silently abandoning a
task that was already enqueued. The headless demo also exits nonzero unless all eleven task
families complete at least once; a slow machine cannot report success after reaching only a partial
tour.

### POSIX

```bash
(
  trap 'unset DJANGO_API_TOKEN' EXIT
  export DJANGO_API_TOKEN="$(
    kubectl --context docker-desktop -n django-ray \
      get secret django-ray-secret \
      -o jsonpath='{.data.DJANGO_API_TOKEN}' |
      uv run python -c \
        'import base64, sys; print(base64.b64decode(sys.stdin.buffer.read()).decode(), end="")'
  )"
  make loadtest-demo
)
```

### PowerShell

```powershell
try {
    $djangoRayEncodedToken = kubectl --context docker-desktop -n django-ray `
      get secret django-ray-secret `
      -o jsonpath='{.data.DJANGO_API_TOKEN}'
    $env:DJANGO_API_TOKEN = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($djangoRayEncodedToken)
    )
    make loadtest-demo
}
finally {
    Remove-Item Env:DJANGO_API_TOKEN -ErrorAction SilentlyContinue
    Remove-Variable djangoRayEncodedToken -ErrorAction SilentlyContinue
}
```

For an interactive Locust session, use `make loadtest` and open
[localhost:8089](http://localhost:8089). The target explicitly selects
`ObservabilityDemoUser`, prefills one user at one user per second, and does not silently mix in
capacity, burst, or stress scenarios. Stopping the interactive run also grants the active scenario
the same bounded 150-second drain window.

Follow the Django task managers that claim durable rows and submit work:

```bash
kubectl --context docker-desktop -n django-ray logs -l app=django-ray,component=worker -c django-ray-worker --prefix --tail=0 --follow --max-log-requests=8
```

Follow the Ray execution processes separately:

```bash
kubectl --context docker-desktop -n django-ray logs -l app=ray,component=head -c ray-head --prefix --tail=0 --follow
kubectl --context docker-desktop -n django-ray logs -l app=ray,component=worker -c ray-worker --prefix --tail=0 --follow --max-log-requests=8
```

An unqualified `component=worker` selector mixes both families and makes it
harder to tell task claiming from Ray execution.

The demo neither scales nor stops the Kubernetes stack, and it retains the completed task rows for
Admin inspection. Use the explicit deployment teardown only after the exploratory test window is
finished.

`make loadtest-quick`, `make loadtest-moderate`, `make loadtest-18`, and
`make loadtest-stress` are explicit capacity or stress profiles. They are not prerequisites for
the observability demo and can create substantially more task rows and resource pressure.

When policy cost rather than HTTP behavior is the question, use the opt-in
[`django_ray_benchmark_workflow_reporting`](../docs/performance.md#attribute-live-workflow-reporting-policies)
command. It runs the same tiny nested workload sequentially under full,
terminal-only, and disabled reporting with counterbalanced order, durable
server-timestamp comparisons, allowlisted ingress/storage evidence, actor-observed
logical traffic/delivery/handler cost, and explicit unavailable metrics. Logical
event bytes are not presented as network traffic, and end-to-end processed delivery
delay is not presented as pure mailbox lag. The low-resource run retains its execution
rows for Admin inspection unless `--cleanup` is requested.

## Verify through Django admin

Create an administrator interactively so the password is not stored in a tracked file or copied into
shell history:

```bash
docker compose exec web python testproject/manage.py createsuperuser
```

Open the [django-ray administration](http://127.0.0.1:8000/admin/) and sign in. The bundled
testproject pins [Django Unfold](https://unfoldadmin.com/) for a modern admin shell branded with the
documentation icon and type treatment plus the landing page's graph artwork. Its light appearance
keeps the established sky-on-white treatment. The optional dark appearance shares the
documentation's near-black and neutral-grey surfaces, with django-ray sky blue reserved for links,
focus rings, active navigation, and compact primary controls instead of broad background washes.
Open
[Ray task executions](http://127.0.0.1:8000/admin/django_ray/raytaskexecution/) and inspect the
completed `add_numbers` record. The task ID should match the API response, its state should be
`SUCCEEDED`, and its result should be `42`. The changelist retains django-ray's retry and cancel
actions while prioritizing compact operational state, timestamps, and the Ray link; workflow
identity remains on the detail page. The change page retains live durable status, bounded
workflow detail as clearly labelled action links, ordered read-only attempt history, and a
**Retry task...** button for failed, lost, or expired executions. The button and bulk action use
the same side-effect warning and fenced confirmation. A succeeded execution instead explains
that repeating the work requires a fresh enqueue so its completed result and history remain
authoritative.
After a recovery, **Workflow execution** stacks previous failed attempts and the current
successful attempt from oldest to newest. Every graph is independently collapsible; archived
graphs are fetched only on first open, and the current graph appears exactly once. The page
shows the RuntimeEnv profile and content hash but intentionally omits the raw snapshot because
environment values and package URIs can contain sensitive application configuration. Execution
metadata is read-only; the detail retry control and list Retry/Cancel actions are the supported
fenced control paths.
The default
`TASK_ATTEMPT_ADMIN_MODE="inline"` hides the
standalone Task Attempt entry from top-level navigation without invalidating authorized list or
detail URLs. Select `standalone` to restore the previous navigation or `both` to expose both views.
Ordinary execution and attempt pages keep arguments, results, errors, and tracebacks
pattern-redacted. Superusers, or operators who hold both the ordinary object-view permission and
`django_ray.view_sensitive_task_data`, see the **Sensitive data** action on the corresponding detail
page. That separate incident view exposes only its fixed field allowlist, removes terminal control
effects, autoescapes HTML, rejects oversized fields before loading them, and is response-bounded;
it is pattern-unredacted rather than raw. The testproject intentionally exposes no equivalent
sensitive-diagnostics HTTP endpoint.

The ordinary testproject settings keep new durable RuntimeEnv snapshots in plaintext
for upgrade compatibility. The local `kuberay-kind` overlay opts only its Django web
and task-manager containers into encrypted writes using the explicit Django-secret
fallback; it does not put the encryption-mode selectors in the shared ConfigMap or
generic Ray pod specification. Its project profile also carries a fixed non-secret
probe marker so the guarded deployment gate can verify, without returning the value,
that Ray received the decrypted environment while the raw database column retained
only an authenticated envelope. Production deployments should prefer a dedicated key
ring and follow the reader-first rollout in
[Runtime Environments](../docs/runtime-environments.md#roll-out-encrypted-writes).

Unfold is a testproject dependency, not a required dependency of the published `django-ray` package.
Downstream projects that want the same theme can install `django-unfold`, place `"unfold"` before
`"django.contrib.admin"` in `INSTALLED_APPS`, and configure the optional `UNFOLD` settings. When the
app is not installed, django-ray's registrations continue to use Django's standard `ModelAdmin`.
Production deployments using Unfold must run `collectstatic`; the tracked Kubernetes web deployment
does this in the pod's `collect-static` init container before the application starts.

The attempt inline also requires global permission to view or change `TaskAttempt`; parent execution
permission alone does not expose child history. A custom `AdminSite` using the package inline must
register both `RayTaskExecution` and `TaskAttempt` on that same site so its detail link resolves.

Before upgrading an already-running copy of the bundled testproject, allow executions in `QUEUED`,
`RUNNING`, or `CANCELLING` state to finish. Their persisted Ray runtime environments predate the
Unfold dependency used by the updated testproject settings. Historical failed, cancelled, or lost
executions from the old testproject must be submitted as new tasks after the upgrade rather than
retried, because retries intentionally retain the original runtime environment.

## Shut down

Remove the disposable containers and PostgreSQL volume:

```bash
docker compose --profile smoke down --volumes --remove-orphans
```

Unset `DJANGO_API_TOKEN` and `POSTGRES_PASSWORD` when the shell no longer needs them.
