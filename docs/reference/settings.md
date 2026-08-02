# Settings Reference

Complete reference for all django-ray settings.

Per-task execution timeouts are configured on the Django `TASKS` backend, not in
`DJANGO_RAY`. See [Defining Tasks](../tasks.md#per-task-timeouts) for the backend
option and mode-specific timeout behavior.

## DJANGO_RAY

All settings are configured under the `DJANGO_RAY` dictionary in your Django settings:

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://localhost:10001",
    "DEFAULT_CONCURRENCY": 10,
    # ... other settings
}
```

## Startup Validation Policy

django-ray validates settings on app startup and fails fast on invalid config.
This happens in `django_ray.apps.DjangoRayConfig.ready()`.

Validation is skipped only when:

- running one of: `migrate`, `makemigrations`, `showmigrations`, `collectstatic`
- `DJANGO_RAY_SKIP_VALIDATION` is set to `1`, `true`, or `yes`

`DJANGO_RAY_SKIP_VALIDATION` is an environment override, not a `DJANGO_RAY` key.

## Ray Connection

### RAY_ADDRESS

- **Type**: `str | None`
- **Default**: `None`
- **Required**: Yes at runtime unless startup validation is explicitly skipped

Ray cluster address. Use `"auto"` for local development or `"ray://host:port"` for an explicit
cluster address. Sync mode does not submit work to Ray, but application startup validation still
requires this setting unless `DJANGO_RAY_SKIP_VALIDATION` is used for a maintenance/bootstrap flow.

```python
# Local Ray (auto-detect)
"RAY_ADDRESS": "auto"

# Remote cluster
"RAY_ADDRESS": "ray://ray-head-svc:10001"
```

Short examples in this reference are individual dictionary entries to place inside
`DJANGO_RAY`; they are intentionally not standalone Python modules. Longer examples
show the complete dictionary.

### RAY_STATE_API_ADDRESS

- **Type**: `str | None`
- **Default**: `None`

Ray dashboard address used by the optional workflow-node state and log helpers.
Processes already initialized with Ray can leave this unset. A separate Django web
process normally needs the internal dashboard URL:

```python
"RAY_STATE_API_ADDRESS": "http://ray-head-svc:8265"
```

Ray's State API is live operational data rather than durable state. Queries may be
partial or stale, and logs from dead nodes are unavailable.

### RAY_STATE_API_TIMEOUT_SECONDS

- **Type**: `int`
- **Default**: `5`
- **Range**: `1` to `60`

Timeout applied independently to optional Ray state and log API requests.

### RAY_RUNTIME_ENV

- **Type**: `dict`
- **Default**: `{}`

Unnamed default Ray runtime environment. It is resolved and stored on each task
when no named profile is selected.

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "RAY_RUNTIME_ENV": {
        "pip": ["pandas", "numpy"],
        "env_vars": {"MY_VAR": "value"},
    },
}
```

### RUNTIME_ENV_PROFILES

- **Type**: `dict`
- **Default**: `{}`

Named Ray RuntimeEnv definitions. A direct profile is a RuntimeEnv dictionary. A
composed profile has `extends` and `runtime_env` keys. Profile inheritance merges
dictionaries and appends the `pip`, `uv`, `py_modules`, and `excludes` lists.

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNTIME_ENV_PROFILES": {
        "project": {
            "working_dir": "s3://deployments/myapp/7f3a2c1.zip",
            "pip": ["django>=6.0"],
        },
        "numpy": {
            "extends": "project",
            "runtime_env": {"pip": ["numpy==2.3.5"]},
        },
    },
}
```

### DEFAULT_RUNTIME_ENV_PROFILE

- **Type**: `str | None`
- **Default**: `None`

Profile selected when the task backend does not specify
`OPTIONS["RUNTIME_ENV_PROFILE"]`. The named profile must exist.

### RUNTIME_ENV_STORAGE_MODE

- **Type**: `str`
- **Default**: `"plaintext"`
- **Values**: `"plaintext"` or `"encrypted"`

Selects the format for newly enqueued RuntimeEnv snapshots. Readers always accept
both supported formats, so changing this setting does not rewrite existing rows.
`encrypted` requires `RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY` to resolve through the
dedicated key ring or the explicit Django-secret fallback.

Keep `plaintext` during the first dual-read and key-distribution stages of a rolling
upgrade. See [Runtime Environments](../runtime-environments.md#roll-out-encrypted-writes)
before enabling encrypted writes.

### RUNTIME_ENV_ENCRYPTION_KEYS

- **Type**: `dict[str, str]`
- **Default**: `{}`

Dedicated AES-256-GCM key ring used to encrypt and decrypt RuntimeEnv snapshots.
Mapping keys are case-sensitive key IDs. Each value must be the unpadded canonical
base64url representation of exactly 32 random bytes.

Key IDs must start with a letter or number and contain at most 64 letters, numbers,
dots, underscores, or hyphens. The ID `django-secret` is reserved. Store values in a
secret manager or environment variable, not source control:

```python
import os


DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNTIME_ENV_ENCRYPTION_KEYS": {
        "runtime-env-2026-01": os.environ["DJANGO_RAY_RUNTIME_ENV_KEY_2026_01"],
        "runtime-env-2025-10": os.environ["DJANGO_RAY_RUNTIME_ENV_KEY_2025_10"],
    },
    "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "runtime-env-2026-01",
    "RUNTIME_ENV_STORAGE_MODE": "encrypted",
}
```

Keep retired keys available to every reader while any durable row may still name
them. Key loss is not recoverable from the database envelope.

### RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY

- **Type**: `str | None`
- **Default**: `None`

Key ID used for new encrypted writes. When configured, it must resolve to a key in
`RUNTIME_ENV_ENCRYPTION_KEYS` or to the reserved `django-secret` fallback. This is
validated even in plaintext mode so a staged rollout cannot silently retain an
unusable active key.

Changing the active key does not rewrap historical rows. Add the new key to every
reader first, then change this setting, and retain the old key until no row needs it.
Always give new dedicated key material a new key ID; never replace the bytes under an
existing dedicated ID.

### RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK

- **Type**: `bool`
- **Default**: `False`

Explicitly enables the reserved `django-secret` key ID. New writes derive a 32-byte
AES key from Django's current `SECRET_KEY` with HKDF-SHA256 and a versioned
django-ray domain context. Reads try the current key followed by
`SECRET_KEY_FALLBACKS`; fallback indexes are never stored in the envelope.

This mode is never selected automatically. To use it:

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNTIME_ENV_STORAGE_MODE": "encrypted",
    "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "django-secret",
    "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": True,
}
```

Prefer dedicated keys for production because Django signing-key and durable-task
retention schedules are often different. If this fallback is used, retain an old
`SECRET_KEY` in `SECRET_KEY_FALLBACKS` for as long as an encrypted RuntimeEnv row may
need it.

### WORKFLOW_PLAN_CODE_REVISION

- **Type**: `str | None`
- **Default**: `None`

Immutable, non-secret application build identity included in every effective workflow
plan. It is optional for the current local and dynamic-task baseline but required
before a future reusable strategy can accept the plan. Prefer a build revision,
verified archive digest, or immutable source revision. django-ray also fingerprints
directly imported callable module bytes; this setting supplies the deployment-wide and
transitive-dependency dimension that one module path cannot express. Values are
limited to 256 characters.

When omitted, django-ray checks `DJANGO_RAY_BUILD_REVISION`, `GIT_COMMIT`,
`SOURCE_VERSION`, and `K_REVISION` in that order. Independently, it reads
`DJANGO_RAY_IMAGE_DIGEST` as a bare `sha256:<64 hexadecimal digits>` container identity.
The build revision and container image digest are separate fingerprint inputs: finding
one never suppresses or substitutes for the other. A non-empty malformed image digest
fails plan materialization, and a digest that disagrees with an immutable Compiled Graph
deployment profile rejects reusable strategies. A missing deployment-wide build
revision does not block dynamic tasks, but adds
`UNRESOLVED_CODE_IDENTITY` and rejects reusable strategies. Direct callable-module
hashes alone cannot cover imported helpers, settings, templates, or other transitive
application dependencies.

### WORKFLOW_PLAN_TRUST_IDENTITY

- **Type**: `dict`
- **Default**: `{}`

