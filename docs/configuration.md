# Configuration

django-ray is configured through the `DJANGO_RAY` setting in your Django settings file.

## Basic Configuration

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
    },
}

DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 10,
    "MAX_TASK_ATTEMPTS": 3,
}
```

### Ray address ownership

`DJANGO_RAY["RAY_ADDRESS"]` is the process-wide fallback. A Django Tasks backend
alias may instead set `OPTIONS["RAY_ADDRESS"]`; django-ray snapshots the resulting
effective Ray Job target on each execution when it is enqueued:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
    },
    "analytics": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["analytics"],
        "OPTIONS": {"RAY_ADDRESS": "ray://analytics-head:10001"},
    },
}
```

Ray Job submission uses and preserves that snapshot across retries. When the alias
omits `RAY_ADDRESS`, the snapshot is the current global
`DJANGO_RAY["RAY_ADDRESS"]` value. `RayTaskExecution.ray_target_address` owns that
durable routing decision; `ray_address` records the mutable submitted-job handle used
for status and cancellation. Once django-ray selects the target, it remains
authoritative for the Ray Job client; ambient `RAY_API_SERVER_ADDRESS` or
`RAY_ADDRESS` process variables cannot redirect that task to another cluster.

Ray Core workers choose one cluster when the process starts, through `--local`,
`--cluster`, or the global settings. They do not switch clusters per task. Use
separate queues and task-manager processes when Ray Core workloads must target
different clusters.

### Backend runner affinity

