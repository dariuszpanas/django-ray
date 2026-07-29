# Docker deployment

The repository includes one tracked Docker Compose application for local evaluation. It uses the
bundled testproject, one PostgreSQL database, one idempotent migration service, a web service, and a
django-ray task-manager worker. This topology is intentionally small and is not production
hardening.

## Reproducible local quickstart

Prerequisites are Docker with the Compose v2 plugin and `uv`. Keep the same shell open so Compose
continues to receive the generated credentials. Neither value is committed to the repository.

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

Compose fails before creating containers when either required variable is absent. It starts
PostgreSQL first, waits for its health check, and runs `migrate` as a one-shot service. The web and
worker services both require that service to exit successfully. Migration is therefore not repeated
by every application replica, while re-running the one-shot service remains safe.

All four application services receive the same explicit database settings:

- `django.db.backends.postgresql`;
- database name and user `django_ray`;
- the generated PostgreSQL password;
- host `postgres` and port `5432`.

Only `web` and the opt-in `smoke` service receive the generated API bearer token. The migration
service and task-manager worker do not receive it, so Ray worker processes cannot inherit the
operator credential.

The local Ray worker receives a 1 GiB shared-memory allocation and concurrency is kept at one for the
quickstart. That is a bounded evaluation profile, not production sizing guidance.

## What the smoke proves

The opt-in `smoke` service has a three-minute deadline. It fails unless all of these observations
hold:

1. Django is connected to PostgreSQL and its migration graph has no unapplied leaves.
2. The web readiness endpoint can query that database.
3. An active task-manager worker lease appears in the same PostgreSQL database.
4. Missing and invalid bearer tokens receive HTTP 401.
5. The configured token enqueues `20 + 22`.
6. The task result endpoint reaches `SUCCESSFUL`.
7. The authenticated execution endpoint and a direct PostgreSQL read both contain result `42`.
8. The authenticated Unfold admin uses the package registrations, hides standalone attempt
   navigation by default, and links the archived successful attempt from its execution page.
9. The attempt detail, live observability endpoint, and manifest-hashed Unfold stylesheet are
   reachable through the same disposable administrator session.

CI generates fresh disposable credentials and runs this same tracked Compose contract. It does not
rely on a developer database, token, Python environment, or a prebuilt application image.

Inspect the startup state without dumping container environments:

```bash
docker compose ps --all
docker compose logs migrate
```

## Authenticate in the browser