Bounded non-secret identity for trust and credential-provider behavior that affects
safe actor or graph reuse. Only `trust_domain`, `credential_provider`,
`credential_profile`, `credential_revision`, `environment_revision`,
`scheduling_revision`, and `service_account_audience` are accepted; every value must
be a non-empty string of at most 256 characters.

Never put a token, password, private key, certificate, kubeconfig, or a digest of such
material here. `credential_revision` names the provider/profile contract. Token
rotation under the same contract is intentionally excluded from the plan, while a
provider or revision change invalidates prepared state.
`environment_revision` may cover ordinary environment or Conda variable values that
must not be represented individually; change it whenever any covered value changes.
`scheduling_revision` separately covers semantic Ray label selectors and fallback
placement constraints. An environment revision does not cover scheduling.

```python
"WORKFLOW_PLAN_TRUST_IDENTITY": {
    "trust_domain": "cluster:production",
    "credential_provider": "workload-identity",
    "credential_profile": "namespace-sync",
    "credential_revision": "provider-v3",
    "environment_revision": "namespace-sync-v8",
    "scheduling_revision": "placement-v2",
}
```

## Concurrency

### DEFAULT_CONCURRENCY

- **Type**: `int`
- **Default**: `10`

Maximum number of concurrent tasks per worker.

```python
"DEFAULT_CONCURRENCY": 50
```

## Worker Polling

### WORKER_POLL_INTERVAL_SECONDS

- **Type**: `int | float` (booleans are rejected)
- **Default**: `0.1`
- **Allowed**: `0.01` to `10`

Base claim-query interval. Activity resets adaptive backoff to this value.

### WORKER_POLL_MAX_INTERVAL_SECONDS

- **Type**: `int | float` (booleans are rejected)
- **Default**: `0.1`
- **Allowed**: `0.01` to `60`, and greater than or equal to the base interval

Maximum delay between claim queries after consecutive empty polls. Bounded jitter can
shorten a particular delay but never extends it beyond this maximum. This setting does
not change Ray completion polling, heartbeat, reconciliation, timeout, or cancellation
schedules. The default equals the base interval, so increasing it is an explicit
idle-backoff tuning choice.

```python
"WORKER_POLL_INTERVAL_SECONDS": 0.1,
"WORKER_POLL_MAX_INTERVAL_SECONDS": 0.5,
```

## Runner Selection

### RUNNER

- **Type**: `str`
- **Default**: `"ray_job"`
- **Allowed**: `"ray_job"`, `"ray_core"`

Default runner selection when no execution mode CLI flag is provided:

- `ray_job`: use Ray Job Submission API mode.
- `ray_core`: use Ray Core mode:
  - if `RAY_ADDRESS == "auto"` -> local mode
  - otherwise -> cluster mode using `RAY_ADDRESS`

CLI flags (`--sync`, `--local`, `--cluster`) always take precedence.

## Retry Policy

### MAX_TASK_ATTEMPTS

- **Type**: `int`
- **Default**: `3`

Maximum number of attempts before marking a task as failed. Includes the initial attempt.

```python
"MAX_TASK_ATTEMPTS": 5  # Initial + 4 retries
```

### RETRY_BACKOFF_SECONDS

- **Type**: `int`
- **Default**: `60`
- **Allowed**: `0` to `3600`

Base delay in seconds between retry attempts. Uses exponential backoff:
- Attempt 2: `RETRY_BACKOFF_SECONDS * 1`
- Attempt 3: `RETRY_BACKOFF_SECONDS * 2`
- Attempt 4: `RETRY_BACKOFF_SECONDS * 4`

```python
"RETRY_BACKOFF_SECONDS": 120  # 2 minutes base delay
```

### RETRY_EXCEPTION_DENYLIST

- **Type**: `list[str]`
- **Default**: `[]`

List of exception class names that should not be retried. Supports short names
and fully qualified names.

```python
"RETRY_EXCEPTION_DENYLIST": [
    "ValueError",
    "KeyError",
    "myapp.exceptions.PermanentError",
]
```

## Reliability

### QUEUE_TIMEOUT_SECONDS