Backend aliases are compatible with every worker mode by default. Set
`OPTIONS["RAY_JOB_ONLY"] = True` when every queue declared by an alias requires the
outer Ray Job driver and must never be claimed by Ray Core or synchronous workers:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
    },
    "batch-jobs": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["ray-data"],
        "OPTIONS": {
            "RAY_ADDRESS": "auto",
            "RAY_JOB_ONLY": True,
        },
    },
}
```

The option is an explicit, boolean, default-off queue-affinity policy. A worker with
no execution-mode flag uses Ray Job mode by default and may claim `ray-data`. Ray Core
and synchronous workers reject an explicitly selected Ray-Job-only queue at startup
and again at the durable claim boundary. Their `--all-queues` expansion reports and
skips those queues; it fails if nothing compatible remains. An undeclared queue keeps
the existing open-ended behavior. If multiple aliases declare the same queue, one
`RAY_JOB_ONLY=True` declaration restricts that queue for every worker.

Explicit/default queue selection imports only aliases that opt in with
`RAY_JOB_ONLY=True`, so an unrelated unavailable task backend without this option does
not break a django-ray worker. An opted-in alias must resolve to `RayTaskBackend` or a
subclass before it can reserve queues; an unavailable opted-in backend fails closed.
The same-named option on a confirmed non-django-ray backend has no django-ray effect.

For a rolling deployment, first pause producers for the queue (or prove it empty), then
drain and stop every old Ray Core or synchronous worker that could select it. Upgrade
worker code, start the dedicated Ray Job worker, enable the backend option, and only
then resume producers. An older worker does not understand the affinity option and can
still claim the queue; do not overlap it with newly protected work.

## All Settings

### Ray Connection

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `RAY_ADDRESS` | `str` | `None` | Ray cluster address. Use `"auto"` for local, or `"ray://host:port"` for cluster |
| `RAY_RUNTIME_ENV` | `dict` | `{}` | Ray runtime environment configuration |
| `RAY_STATE_API_ADDRESS` | `str \| None` | `None` | Ray dashboard URL used for live task state and log lookup |
| `RAY_STATE_API_TIMEOUT_SECONDS` | `int` | `5` | Timeout for optional Ray state and log queries |
| `RUNTIME_ENV_PROFILES` | `dict` | `{}` | Named, validated Ray RuntimeEnv definitions |
| `DEFAULT_RUNTIME_ENV_PROFILE` | `str \| None` | `None` | Profile used when a backend does not select one |
| `RUNTIME_ENV_STORAGE_MODE` | `str` | `"plaintext"` | Format for new snapshots: `"plaintext"` or `"encrypted"`; readers always support both |
| `RUNTIME_ENV_ENCRYPTION_KEYS` | `dict[str, str]` | `{}` | Dedicated key IDs mapped to canonical unpadded base64url AES-256 keys |
| `RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY` | `str \| None` | `None` | Dedicated key ID or reserved `"django-secret"` ID used for new encrypted snapshots |
| `RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK` | `bool` | `False` | Explicitly allow HKDF-derived keys from `SECRET_KEY` and `SECRET_KEY_FALLBACKS` |
| `WORKFLOW_PLAN_CODE_REVISION` | `str \| None` | `None` | Immutable non-secret application build, artifact, or image revision; required for reusable-plan eligibility |
| `WORKFLOW_PLAN_TRUST_IDENTITY` | `dict` | `{}` | Bounded non-secret trust, credential-provider, and optional full-environment revision used to decide safe actor reuse |
| `RUNNER` | `str` | `"ray_job"` | Default runner when no mode flag is passed: `"ray_job"` or `"ray_core"` |

### Concurrency

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `DEFAULT_CONCURRENCY` | `int` | `10` | Maximum concurrent tasks per worker |

### Worker Polling

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `WORKER_POLL_INTERVAL_SECONDS` | `int \| float` | `0.1` | Base delay between claim queries (`0.01`-`10` seconds) |
| `WORKER_POLL_MAX_INTERVAL_SECONDS` | `int \| float` | `0.1` | Maximum idle claim delay (`0.01`-`60` seconds and not below the base) |

The default maximum equals the 100 ms base, preserving the existing polling cadence.
Set a larger maximum to opt into exponential idle backoff with bounded jitter, which
keeps multiple idle workers from repeatedly querying in lockstep. A claim, completion,
cancellation, timeout/recovery transition, or other lifecycle activity resets an
opted-in backoff immediately.

The maximum bounds how long an idle, available worker waits between observations of its
queue; it is not an end-to-end task-start guarantee. Heartbeats, Ray completion polling,
reconciliation, timeout checks, cancellation recovery, and lease cleanup use independent
monotonic schedules; idle claim backoff does not postpone them.

### Retry Policy

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `MAX_TASK_ATTEMPTS` | `int` | `3` | Maximum number of attempts before marking as failed |
| `RETRY_BACKOFF_SECONDS` | `int` | `60` | Base delay between retries (exponential backoff), from `0` to `3600` seconds |
| `RETRY_EXCEPTION_DENYLIST` | `list[str]` | `[]` | Exception types that should not be retried |

### Reliability

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `STUCK_TASK_TIMEOUT_SECONDS` | `int` | `300` | Time before a running task with no worker or monitor heartbeat is considered stuck |
| `QUEUE_TIMEOUT_SECONDS` | `int` or `None` | `86400` | Queued-wait budget after eligibility (`1`-`2147483647` seconds); `None` deliberately keeps an unlimited backlog |
| `WORKER_LEASE_SECONDS` | `int` | `60` | Worker lease duration (`1`-`86400` seconds) for distributed coordination |
| `WORKER_HEARTBEAT_SECONDS` | `int` | `15` | Heartbeat interval (`1`-`86400` seconds), which must be below the lease duration |
| `TASK_MONITOR_HEARTBEAT_SECONDS` | `int` | `15` | Minimum interval between database heartbeat writes for in-flight Ray Core tasks |
| `WORKFLOW_PROGRESS_REPORTING_POLICY` | `str` | `"full"` | Default Ray workflow progress policy: `"full"`, `"terminal_only"`, or `"disabled"` |
| `WORKFLOW_PROGRESS_SCHEMA_V3_PILOT` | `bool` | `False` | Experimental terminal schema-v3 publication for admitted full-reporting workflows |
| `WORKFLOW_PROGRESS_FLUSH_SECONDS` | `int` | `1` | Minimum interval between full-mode workflow progress snapshot writes |
| `WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS` | `int` | `15` | Total deadline for the final full-mode snapshot while a progress actor starts or drains (`1`-`60` seconds) |
| `WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS` | `int` | `7` | Terminal workflow topology and node-detail retention (`0`-`30` days) |

`django-ray` validates numeric settings at startup, rejects booleans passed as integers, and enforces
that worker/task-monitor heartbeats are shorter than their lease/stuck-task windows.
`django-ray` uses worker lease heartbeats to track worker liveness and task monitor
heartbeats to show that a worker is still actively reconciling in-flight work. For
persisted Ray Job handles from inactive workers, another worker will first try to
reconcile or adopt the existing job before timeout-based stuck recovery marks it lost.
Task monitor heartbeats are batched into one update for all in-flight tasks and
throttled by `TASK_MONITOR_HEARTBEAT_SECONDS`.
Ray-native workflow progress defaults to `"full"`. Full mode collects node events in
memory and writes a schema-v2 compatibility snapshot no more often than
`WORKFLOW_PROGRESS_FLUSH_SECONDS`; the interval limits database write frequency, not
producer admission or actor memory. Each reporting leaf invocation accepts validated
application progress into a best-effort session with at most one outstanding actor
acknowledgement and one canonical latest-value slot. A slow acknowledgement causes
later application updates to replace the slot; leaf exit makes at most one bounded
handoff before the non-coalesced `COMPLETED` or `FAILED` event. This is
acknowledgement-driven containment, not time-based sampling, and no `sampled` policy
is available.

A schema-v2 snapshot may include one versioned, fixed-shape producer
aggregate. It counts actor-accepted leaf-invocation reports, valid offers, submissions,
superseded and locally dropped values, producer-observed acknowledgement outcomes,
and terminal-handoff outcomes without retaining producer identities or application
values. A pending acknowledgement means that the leaf had not observed its result
when it sealed the report; it does not prove the actor failed to process the call.
A physical Ray leaf retry or another forked actor handle within the same run can still
create another independently bounded leaf session. An outer durable-task retry uses a
new run identity and actor. Aggregate workflow-wide mailbox admission and coalescing
therefore remain open prerequisites for a future sampled policy.

Use `"terminal_only"` when the outer task needs one bounded terminal observability
record without live node reporting. This mode creates no progress actor, sends no node
or application-progress RPCs, and never writes legacy `progress_data`. On durable
success or failure it makes exactly one best-effort, run-fenced schema-v3 summary
publication. The summary records the pinned strategy and plan fingerprint, declared
plan counts, terminal outcome, and bounded timestamps. It deliberately reports zero
discovered or executed nodes, sets detail availability to `OMITTED_BY_POLICY`, and
creates no topology manifest, page, or node-detail row. Summary serialization,
validation, or database attachment failure is observational and cannot replace the
workflow result or application error. A stale lifecycle fence accepts neither the
terminal task transition nor its summary.

Use `"disabled"` when even that terminal summary is not wanted. Disabled mode also
avoids the actor, progress RPCs, and `progress_data`, but makes no schema-v3 summary
publication. Calling
`WorkflowSignature.with_progress_reporting("terminal_only").run(...)` or
`WorkflowSignature.with_progress_reporting("disabled").run(...)` overrides the global
setting for one invocation without reserving an application task keyword. Local
execution remains actor-free and is recorded as disabled.

`WORKFLOW_PROGRESS_SCHEMA_V3_PILOT` is an experimental, default-off bridge from one
terminal full-reporting actor snapshot into the bounded schema-v3 summary, topology,
and node-detail storage. Enabling it applies the fixed `schema-v3-pilot-v1` profile to
both actor collection and publication: at most 512 nodes, 2,048 edges, 2 MiB of
topology, 1 MiB of detail, and 4 MiB combined, with the byte ceilings enforced for
both encoded and decoded evidence. This is deliberately below the hard protocol-v1
limits and is not a high-scale readiness claim.

The pilot flag applies only to full reporting. Terminal-only publication is already
summary-only, never starts the live actor, and does not opt into pilot topology or
node-detail collection.

Publication fails closed if actor ingress rejected or truncated an event, the
snapshot or pinned plan is inconsistent, a pilot admission or preparation limit is
exceeded, the exact run fence is stale, or storage cannot publish atomically. No
partial schema-v3 graph is exposed, and the workflow's application result is
unchanged. A staged topology candidate is discarded after a rejected or failed
publication, with cleanup failure reported explicitly. The bounded schema-v2
`progress_data` snapshots remain the live and rolling compatibility path regardless
of whether terminal schema-v3 publication succeeds.

The bundled testproject deliberately enables the pilot so its real workflow topology
can be exercised. Its
`DJANGO_RAY_WORKFLOW_PROGRESS_SCHEMA_V3_PILOT` environment variable controls that
exception and defaults to enabled; the
[guarded local KubeRay stack](deployment/local-kuberay-gate.md) uses the same
testproject setting and verifies the resulting summary, topology nodes, edges, and node
detail. The same gate separately exercises terminal-only success and failure with a
null legacy snapshot and no detail storage; that summary-only path does not depend on
the pilot setting. Production projects must opt into the full-detail pilot explicitly
after checking its limits against their workloads.

When a workflow finishes, the coordinator retries one pending actor snapshot for up to
`WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS`. Exhausting that bounded deadline
leaves task execution unaffected and emits a structured warning instead of silently
abandoning a requested full-reporting snapshot. This coordinator deadline is separate
from each leaf producer's one bounded terminal latest-value handoff before its
non-coalesced terminal event.
Terminal topology and node detail become eligible for cleanup after
`WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS`; `0` makes them eligible as soon as the
terminal state is durably archived. Active current detail is not expired by this
setting, and bounded per-attempt summaries remain subject to task-attempt retention.
Terminal-only summaries retain the configured policy value for audit consistency but
have no detail expiry because no detail rows are created.

RuntimeEnv profiles are resolved and stored when a task is enqueued. See
[Runtime Environments](runtime-environments.md) for inheritance, backend aliases,
workflow leaf overrides, cache behavior, encrypted snapshot configuration, and the
required rolling-deployment order.

### Inputs

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `MAX_INLINE_INPUT_SIZE_BYTES` | `int \| None` | `None` | Combined input-envelope threshold; `None` disables spillover |
| `INPUT_STORAGE_BACKEND` | `str \| None` | `None` | Retrievable task-input and Ray Job request backend: `"filesystem"`, `"s3"`, or `"gcs"` |
| `INPUT_STORAGE_FILESYSTEM_PATH` | `str \| None` | `None` | Shared root required for the filesystem backend |
| `INPUT_STORAGE_S3_BUCKET` | `str \| None` | `None` | Bucket required for the S3 backend |
| `INPUT_STORAGE_S3_PREFIX` | `str` | `"django-ray/inputs"` | S3 object-key prefix |
| `INPUT_STORAGE_S3_REGION` | `str \| None` | `None` | Optional S3 region |
| `INPUT_STORAGE_S3_ENDPOINT_URL` | `str \| None` | `None` | Optional S3-compatible endpoint |
| `INPUT_STORAGE_GCS_BUCKET` | `str \| None` | `None` | Bucket required for the GCS backend |
| `INPUT_STORAGE_GCS_PREFIX` | `str` | `"django-ray/inputs"` | GCS object-key prefix |

Argument spillover is opt-in. Configure a retrievable backend before setting the
threshold; digest-only storage is not valid for inputs. Ray Job task managers require a
retrievable backend even when the threshold remains `None`, because every new rq2
submission externalizes its complete canonical execution request before opening the Ray
client. Synchronous and Ray Core workers remain storage-optional while spillover is
disabled. See
[Durable Input Storage](reference/input-storage.md) for rollout, retention, and backend
requirements.

Input references use the same exact canonical grammar and configuration-bound object authority as
results, and validation completes before storage-client or credential-provider initialization.
New writes use `INPUT_STORAGE_BACKEND`; reads and cleanup can continue dispatching to a configured
retained namespace with a different scheme. Same-scheme bucket/prefix and filesystem-root changes
require the migration sequence in the input-storage reference. Input and result storage must use
different filesystem roots or different object-store namespaces because input retention is allowed
to delete objects; identical configured namespaces fail startup validation. An S3-compatible
endpoint is part of that namespace identity, while the signing region is not.

### Results

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `MAX_RESULT_SIZE_BYTES` | `int` | `1048576` | Maximum inline result size in `result_data` (larger payloads use `result_reference`) |
| `RESULT_STORAGE_BACKEND` | `str` | `"digest"` | Oversized result backend: `"digest"`, `"filesystem"`, `"s3"`, or `"gcs"` |
| `RESULT_STORAGE_FILESYSTEM_PATH` | `str \| None` | `None` | Required when backend is `"filesystem"` |
| `RESULT_STORAGE_S3_BUCKET` | `str \| None` | `None` | Required when backend is `"s3"` |
| `RESULT_STORAGE_S3_PREFIX` | `str` | `"django-ray/results"` | Object key prefix for `"s3"` backend |
| `RESULT_STORAGE_S3_REGION` | `str \| None` | `None` | Optional S3 region |
| `RESULT_STORAGE_S3_ENDPOINT_URL` | `str \| None` | `None` | Optional S3-compatible endpoint |
| `RESULT_STORAGE_GCS_BUCKET` | `str \| None` | `None` | Required when backend is `"gcs"` |
| `RESULT_STORAGE_GCS_PREFIX` | `str` | `"django-ray/results"` | Object key prefix for `"gcs"` backend |

When `RESULT_STORAGE_BACKEND` is:

- `digest` (default): oversized payload is not persisted externally; a digest pointer is written.
- `filesystem`: oversized payload is written to the configured filesystem path and a pointer is written.
- `s3`: oversized payload is written to S3 (or compatible object storage) and a pointer is written.
- `gcs`: oversized payload is written to Google Cloud Storage and a pointer is written.

`RayTaskBackend.get_result()` can automatically reload oversized payloads from
`filesystem`, `s3`, and `gcs` references when the reading process has the same
storage configuration and credentials available. `digest` references remain
metadata-only and do not restore the original return value.

External references are authorized against the current filesystem root or object-store
bucket and prefix, then verified against their exact UTF-8 byte count and SHA-256 digest
before use. Bucket, prefix, or filesystem-root changes therefore require the rotation
procedure in [Result Storage](reference/result-storage.md#configuration-rotation-and-legacy-references);
namespace-mismatched references fail closed. Startup validates active writers and any
configured inactive namespaces retained for historical reads. Prefixes must be canonical
and capable of producing references that fit the database field. The only non-canonical
read compatibility is the encoding-only S3/GCS format produced by django-ray 0.2 and 0.3;
see the migration procedure in the linked reference.

Optional install extras:

- `pip install "django-ray[s3]"` for S3 backend dependencies.
- `pip install "django-ray[gcs]"` for GCS backend dependencies.
- `pip install "django-ray[object-storage]"` for both.

### Admin presentation

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `TASK_ATTEMPT_ADMIN_MODE` | `str` | `"inline"` | Attempt-history presentation: `"inline"`, `"standalone"`, or `"both"` |

The default `inline` mode shows immutable attempt history on an existing
`RayTaskExecution` change page and hides the top-level `TaskAttempt` entry from the
admin index. `standalone` restores the previous top-level presentation without the
inline, while `both` enables both navigation paths. Selection happens per request;
`TaskAttempt` remains registered and authorized list/detail bookmarks remain valid in
every mode.

The inline requires permission to view the parent execution plus global
`view_taskattempt` or `change_taskattempt` permission. Direct attempt detail views
also accept object-specific child permission. The setting controls presentation, not
access. See [Settings Reference](reference/settings.md#task_attempt_admin_mode) for
the bounded diagnostic fields and custom-`AdminSite` contract.

### Redaction and operational output

`REDACT_PATTERNS` is an optional sequence of bounded pattern expressions used for
worker logs, structured log fields, Ray State API/log responses, the sample
operational API, and bounded diagnostic fields in the Django admin task detail
view. When it is `None`, the built-in patterns cover common names such as
`password`, `secret`, `token`, `authorization`, `cookie`, and `private_key`.
A matching mapping key is replaced with the fixed `<redacted>` marker and its
value becomes `[REDACTED]`; a matching string is replaced with `[REDACTED]`.
Marker or normalized-key collisions also become `[REDACTED]` regardless of
mapping order.

The expressions use case-insensitive substring-search semantics. They support literals
and escaped literals, `.`, positive or negated character classes and ranges, capturing
or noncapturing groups, alternation, `?`, `*`, `+`, bounded/open numeric repetition,
and `\d`, `\D`, `\s`, `\S`, `\w`, and `\W`. Zero-width anchors or word boundaries,
lookaround, backreferences, inline flags, named/conditional/atomic groups, possessive
quantifiers, and any expression which can match empty text are rejected during Django
startup. Direct `redact_text(..., patterns=...)` and `redact_value(..., patterns=...)`
arguments use the same validator; they never fall back to raw-only matching.

At most 64 configured entries are accepted. Each source is limited to 256 UTF-8 bytes
and configured sources to 4,096 bytes in aggregate. The built-ins remain independently
active; the combined program admits at most 80 expressions and 8,192 source bytes.
Compilation allows at most 512 states per expression, 2,048 in aggregate, numeric repeat
bounds through 128, and group depth 32. Evaluation shares a 250,000-unit matcher ceiling
across every terminal representation and nested member of one root value. One structured
root also admits at most 4,096 visited items and 65,536 aggregate characters across its
string keys and values. Exhausting any boundary fails closed to a type-compatible
`[REDACTED]` projection. Pattern iterables stop at the configured count boundary rather
than being materialized without limit.

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "REDACT_PATTERNS": [
        r"password",
        r"access[_-]?token",
        r"customer[_-]?email",
    ],
}
```