Open the [sample landing page](http://127.0.0.1:8000/) and paste the current token value into
**Browser API access**. The page retains a verified token only in the current tab's
`sessionStorage`; it does not place the token in rendered HTML, a cookie, `localStorage`, or a URL.

Open [Swagger](http://127.0.0.1:8000/api/docs), select **Authorize**, and paste the token value.
Swagger adds the `Bearer` scheme automatically, so the dialog should receive the value without the
word `Bearer`.

Print the value only in a trusted terminal when it needs to be copied:

### POSIX

```bash
printf '%s\n' "$DJANGO_API_TOKEN"
```

### PowerShell

```powershell
$env:DJANGO_API_TOKEN
```

Do not put bearer tokens in query strings. Browser extensions, developer tools, same-origin
JavaScript, and browser session recovery can expose `sessionStorage`; this operator-token flow is
only for a trusted local demo. A remotely accessible deployment requires HTTPS and an appropriate
user identity/session design.

## Make direct API requests

The authorization header carries the token without embedding it in the URL.

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

curl --fail --silent --show-error \
  --header "Authorization: Bearer ${DJANGO_API_TOKEN}" \
  "http://127.0.0.1:8000/api/tasks/${task_id}"
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

Invoke-RestMethod `
  -Headers $headers `
  -Uri "http://127.0.0.1:8000/api/tasks/$($task.task_id)"
Invoke-RestMethod `
  -Headers $headers `
  -Uri "http://127.0.0.1:8000/api/executions?task_id=$($task.task_id)&limit=1"
```

The first result refresh may still report an in-progress state. The
[bundled testproject quickstart](https://github.com/dariuszpanas/django-ray/blob/main/testproject/README.md)
contains bounded POSIX and PowerShell polling loops, plus the complete admin verification flow.

## Verify through Django admin

Create a superuser interactively so its password is neither committed nor copied into shell
history:

```bash
docker compose exec web python testproject/manage.py createsuperuser
```

Then open [Ray task executions](http://127.0.0.1:8000/admin/django_ray/raytaskexecution/) and inspect
the task ID returned by the enqueue request. Its final state should be `SUCCEEDED` and its result
should be `42`. The change page shows its ordered read-only attempt history and links to the bounded
attempt detail. The default inline mode intentionally omits Task Attempts from top-level navigation;
authorized direct URLs remain valid.

## Shut down

Remove the disposable containers and PostgreSQL volume:

```bash
docker compose --profile smoke down --volumes --remove-orphans
```

Unset `DJANGO_API_TOKEN` and `POSTGRES_PASSWORD` when the shell no longer needs them.

## Build and entrypoint contract

The application image uses the committed `uv.lock`, uv `0.9.18`, and the `postgres` and `sample`
extras. Build it directly when inspecting the image:

```bash
docker build --tag django-ray:latest .
```

The entrypoint supports these explicit modes:

| Command | Behavior |
|---|---|
| `web` | Run Gunicorn after database connectivity succeeds |
| `web-dev` | Run Django's development server after database connectivity succeeds |
| `worker` | Run a task-manager worker with local Ray |
| `worker-cluster` | Run a task-manager worker connected to `RAY_ADDRESS` |
| `migrate` | Apply Django migrations once and exit |
| `collectstatic` | Collect static files once and exit |
| `createsuperuser` | Create a superuser from the standard Django environment variables |
| `shell` | Open the Django shell |

The image does not run migrations implicitly when web or worker replicas start. In production,
orchestration must provide the same database configuration to every application process, run one
controlled migration job before traffic or task claiming, and source credentials from the
deployment's secret manager. Do not copy the local Compose credentials or development topology into
a shared environment.

## Environment variables

| Variable | Description | Sample default |
|---|---|---|
| `DJANGO_DEPLOYMENT_MODE` | `demo` or fail-closed `production` validation | `demo` |
| `DJANGO_SECRET_KEY` | Django signing key; production requires a strong random value | local demo placeholder |
| `DJANGO_API_TOKEN` | Bearer token for every non-health sample API route | unset |
| `DJANGO_DEBUG` | Enable Django debug mode | `False` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated host allowlist | `localhost,127.0.0.1` |
| `DATABASE_ENGINE` | Django database backend | SQLite |
| `DATABASE_NAME` | Database name | `django_ray` |
| `DATABASE_USER` | Database user | `django_ray` |
| `DATABASE_PASSWORD` | Database password | unset |
| `DATABASE_HOST` | Database host | `localhost` |
| `DATABASE_PORT` | Database port | `5432` |
| `RAY_ADDRESS` | Ray cluster address | `auto` |
| `DJANGO_RAY_QUEUE` | Queue passed to the Docker worker | `default` |
| `DJANGO_RAY_QUEUES` | Comma-separated queues; overrides `DJANGO_RAY_QUEUE` | unset |
| `DJANGO_RAY_CONCURRENCY` | Worker concurrency | `10` |
| `RAY_DASHBOARD_URL` | Ray dashboard URL used by admin links | `http://localhost:8265` |

Only `/api/livez`, `/api/readyz`, and `/api/health` are unauthenticated. Task arguments, results,
logs, metrics, and workflow observability require `Authorization: Bearer <DJANGO_API_TOKEN>`.

## External Ray cluster

Build the separate Ray image when the application will connect to an existing cluster:

```bash
docker build --file Dockerfile.ray --tag django-ray-worker:latest .
```

The web process, task-manager worker, Ray nodes, and one-shot migration job must still share the same
durable database settings. Set `RAY_ADDRESS=ray://<head-service>:10001` for `worker-cluster`. The
tracked Compose quickstart intentionally exercises local Ray and does not model that production
topology.

## See also

- [Kubernetes deployment](kubernetes.md)
- [TLS configuration](tls.md)
- [Operator runbook](../runbook.md)