- **Type**: `int | None` (booleans are rejected)
- **Default**: `86400` (24 hours)
- **Allowed**: `1` to `2147483647`, or `None`

Maximum time that an execution may remain queued after it becomes eligible. django-ray
snapshots the effective value on every enqueue. The absolute deadline is the later of
enqueue time and `run_after`, plus this budget. A worker reaching that deadline records
terminal `EXPIRED` without submitting the task to Ray or automatically retrying it.
An explicit or automatic retry refreshes the deadline from the new eligibility time;
shutdown handoff of an unsubmitted claim does not.

A Django Tasks backend alias may override the process-wide value through
`OPTIONS["QUEUE_TIMEOUT_SECONDS"]`. Set it explicitly to `None` only for a deliberately
unlimited queue with idempotent tasks, backlog alerts, and an operator drain or discard
policy. See [Queue expiration](../tasks.md#queue-expiration) for backend examples and the
existing-backlog migration procedure.

### STUCK_TASK_TIMEOUT_SECONDS

- **Type**: `int`
- **Default**: `300` (5 minutes)

Time in seconds after which a running task with no updates is considered stuck and marked as LOST.

This timeout is evaluated from `last_heartbeat_at` (falling back to `started_at`).
That heartbeat can come from the worker lease path or from active task-monitor updates
while a worker is still reconciling in-flight Ray work.

For persisted Ray Job handles from inactive workers, `django-ray` first attempts to
reconcile or adopt the existing job before the stale task is marked `LOST` and routed
through retry handling.

```python
"STUCK_TASK_TIMEOUT_SECONDS": 600  # 10 minutes
```

### WORKER_LEASE_SECONDS

- **Type**: `int`
- **Default**: `60`
- **Allowed**: `1` to `86400`

Duration of worker lease for distributed coordination. Workers must renew their lease within this period.
`WORKER_HEARTBEAT_SECONDS` must be lower than this value.

```python
"WORKER_LEASE_SECONDS": 120
```

### WORKER_HEARTBEAT_SECONDS

- **Type**: `int`
- **Default**: `15`
- **Allowed**: `1` to `86400`, and less than `WORKER_LEASE_SECONDS`

Interval between worker heartbeats. Should be less than `WORKER_LEASE_SECONDS`.

This controls lease freshness for worker coordination. Task monitor heartbeats for
actively reconciled in-flight work are updated separately.

```python
"WORKER_HEARTBEAT_SECONDS": 30
```

### TASK_MONITOR_HEARTBEAT_SECONDS

- **Type**: `int`
- **Default**: `15`
- **Allowed**: `1` to `300`

Minimum interval between database heartbeat updates for in-flight Ray Core tasks.
Each update covers all tasks currently monitored by that worker. Status polling remains
non-blocking and frequent; this setting only throttles persistence writes.

Keep this value comfortably below `STUCK_TASK_TIMEOUT_SECONDS`.
Validation requires it to be strictly less than `STUCK_TASK_TIMEOUT_SECONDS`.

```python
"TASK_MONITOR_HEARTBEAT_SECONDS": 15
```

### WORKFLOW_PROGRESS_REPORTING_POLICY

- **Type**: `str`
- **Default**: `"full"`
- **Allowed**: `"full"`, `"terminal_only"`, or `"disabled"`

Default node-reporting policy for Ray-native workflow invocations. `"full"` preserves
the progress actor, per-node events, application progress, and periodic
`progress_data` snapshots. A full-reporting leaf invocation holds at most one
outstanding application-progress acknowledgement plus one canonical latest-value
slot and makes at most one bounded terminal handoff/report. `STARTED`, `COMPLETED`,
`FAILED`, and other structural or lifecycle events are never coalesced. This is
acknowledgement-driven containment, not time-based sampling; no `sampled` policy is
available. `"terminal_only"` creates no progress actor, sends no node or
application-progress RPCs, and writes no `progress_data`; it makes one best-effort
fenced schema-v3 summary publication when the durable workflow succeeds or fails.
That summary has `OMITTED_BY_POLICY` detail and creates no topology or node-detail
rows. `"disabled"` uses the same actor-free path without the terminal summary attempt.
Neither actor-free policy disables task claiming, result/error persistence, retry and
cancellation fencing, or task-monitor heartbeats.

Call `WorkflowSignature.with_progress_reporting(policy).run(...)` to override this
setting for one invocation. The fluent configuration keeps package execution options
separate from keyword arguments forwarded to the root application callable.

```python
"WORKFLOW_PROGRESS_REPORTING_POLICY": "terminal_only"
```

### WORKFLOW_PROGRESS_SCHEMA_V3_PILOT

- **Type**: `bool`
- **Default**: `False`

Experimental terminal publication of admitted full-reporting workflow progress into
the schema-v3 summary, topology, and normalized node-detail storage. The package
default is disabled. A value such as `1`, `"true"`, or `None` is not accepted in
`DJANGO_RAY`; configure a real Python boolean.

When enabled, both the progress actor and the terminal publication adapter use the
fixed `schema-v3-pilot-v1` admission profile:

| Evidence | Maximum items | Maximum encoded bytes | Maximum decoded bytes |
|---|---:|---:|---:|
| Topology nodes | 512 | 2 MiB for all topology | 2 MiB for all topology |
| Topology edges | 2,048 | 2 MiB for all topology | 2 MiB for all topology |
| Node detail | 512 | 1 MiB | 1 MiB |
| Combined topology and detail | — | 4 MiB | 4 MiB |

The adapter makes one best-effort publication attempt from a complete terminal actor
snapshot while the exact task, attempt, execution generation, and workflow run still
own the `RUNNING` fence. It derives policy, strategy, and plan identity from the pinned
effective selection. Any rejected or truncated ingress, malformed cross-field
evidence, admission overflow, preparation truncation, stale fence, or atomic storage
failure refuses schema-v3 publication instead of presenting an incomplete graph as
complete. Publication failure does not change the workflow result. Full mode continues
to write bounded schema-v2 `progress_data` snapshots for live and rolling
compatibility; disabled reporting still creates no progress actor and publishes no
progress data.

The bundled testproject is intentionally different from the package default:
`DJANGO_RAY_WORKFLOW_PROGRESS_SCHEMA_V3_PILOT` defaults to enabled there so the sample
workflow and guarded local KubeRay gate exercise the real producer path. Set that
environment variable to a false value to test the package's ordinary default-off
behavior.

This pilot is not support for workloads near the hard protocol-v1 ceilings or for
arbitrary concurrent publication. Keep it disabled unless the workload fits the
documented profile and the deployment has been validated.

```python
"WORKFLOW_PROGRESS_SCHEMA_V3_PILOT": True
```

### WORKFLOW_PROGRESS_FLUSH_SECONDS

- **Type**: `int`
- **Default**: `1`
- **Allowed**: `1` to `300`

Minimum interval between database writes of an active full-reporting Ray-native
workflow's progress snapshot. Leaf events are collected by a per-workflow Ray actor.
This setting limits snapshot write frequency; it is not a sampling or producer
backpressure interval. Per-leaf application progress is independently contained by one
outstanding acknowledgement and one canonical latest-value slot, but this setting
does not bound aggregate RPC count, actor mailbox depth, or actor memory across forked
handles. Every write is conditional on the current task attempt, execution generation,
lifecycle state, and workflow run ID.

```python
"WORKFLOW_PROGRESS_FLUSH_SECONDS": 1
```

### WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS

- **Type**: `int`
- **Default**: `15`
- **Allowed**: `1` to `60`

Total deadline for the final full-reporting snapshot after a Ray-native workflow
finishes. The coordinator keeps polling one pending actor request while a newly
scheduled progress actor starts or drains leaf events. If no snapshot becomes
available before the deadline, task execution remains unaffected and a structured
warning records the unavailable producer.

```python
"WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": 15
```

### WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS

- **Type**: `int` (booleans are rejected)
- **Default**: `7`
- **Allowed**: `0` to `30`

Number of whole days to retain terminal workflow topology and node detail. A value of
`0` makes terminal detail eligible for cleanup as soon as the terminal state is durably
archived; cleanup is still a separate operation. Active current detail is never made
eligible by this setting. The bounded terminal summary stored with the task attempt
follows task-attempt retention and remains available after detail expires.

```python
"WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS": 7
```

## Admin Presentation

### TASK_ATTEMPT_ADMIN_MODE

- **Type**: `str`
- **Default**: `"inline"`
- **Values**: `"inline"`, `"standalone"`, or `"both"`

Controls where immutable `TaskAttempt` history is presented in Django admin:

- `inline` shows ordered contextual history on an existing `RayTaskExecution` change
  page and hides the top-level `TaskAttempt` entry from the app index.
- `standalone` retains the independent attempt changelist and does not add the
  contextual inline.
- `both` provides both presentations.

The mode is read at request time. `TaskAttempt` stays registered in every mode, so
authorized changelist/detail URLs and existing bookmarks remain valid even when
top-level navigation is hidden. Changing the mode never grants or revokes access.

The inline shows attempt number, state, start/finish times, and a redacted error
preview capped at 512 characters. An oversized message is replaced by a fixed prompt
to open the bounded detail view. Tracebacks, results, result references, and workflow
summaries are not selected or rendered by the inline.

A caller must be authorized to view the parent execution and must also have global
`view_taskattempt` or `change_taskattempt` permission before Django renders the
inline. Object-specific child permission is checked on direct attempt detail
requests; it is not evaluated once per inline row because the generic Django inline
has no queryset-level object-permission contract.

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "TASK_ATTEMPT_ADMIN_MODE": "both",
}
```

A fully custom `AdminSite` may replace the package presentation, but it then owns its
permission, redaction, and query-bounding behavior. Register both
`RayTaskExecution` and `TaskAttempt` on the same site when using the package inline,
otherwise Django cannot resolve its stock attempt-detail link.

## Durable Inputs

### MAX_INLINE_INPUT_SIZE_BYTES

- **Type**: `int | None`
- **Default**: `None`
- **Allowed**: `1024` through `104857600` bytes, or `None`

Maximum UTF-8 byte size of the combined, versioned input envelope to keep inline.
`None` disables spillover and preserves the legacy `args_json`/`kwargs_json` behavior.
Enabling a threshold requires a retrievable `INPUT_STORAGE_BACKEND`.

```python
"MAX_INLINE_INPUT_SIZE_BYTES": 1024 * 1024
```

### INPUT_STORAGE_BACKEND

- **Type**: `str | None`
- **Default**: `None`
- **Allowed**: `"filesystem"`, `"s3"`, `"gcs"`, or `None`

Backend used for inputs larger than `MAX_INLINE_INPUT_SIZE_BYTES`. Digest-only storage
is not supported because the worker must recover the original arguments.

### INPUT_STORAGE_FILESYSTEM_PATH

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `INPUT_STORAGE_BACKEND == "filesystem"`

Root for content-addressed input envelopes. Use a shared volume for multi-host workers.

```python
"INPUT_STORAGE_FILESYSTEM_PATH": "/var/lib/django-ray/inputs"
```

### INPUT_STORAGE_S3_BUCKET

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `INPUT_STORAGE_BACKEND == "s3"`

S3 or S3-compatible bucket for durable input envelopes.

### INPUT_STORAGE_S3_PREFIX

- **Type**: `str`
- **Default**: `"django-ray/inputs"`

Authorized object-key prefix for S3 input payloads.

### INPUT_STORAGE_S3_REGION

- **Type**: `str | None`
- **Default**: `None`

Optional region passed to the S3 client.

### INPUT_STORAGE_S3_ENDPOINT_URL

- **Type**: `str | None`
- **Default**: `None`

Optional endpoint for an S3-compatible provider such as MinIO.

### INPUT_STORAGE_GCS_BUCKET

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `INPUT_STORAGE_BACKEND == "gcs"`

Google Cloud Storage bucket for durable input envelopes.

### INPUT_STORAGE_GCS_PREFIX

- **Type**: `str`
- **Default**: `"django-ray/inputs"`

Authorized object-key prefix for GCS input payloads.

See [Durable Input Storage](input-storage.md) for backend dependencies, execution
validation, rollout ordering, retry behavior, and safe cleanup.

## Results

### MAX_RESULT_SIZE_BYTES

- **Type**: `int`
- **Default**: `1048576` (1 MB)

Maximum size of task results to store inline in `result_data`.
When exceeded, django-ray stores a compact pointer in `result_reference`
and leaves `result_data` empty according to `RESULT_STORAGE_BACKEND`.

```python
"MAX_RESULT_SIZE_BYTES": 10 * 1024 * 1024  # 10 MB
```

### RESULT_STORAGE_BACKEND

- **Type**: `str`
- **Default**: `"digest"`
- **Allowed**: `"digest"`, `"filesystem"`, `"s3"`, `"gcs"`

Backend used when result payload exceeds `MAX_RESULT_SIZE_BYTES`.

- `digest`: store a deterministic digest pointer only (no external payload persistence).
- `filesystem`: persist oversized payload to disk and store a reference pointer.
- `s3`: persist oversized payload to S3/object storage and store a `s3://...` reference pointer.
- `gcs`: persist oversized payload to Google Cloud Storage and store a `gs://...` reference pointer.