After upgrading, run `python manage.py check` with production settings before starting
workers. An unsupported entry is reported by its configured index without echoing its
source. Replace anchors and boundaries with the intended consuming marker, expand a
backreference/lookaround into bounded alternatives, or perform richer classification in
application code before passing a small marker to the redaction boundary.

Package-owned management commands apply the same policy when they render
exceptions or Ray control-plane diagnostics. An exception's bounded class label
and provider message enter one matching boundary, so a configured pattern may
match the label, the message, or text spanning their separator. Before a
diagnostic reaches the console, ANSI and control-string introducers are made
inert, C0/C1 and unsafe Unicode formatting controls are neutralized, and
configured secret patterns are evaluated across every bounded terminal
interpretation. Oversized input and exhausted matching budgets fail closed before
the rendered result is limited to 4 KiB; output truncation therefore cannot
bypass a configured pattern.

This diagnostic boundary does not rewrite ordinary command structure or durable
identity. Task, attempt, generation, and worker IDs; lifecycle enums; numeric
counts; and benchmark JSON keys remain unchanged. Custom Ray resource names use
the same terminal-safe redaction boundary while numeric counts remain intact; the
summary is capped at 256 entries and 4 KiB. Machine-readable benchmark output
keeps its documented schema while exception-bearing diagnostic fields use the
safe renderer. Type-only benchmark and input-purge failure fields apply configured
patterns to the validated exception class label; input purge never materializes
the provider message.

