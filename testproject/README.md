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
- the worker executes the task and both the result API and PostgreSQL return `42`.

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

The following direct requests enqueue `20 + 22`, refresh the Django task result, and read the durable
execution record. The token is carried in the authorization header, never in the URL.

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
  result_json="$(
    curl --fail --silent --show-error \
      --header "Authorization: Bearer ${DJANGO_API_TOKEN}" \
      "http://127.0.0.1:8000/api/tasks/${task_id}"
  )"
  status="$(
    printf '%s' "$result_json" |
      uv run python -c 'import json, sys; print(json.load(sys.stdin)["status"])'
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
    $result = Invoke-RestMethod `
      -Headers $headers `
      -Uri "http://127.0.0.1:8000/api/tasks/$($task.task_id)"
    Write-Host "Task status: $($result.status)"
    if ($result.status -eq "SUCCESSFUL") { break }
    if ($result.status -eq "FAILED") { throw "Task execution failed" }
    Start-Sleep -Seconds 2
}
if ($result.status -ne "SUCCESSFUL") { throw "Task execution timed out" }

Invoke-RestMethod `
  -Headers $headers `
  -Uri "http://127.0.0.1:8000/api/executions?task_id=$($task.task_id)&limit=1"
```

The automated smoke fails if the success state is not reached by its deadline. When using the manual
commands, confirm the final status is `SUCCESSFUL`; a result still in progress after the bounded loop
needs investigation.

## Verify through Django admin

Create an administrator interactively so the password is not stored in a tracked file or copied into
shell history:

```bash
docker compose exec web python testproject/manage.py createsuperuser
```

Open the [django-ray administration](http://127.0.0.1:8000/admin/) and sign in. The bundled
testproject pins [Django Unfold](https://unfoldadmin.com/) for a modern admin shell, forms, and
navigation. Open
[Ray task executions](http://127.0.0.1:8000/admin/django_ray/raytaskexecution/) and inspect the
completed `add_numbers` record. The task ID should match the API response, its state should be
`SUCCEEDED`, and its result should be `42`. The changelist retains django-ray's retry and cancel
actions, while the change page retains live durable status and bounded workflow-detail links.

Unfold is a testproject dependency, not a required dependency of the published `django-ray` package.
Downstream projects that want the same theme can install `django-unfold`, place `"unfold"` before
`"django.contrib.admin"` in `INSTALLED_APPS`, and configure the optional `UNFOLD` settings. When the
app is not installed, django-ray's registrations continue to use Django's standard `ModelAdmin`.
Production deployments using Unfold must run `collectstatic`; the tracked Kubernetes web deployment
does this in the pod's `collect-static` init container before the application starts.

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