`RayTaskBackend.get_result()` can rehydrate oversized results from `filesystem`, `s3`,
and `gcs` references when the reading process has matching storage configuration and
credentials. `digest` references remain retrieval metadata only.

Install extras:

- `pip install "django-ray[s3]"` for S3 SDK dependency.
- `pip install "django-ray[gcs]"` for GCS SDK dependency.
- `pip install "django-ray[object-storage]"` for both.

```python
"RESULT_STORAGE_BACKEND": "filesystem"
```

### RESULT_STORAGE_FILESYSTEM_PATH

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `RESULT_STORAGE_BACKEND == "filesystem"`

Filesystem root used by the `filesystem` backend for oversized result payloads.
In multi-worker setups, use a shared volume if retrieval may happen on a different worker.

```python
"RESULT_STORAGE_FILESYSTEM_PATH": "/var/lib/django-ray/results"
```

### RESULT_STORAGE_S3_BUCKET

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `RESULT_STORAGE_BACKEND == "s3"`

S3 bucket name used for oversized result payload storage.

```python
"RESULT_STORAGE_S3_BUCKET": "my-django-ray-results"
```

### RESULT_STORAGE_S3_PREFIX

- **Type**: `str`
- **Default**: `"django-ray/results"`

Object key prefix used by S3 backend.