Successful task logs and task-manager status lines use a fixed success marker;
they never traverse or print the complete return value merely to describe it.
Ray Job completion envelopes are persisted through the database channel and are
not printed to Ray stdout. Redaction is a safety boundary for operational output,
not encryption: durable task failure and cancellation diagnostics, task results,
and arguments remain available to authorized readers. Framework lifecycle paths
guard provider exception conversion with a fixed fallback, but do not treat that
fallback as storage redaction. Application code that prints directly to stdout
bypasses this policy. Protect the API, admin, Ray dashboard, and result storage
with the appropriate access controls.
The admin never renders the raw durable RuntimeEnv snapshot because arbitrary
`env_vars`, package references, and URIs cannot be made safe through
name-pattern redaction alone. It shows only the profile and content hash.
`RUNTIME_ENV_STORAGE_MODE="encrypted"` can protect that one database column from
read-only database and backup exposure, but it does not encrypt task inputs,
results, progress, Ray transport, process memory, or application-created logs.

`RayTaskExecution` detail fields are read-only, including queue, priority,
lifecycle state, attempt/generation identity, and worker ownership. Queue and
priority influence claim ordering only while work is queued, but changing them
through a generic model save would bypass state fencing and can race a worker
claim. Use the task-list Retry and Cancel actions for package-owned control
transitions.