```python
"RESULT_STORAGE_S3_PREFIX": "prod/django-ray/results"
```

### RESULT_STORAGE_S3_REGION

- **Type**: `str | None`
- **Default**: `None`

Optional S3 region passed when creating the S3 client.

```python
"RESULT_STORAGE_S3_REGION": "us-east-1"
```

### RESULT_STORAGE_S3_ENDPOINT_URL

- **Type**: `str | None`
- **Default**: `None`

Optional endpoint URL for S3-compatible providers (for example MinIO).

```python
"RESULT_STORAGE_S3_ENDPOINT_URL": "https://minio.internal:9000"
```

### RESULT_STORAGE_GCS_BUCKET

- **Type**: `str | None`
- **Default**: `None`
- **Required when**: `RESULT_STORAGE_BACKEND == "gcs"`

Google Cloud Storage bucket used for oversized result payload storage.

```python
"RESULT_STORAGE_GCS_BUCKET": "my-django-ray-results"
```

### RESULT_STORAGE_GCS_PREFIX

- **Type**: `str`
- **Default**: `"django-ray/results"`

Object key prefix used by GCS backend.

```python
"RESULT_STORAGE_GCS_PREFIX": "prod/django-ray/results"
```

## Operational Redaction

### REDACT_PATTERNS

- **Type**: `str | sequence[str] | None`
- **Default**: `None` (built-in patterns are enabled)

Regular expressions used to redact sensitive mapping keys and matching string
values in structured logs, Ray observability responses, the sample operational
API, and Django admin task details. A configured string or sequence extends the
built-in patterns for common names such as `password`, `secret`, `token`,
`authorization`, and `private_key`.

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "REDACT_PATTERNS": [r"customer[_-]?email", r"access[_-]?token"],
}
```

Successful task logs expose only result type and serialized size. The completion
envelope is persisted through the database channel and is not printed to Ray
stdout. Redaction does not encrypt task data or protect direct application
`print()` calls; secure the database, result backend, admin, API, and Ray
dashboard separately.

## Django Settings

These settings are configured directly in Django settings, not in `DJANGO_RAY`:

### RAY_DASHBOARD_URL

- **Type**: `str`
- **Default**: `"http://localhost:8265"`

URL of the Ray Dashboard. Used by Django Admin to generate deep links to tasks in the Ray Dashboard.
In the sample Kubernetes manifests this is set explicitly via environment/config:

- base NodePort manifests: `http://localhost:30265`
- Kong local overlay: `http://ray.localhost:30080`

```python
# settings.py
RAY_DASHBOARD_URL = "http://ray-dashboard.example.com:8265"
```