## Startup Validation

django-ray validates `DJANGO_RAY` at Django app startup (`AppConfig.ready()`).
Invalid configuration raises `ImproperlyConfigured` by default (fail-fast).

Validation is skipped only in these cases:

- management command is one of: `migrate`, `makemigrations`, `showmigrations`, `collectstatic`
- environment variable `DJANGO_RAY_SKIP_VALIDATION` is set to `1`, `true`, or `yes`

The env override is intended for bootstrap/maintenance flows where full Ray config is
not available yet.

## Example Configurations

### Development

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 5,
    "MAX_TASK_ATTEMPTS": 1,  # Fail fast during development
    "STUCK_TASK_TIMEOUT_SECONDS": 60,
}
```

### Production

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 50,
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 120,
    "STUCK_TASK_TIMEOUT_SECONDS": 600,
    "WORKER_LEASE_SECONDS": 120,
    "WORKER_HEARTBEAT_SECONDS": 30,
    "TASK_MONITOR_HEARTBEAT_SECONDS": 15,
    "WORKER_POLL_INTERVAL_SECONDS": 0.1,
    "WORKER_POLL_MAX_INTERVAL_SECONDS": 0.5,
}
```

### High-Throughput Starting Point

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNNER": "ray_core",
    "DEFAULT_CONCURRENCY": 100,
    "MAX_TASK_ATTEMPTS": 5,
    "RETRY_BACKOFF_SECONDS": 30,
}
```

## Environment Variables

`django-ray` reads runtime settings from Django's `DJANGO_RAY` setting. The sample project and
Docker entrypoint in this repository also map these environment variables into settings or worker
CLI flags:

| Variable | Used by | Description |
|----------|---------|-------------|
| `RAY_ADDRESS` | sample settings, Docker entrypoint | Ray cluster address (`"auto"` locally, `ray://...` for clusters) |
| `RAY_DASHBOARD_URL` | sample settings | Ray Dashboard URL used by Django admin deep links |
| `DJANGO_RAY_QUEUE` | Docker entrypoint | Queue name passed to `django_ray_worker --queue` |
| `DJANGO_RAY_QUEUES` | Docker entrypoint | Comma-separated queues passed to `django_ray_worker --queue`; takes precedence over `DJANGO_RAY_QUEUE` |
| `DJANGO_RAY_CONCURRENCY` | Docker entrypoint | Concurrency passed to `django_ray_worker --concurrency` |
| `DJANGO_RAY_SKIP_VALIDATION` | django-ray app config | Skip startup setting validation (maintenance/bootstrap only) |

## Django Tasks Configuration

django-ray integrates with Django's native Tasks framework. Configure the backend in `TASKS`:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default", "high-priority", "low-priority"],
    },
}
```

`DEFAULT_CONCURRENCY=100` is not a universal recommendation. It controls how many
durable tasks one task manager can keep active; Ray resources, database capacity, and
external API limits may require a much lower value. See [Performance](performance.md).

Backend `OPTIONS` may select a named environment. This is a complete second alias:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"RUNTIME_ENV_PROFILE": "project"},
    },
    "numpy": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"RUNTIME_ENV_PROFILE": "numpy-2-3"},
    },
}
```

## See Also

- [Worker Modes](worker-modes.md) - How different modes affect configuration
- [Runtime Environments](runtime-environments.md) - Per-task dependencies and code
- [Durable Input Storage](reference/input-storage.md) - Oversized JSON input handling
- [Retry & Error Handling](retry.md) - Detailed retry configuration