## Example Configurations

### Minimal (Development)

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
}
```

### Standard (Production)

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "DEFAULT_CONCURRENCY": 50,
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 60,
    "QUEUE_TIMEOUT_SECONDS": 86400,
    "STUCK_TASK_TIMEOUT_SECONDS": 300,
    "WORKER_LEASE_SECONDS": 60,
    "WORKER_HEARTBEAT_SECONDS": 15,
    "TASK_MONITOR_HEARTBEAT_SECONDS": 15,
    "WORKFLOW_PROGRESS_REPORTING_POLICY": "full",
    "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT": False,
    "WORKFLOW_PROGRESS_FLUSH_SECONDS": 1,
    "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": 15,
    "WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS": 7,
}
```

### Encrypted RuntimeEnv Snapshots

```python
import os


DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "RUNTIME_ENV_STORAGE_MODE": "encrypted",
    "RUNTIME_ENV_ENCRYPTION_KEYS": {
        "runtime-env-2026-01": os.environ["DJANGO_RAY_RUNTIME_ENV_KEY_2026_01"],
    },
    "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "runtime-env-2026-01",
}
```

Deploy the same complete key ring to web processes, task producers, retry/admin
services, and every task manager. Do not inject these database keys into generic Ray
head or worker pods.

### High Throughput

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "ray://ray-head-svc:10001",
    "DEFAULT_CONCURRENCY": 200,
    "MAX_TASK_ATTEMPTS": 5,
    "RETRY_BACKOFF_SECONDS": 30,
    "STUCK_TASK_TIMEOUT_SECONDS": 600,
}
```

### Fail Fast (Testing)

```python
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",
    "DEFAULT_CONCURRENCY": 1,
    "MAX_TASK_ATTEMPTS": 1,
    "STUCK_TASK_TIMEOUT_SECONDS": 30,
}
```

## Django Tasks Configuration

Configure Django's native Tasks framework to use django-ray:

```python
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": [
            "default",
            "high-priority",
            "low-priority",
        ],
    },
}
```

## Environment Variables

`django-ray` itself reads the `DJANGO_RAY` Django setting. The sample project and Docker entrypoint
map these environment variables into settings or worker CLI flags:

| Variable | Used by | Equivalent |
|----------|---------|------------|
| `RAY_ADDRESS` | sample settings, Docker entrypoint | `DJANGO_RAY["RAY_ADDRESS"]` / cluster address |
| `RAY_DASHBOARD_URL` | sample settings | Django `RAY_DASHBOARD_URL` |
| `DJANGO_RAY_RUNTIME_ENV_STORAGE_MODE` | sample settings | `DJANGO_RAY["RUNTIME_ENV_STORAGE_MODE"]` |
| `DJANGO_RAY_RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY` | sample settings | `DJANGO_RAY["RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY"]` |
| `DJANGO_RAY_RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK` | sample settings | `DJANGO_RAY["RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK"]` |
| `DJANGO_RAY_QUEUE` | Docker entrypoint | CLI `--queue` |
| `DJANGO_RAY_QUEUES` | Docker entrypoint | CLI `--queue` with comma-separated queues |
| `DJANGO_RAY_CONCURRENCY` | Docker entrypoint | CLI `--concurrency` |
| `DJANGO_RAY_SKIP_VALIDATION` | django-ray app config | Startup validation bypass |

## See Also

- [Configuration Guide](../configuration.md) - Usage guide
- [CLI Reference](cli.md) - Command-line options
- [Result Storage](result-storage.md) - Oversized result backend behavior

