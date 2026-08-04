# Local KubeRay final gate

The local KubeRay final gate complements, but never replaces, `uv run make ci` and the required
GitHub Actions matrix. It exercises the Docker Desktop or Kind deployment boundary that unit and
disposable CI clusters cannot reproduce: locally built images, Kustomize, the setup Job, the shared
RuntimeEnv archive, generic Ray nodes, application task managers, protected HTTP APIs, kubelet
probes, encrypted durable RuntimeEnv storage, full and terminal-only schema-v3 workflow progress,
multi-attempt recovery, authenticated admin presentation, and live Prometheus discovery.

This is a maintainer integration-validation gate for the checked-in local profile. A passing run
is not deployment certification, a threat-model review, or evidence that the sample manifests form
a production-ready topology.

Run it only against the dedicated local `django-ray` namespace. The command fails before its first
Docker build or Kubernetes mutation unless the checkout is clean, the active context is the named
context, the context is `docker-desktop` or `kind-<name>`, its API server is local, and the rendered
resource set cannot escape `django-ray`. It also rejects a remote Docker context or `DOCKER_HOST`.
Docker receives an archive of the committed Git tree, never the checkout directory, and both
Dockerfiles have deny-by-default context allowlists. The same one-time archive is the only source for
the rendered Kubernetes manifests. Preflight also creates a private, flattened kubeconfig snapshot;
every later Kubernetes command uses that immutable file, and every Docker daemon command uses the
validated explicit endpoint rather than ambient context state. Kubernetes, Docker, and Kind
subprocesses receive sanitized environments: proxy, kubeconfig, Docker context/TLS, BuildKit/Buildx,
and ambient Kind provider routing overrides are removed; Kind is then explicitly pinned to Docker.
Named-pipe Docker endpoints must use the local `//./pipe/` authority.

## Trigger matrix

Use the strongest row that applies to a change. When uncertain about a cross-component boundary,
run the gate and choose the cold Ray restart.

| Change class | Gate | `K8S_RAY_RESTART` | Why |
|---|---|---|---|
| Isolated Compiled Graph pilot files under `k8s/pilots/compiled-graph/`, `scripts/kuberay_compiled_graph_pilot.py`, its focused tests, or its retained investigation evidence for #102 | Not applicable | N/A | The pinned pilot has its own isolated namespace, manifests, resource profile, teardown checks, and evidence contract. It must not mutate or validate the supported local application stack. |
| Testproject dashboard, templates, JavaScript, static collection, web image, entrypoint, or web dependencies | Required | `skip`, unless the RuntimeEnv or Ray boundary also changed | Proves the exact asset and image reached the live web pod and protected actions still work. |
| `Dockerfile.ray`, package or RuntimeEnv contents, source archive construction, dependency delivery, or remote bootstrap/import behavior | Required | `required` | Proves a newly built archive reaches newly created generic Ray interpreters without preinstalling `django_ray`. |
| RuntimeEnv snapshot storage, encryption settings or dependencies, storage/retry validation, the fixed deployment canary, or KubeRay encryption selectors | Required | `required` | Proves a cold generic Ray generation receives the decrypted marker while the database retains only the authenticated envelope, and proves corrupt or unknown-key rows fail before Ray. |
| Ray Client submission, reconnect, cancellation, retry, task context, result persistence, or cross-component task lifecycle | Required | `required` | Exercises a fresh Ray session plus fresh task managers and a durable result. |
| Workflow execution, progress capture/publication, schema-v3 bounded readers, failure diagnostics, retry identity, or the admin graph fed by workflow progress | Required | `required` | Proves a cold Ray generation can execute the same tiny nested workflow with default-full and explicit terminal-only reporting in both success and deterministic first-attempt failure modes, then preserve one recovery showcase across early-failed, mid-failed, and successful attempts. Full reporting must expose complete bounded detail; terminal-only must expose exactly one summary and no detail storage or actions. New and archived protected fields must retain the original external diagnostic evidence, every ordinary Admin/API projection must be terminal-inert and pattern-redacted, and Admin must present escaped tracebacks with preserved line separation and safe wrapping. |
| KubeRay `RayCluster`, Ray services, Ray pod volumes/environment, or Ray metrics configuration | Required | `required` | The tested Ray pods must be cold replacements of the prior pods. |
| Kubernetes application resources, setup Job, web/worker Deployments, probes, Secrets/ConfigMaps, RBAC, services, ingress, Prometheus, or Grafana | Required | `required` when Ray resources or the RuntimeEnv mount changed; otherwise `skip` | Proves rendered resources, rollouts, probes, authentication, and scrape ownership together. |
| Worker command, queue selection, application image, polling, or database-backed execution behavior | Required | `required` for cluster-mode submission changes; otherwise `skip` | Proves all task-manager Deployments reconnect and consume a real task. |
| Dependency update plausibly affecting Django, Ray, Gunicorn, Psycopg, Ninja, container packaging, or the sample deployment | Recommended | Match the affected boundary; use `required` for Ray or RuntimeEnv uncertainty | Catches integration drift that focused dependency tests may not expose. |
| Package behavior with no container, Kubernetes, RuntimeEnv, HTTP, worker, or task-lifecycle effect | Recommended before a release; otherwise not applicable | `skip` | The normal CI matrix is the primary gate. |
| Documentation, issue templates, repository policy, comments, or tests that do not change deployed behavior | Not applicable | N/A | Do not turn deployment-independent work into a cluster requirement. |

The gate decision and a concise semantic validation summary belong in every material retained commit
and PR to which a **Required** row applies. Record the exact gate command and result, the explicit
cold-Ray decision, the verified source-tree match, and the relevant workload, API/task-smoke, and
preservation outcomes. For a **Recommended** row, record either the same passing summary or a
specific reason it was not run. The detailed evidence still binds the run to the immutable Git tree
rather than relying on a commit SHA that changes when only the commit message is amended. Never copy
a token, Secret payload, unbounded log, or browser credential into history.

## Guarded local capacity

The gate renders the direct `kuberay-kind` exploratory profile: one default
task-manager replica consuming `default,high-priority,low-priority`, one sync
replica, one ML replica, one Ray head, and two fixed Ray workers. The rendered
steady state is 10 running workload pods with 3.2 CPU and 4,800 MiB requested
and 9.3 CPU and 11,648 MiB limited. These totals exclude the completed setup
Job, the cluster-wide KubeRay operator, and Docker Desktop/Kubernetes overhead.

Preflight derives the application replica contracts and complete static Ray
topology from that source-bound render. The full gate then requires the live
Deployments, ReplicaSets, application pods, RayCluster, Ray pods, and
Prometheus targets to converge to those exact counts; it does not scale the
profile independently. The optional `kong-local` overlay is deliberately
outside this gate and explicitly restores two default task managers and four
Ray workers for its heavier backlog/capacity role.

## Prerequisites

- A clean checkout whose committed tree is the exact source under test. Untracked files also fail
  the preflight.
- `uv`, Docker, and `kubectl`; a named Kind context also needs `kind`.
- A running Docker Desktop Kubernetes cluster or a Kind cluster whose context is `kind-<name>`.
- The KubeRay CRD and operator already installed. The gate deliberately does not install or mutate
  cluster-wide operators.
- The local access path already exposed, either through the direct NodePorts or the documented Kong
  local routes.
- A `django-ray-secret` in `django-ray`. During preflight, the gate reads `DJANGO_API_TOKEN` through a
  sensitive-output-suppressed command path and accepts only 32-512 characters from the Bearer
  `token68` alphabet (`A-Z`, `a-z`, `0-9`, `-`, `.`, `_`, `~`, `+`, and `/`) with at most two
  trailing `=` padding characters. The 32-512-character bound includes any trailing padding.
  Quotes, backslashes, whitespace, controls, non-ASCII characters, embedded padding, and longer
  padding are rejected. The gate immediately registers the token,
  Kubernetes base64 value, and their JSON-, repr-, and URL-escaped forms with the output redactor. It
  treats percent-escape hex case as equivalent, never prints the token, and never places it in a
  subprocess argument. The rendered placeholder Secret is intentionally excluded from apply so an
  existing conforming local token is preserved.
- Static bearer/basic-auth, auth-provider, private-key, and sensitive exec argument/environment
  values embedded in the private kubeconfig snapshot are registered with the same redactor before
  the first snapshot-backed Kubernetes command. The snapshot command itself suppresses captured
  failure output because its credentials are not known until capture succeeds.

Older checkouts created cluster-scoped `ClusterRole/prometheus-django-ray` and
`ClusterRoleBinding/prometheus-django-ray`. Current manifests use a namespace-scoped Role and
RoleBinding because Prometheus discovers pods only in `django-ray`. The gate refuses to proceed while
either obsolete cluster-scoped object exists; it will not silently mutate cluster-wide RBAC. Review
the migration, then remove only those two old objects once before running this gate:

```bash
kubectl --context docker-desktop delete \
  clusterrole/prometheus-django-ray \
  clusterrolebinding/prometheus-django-ray \
  --ignore-not-found
```

This one-time RBAC migration deletes no workload or data. Do not broaden it to other roles or
bindings.

Run `uv run make ci` first. Then run the non-mutating preflight with an explicit context, namespace,
and Ray decision:

```powershell
uv run make k8s-final-gate-preflight `
  K8S_CONTEXT=docker-desktop `
  K8S_NAMESPACE=django-ray `
  K8S_RAY_RESTART=required
```

The gate renders and owns the `kuberay-kind` overlay, so its HTTP acceptance boundary is the
direct NodePort pair: Django at `http://localhost:30080` and Prometheus at
`http://localhost:30090`. Keep those defaults unless the same services are already exposed through
equivalent reviewed local routes. In particular, `http://prometheus.localhost:30080` belongs to the
optional, independently deployed `kong-local` overlay. Without that Prometheus ingress, port `30080`
routes to Django and the Prometheus `/api/v1/targets` request returns HTTP 404.

For a plain Kind cluster, the context encodes the cluster name. The optional override must match it:

```bash
uv run make k8s-final-gate-preflight \
  K8S_CONTEXT=kind-django-ray \
  K8S_NAMESPACE=django-ray \
  K8S_RAY_RESTART=required \
  K8S_FINAL_GATE_EXTRA_ARGS="--kind-cluster-name django-ray"
```

Preflight verifies Git cleanliness, captures `HEAD` once, derives the tree from that captured commit,
and exports one immutable archive used by both Kustomize and Docker. It checks a local Docker daemon,
pins its explicit endpoint, validates local Kubernetes context identity, writes a private flattened
kubeconfig snapshot without a proxy route, verifies that snapshot's SHA-256 before every later
command, strips ambient routing overrides from Kubernetes, Docker, and Kind subprocesses, checks the
KubeRay CRD, enforces an exact GVK/name inventory and namespace confinement, loads the existing token
into the in-memory redactor, and runs a client-side apply. It does not build images, load images, or
change Kubernetes. Unknown resources have no implicit apply phase: adding a new workload requires an
explicit review and gate classification.

## Run the gate

Replace the preflight target with the full target after reviewing the trigger matrix:

```powershell
uv run make k8s-final-gate `
  K8S_CONTEXT=docker-desktop `
  K8S_NAMESPACE=django-ray `
  K8S_RAY_RESTART=required
```

The gate performs these bounded layers:

1. Repeats preflight and derives a unique tag containing the first 12 characters of the immutable
   Git tree, a UTC timestamp, and a random suffix.
2. Builds `django-ray:<source-tag>` and `django-ray-worker:<source-tag>` from the same preflight
   archive used to render Kubernetes. Deny-by-default Dockerfile context rules admit
   only reviewed image inputs; the gate rejects a missing or altered Dockerfile-specific policy
   instead of falling back to the broader root context. Both images carry the commit-at-run and
   stable source-tree OCI labels, and named Kind clusters receive those exact tags through
   `kind load docker-image`.
3. Renders a temporary copy of `k8s/overlays/kuberay-kind`, rejects floating application tags or any
   resource outside `django-ray`, and preserves the existing live API Secret. It requires encrypted
   Django-secret mode directly on `django-web` and the default, synchronous, and ML task-manager
   containers, with no selector in an init container, shared ConfigMap, setup Job, or Ray pod
   template. It applies only setup prerequisites first, then rolls Prometheus and waits for
   PostgreSQL.
4. Recreates `Job/django-setup` with timeout-bounded deletion and completion waits. Setup must prove
   migrations, static collection, RuntimeEnv bundle creation, the exact Job UID ownership of its
   pod, and the rendered `django-setup` container name, tag, and image ID without a substitute
   sidecar. A failure stops the run before any application
   Deployment or `RayCluster` is reconciled.
5. Applies the staged application Deployments and `RayCluster` only after setup passes. With
   `required`, a restart-discovery phase inventories only pods with the exact live RayCluster UID,
   sole controlling `RayCluster/ray` owner, `ray.io/cluster=ray` and `app=ray` labels, recognized
   head/worker role and worker group, unique bounded pod UIDs, and the exact supported container-name
   inventory. That phase intentionally permits the prior pinned Ray image so a newly applied profile
   can safely cold-replace the old generation. It then deletes only those verified pod names with a
   timeout and proves the RayCluster UID remains pinned and all replacement pod UIDs are new. Final
   convergence requires the live head spec, named worker groups, min/replicas/max values, Ray start
   parameters, image references, and pod counts to match the rendered static topology. It also
   validates KubeRay 1.6.2's effective worker pod contract: exactly one injected `wait-gcs-ready`
   init container, the rendered pinned `ray-worker` image, `/bin/bash -c --`, the exact generated GCS
   health-check loop targeting `ray-head-svc.<namespace>.svc.cluster.local:6379`, successful init
   termination, the exact regular/status inventories, and no extra init or ephemeral container.
   Namespace-wide pod inventory also rejects owned pods hidden by missing labels. The converged pod
   UID plus complete named init/regular container image and runtime image-ID set is retained and must
   remain identical before and after Prometheus and before evidence. With `skip`, no relaxed discovery
   is used: the gate waits for and pins that same strict effective topology without deleting pods.
6. Restarts exactly the three task-manager Deployments and waits for them plus `django-web`.
   Because `kubectl rollout status` can return before an old pod finishes terminating, a second
   deadline-bounded convergence barrier polls the complete application inventory. Every rendered
   Deployment must report its exact desired, updated, ready, and available replicas and exactly one
   current-revision ReplicaSet. The gate binds Deployment UID -> current ReplicaSet UID -> Pod UID
   and waits until no pod remains under an older ReplicaSet, including pods with a
   `deletionTimestamp`. It never deletes those pods directly. Kubernetes may retain inert,
   zero-replica historical ReplicaSets for rollback history; they are accepted only when they own no
   remaining pod and are not themselves terminating. Hidden, unowned, substituted, malformed, or
   unexpected-container resources remain immediate failures rather than retryable rollout state.
7. Strictly rechecks that converged application topology without polling and verifies the full named
   init/regular container image and runtime image-ID set. This prevents a terminating or old-image
   pod from being omitted from final evidence. It also proves every generic Ray interpreter lacks an
   installed `django_ray` and sees the same RuntimeEnv archive, SHA-256, and remote-bootstrap member.
8. Verifies live readiness/liveness paths and `Host: django-ray.localhost`, a Ready web pod, and its
   restart count.
9. Requires unauthenticated enqueue/stats/metrics/executions requests to return `401`; requires the
   same protected reads to return `200` with the in-memory token; then polls the fresh task through
   the exact `/api/tasks/{task_id}` status route and execution-list filter. The status response must
   keep consistent Django and durable states, exact attempt identity, the fixed input-omission
   vocabulary, a 16,384-byte combined inline-input guard, a 65,536-byte response ceiling, and the
   no-store/nosniff headers. The execution must reach durable `SUCCEEDED` with `result_data=5`.
   OpenAPI must omit the removed bulk-reset, complete-graph, and live-node routes while retaining the
   exact retry and durable indexed node-detail replacements. The gate also rejects execution
   `DELETE`, then requires the exact detail read to preserve the row and result while advertising
   65,536-byte diagnostic guards, a 262,144-byte response ceiling, and no omission for the small
   inline value.
10. Enqueues the lightweight `thin` RuntimeEnv probe, keeps every poll within the gate's 65,536-byte
    HTTP read, and requires each response to advertise the 16,384-byte diagnostic guard, 65,536-byte
    response ceiling, fixed result/error omission vocabulary, and consistent value/omission pairs.
    Its sanitized result must report `storage_encryption_verified=true`. A
    sensitive-output-suppressed in-pod inspector then reads the
    raw database field, requires the exact canonical AES-256-GCM envelope with the guarded
    Django-secret key selection, and proves the fixed plaintext marker is absent. The marker, raw
    envelope, nonce, and ciphertext are registered with both command and gate redactors before any
    later diagnostics. One atomic in-pod transaction creates exactly two additional encrypted queued
    probe rows through the production storage seam, changing canonical ciphertext on one and the key
    ID on the other before commit. Both must fail permanently on attempt 1 before any Ray submission;
    an authenticated retry of one must return `409` without changing the row or archived attempt.
    Sanitized API bodies plus bounded current API/admin and task-manager logs must contain none of the
    protected values. The layer retains only booleans and creates three bounded disposable rows.
11. Enqueues the same tiny nested workflow with the unchanged default-full behavior once for success
    and once with the deterministic slow-branch failure fixture. Each response must retain the exact
    typed enqueue arguments, and each execution must remain on durable attempt 1. The successful
    result must report all three leaves. The failed execution must retain the normalized fixture
    error; its terminal snapshot may legitimately retain pending or running downstream nodes that Ray
    did not execute after their dependency failed. For both runs, the gate requires terminal
    schema-v3 summaries and complete one-page topology-node, topology-edge, and node-detail readers
    with matching run identity, publication revisions, graph membership, states, and counts. Each
    task poll must remain within the gate's 65,536-byte HTTP read and advertise the same 16,384-byte
    diagnostic guard, response ceiling, fixed omission vocabulary, and value/omission consistency.
12. Repeats the success and deterministic failure through the explicit
    `reporting_policy=terminal_only` testproject option. Each run must remain on attempt 1 and expose
    one revision-1 schema-v3 summary with `reporting_policy="terminal_only"` and
    `detail.availability="OMITTED_BY_POLICY"`. Declared plan counts must match each run's persisted
    materialized plan and remain consistent across the equivalent success and failure fixtures,
    while discovered, retained, and node-state counts remain zero. Topology and detail revisions
    are null, and all three bounded collection readers return empty omitted-by-policy envelopes.
13. Enqueues one fixed full-reporting order-fulfillment recovery showcase. The same durable task ID
    must archive exactly three outcomes in order: attempt 1 fails at the workflow entry, attempt 2
    replays from the entry and fails at the mid-workflow join after seven upstream nodes succeed,
    and attempt 3 replays the complete workflow and succeeds. The gate requires distinct run IDs,
    generations 1 through 3, one stable plan fingerprint, exact fixture errors, no fourth attempt,
    and a current result that identifies successful attempt 3 without a stale error. Its task poll
    must meet the common diagnostic and response contract, advertise the 4,096-byte archived-attempt
    error guard, and use only the fixed attempt-error omission vocabulary. The temporary
    setup job must build the bounded deterministic recovery archive, the Django endpoint must verify
    and report the explicit `recovery-showcase` profile, and the Ray Client task manager must upload
    those exact content-hashed bytes before the generic Ray pods execute them. Archived Admin
    diagnostics must report the plan as retry-safe. It reads each
    attempt explicitly through the schema-v3 summary, topology, and node-detail APIs. The one-item
    fixture must retain 2 nodes and 1 edge on the early failure, 15 nodes and 20 edges on the middle
    failure, and the complete 21-node, 28-edge graph on success.
14. Enters the exact converged `django-web` container through a sensitive-output-suppressed command
    path and creates a disposable authenticated admin session. For the default-full runs it verifies
    the change view, diagnostics, all three bounded readers, and the sanitized graph route. The
    successful graph must be fully succeeded. The failed graph must retain one failure origin, at
    least one incoming edge and its ancestor path, and at least one successful node outside that path
    as sibling context. Both graphs must match the bounded pages, remain within their fixed allowlist
    and byte cap, and have exactly one current manifest with no pending manifest or unlinked page.
    It then selects all three archived recovery attempts through the Admin readers. The early root
    failure must have one failed origin and no fabricated incoming edge, the middle failure must
    retain its successful ancestor path, and the final attempt must be fully succeeded. All three
    must match the API counts and their exact run-scoped storage identity.
    For both terminal-only runs it instead proves null legacy progress, an identical archived attempt
    summary, no run storage, manifest, page, link, or node-detail row, zero advertised admin actions,
    and a bounded `UNAVAILABLE` graph response. The disposable sessions and users are removed before
    each child smoke returns scalar evidence.
15. Reuses the checked-in Prometheus checker through the same proxy-disabled, redirect-rejecting
    local HTTP opener. It requires exactly one `django-ray`, one `ray-head`, and one target for every
    converged Ray worker, plus the absence of the removed `django-ray-worker` pool. The exact
    RayCluster UID/topology is rechecked before and after Prometheus and again before evidence.

Every subprocess and Kubernetes API request has a configurable upper bound. The defaults are 120
seconds for ordinary commands, 1,200 seconds for each Docker build or Kind image load, and 30 seconds
for an individual Kubernetes API request. Override them only for a reviewed slow local environment:

```powershell
uv run make k8s-final-gate `
  K8S_CONTEXT=docker-desktop `
  K8S_NAMESPACE=django-ray `
  K8S_RAY_RESTART=required `
  K8S_FINAL_GATE_EXTRA_ARGS="--command-timeout 180 --build-timeout 1800 --kubectl-request-timeout 45"
```

The KubeRay overlay intentionally runs upstream `rayproject/ray` images. The separately built
`django-ray-worker` image covers the legacy custom-Ray image build boundary and is revision-checked,
but it is not substituted into the generic KubeRay pods. Running image-ID checks apply to the
complete Django web, setup, task-manager, and generic-Ray pod container sets. The locally built image
ID is required for every source-bound container; upstream/helper containers retain their observed
runtime image IDs in the exact identity contract. Ray-node checks additionally prove the generic-image
and RuntimeEnv boundary.

## Runtime evidence and durable validation summary

On success, the gate prints a complete `=== Local KubeRay final gate evidence ===` block for immediate
diagnosis. It prepares the block during its final identity check but emits neither the final success
line nor any evidence until its private workspace and kubeconfig have been removed successfully. A
workspace creation or cleanup failure therefore cannot leave a false passing block. The block is
secret-free and bounded, but it contains ephemeral image IDs, pod hashes, cluster UIDs, checksums,
and other run-specific identifiers. Do not copy the complete block into a retained commit or PR by
default.

Each emitted line is at most 72 characters for bounded terminal output and stable diagnostic
artifacts. A long value is emitted as `key_parts=<count>` followed by ordered `key_part_001=...`
lines; concatenate the numbered parts without a separator to recover the original value. Preserve
the complete ordered block when attaching it as a diagnostic artifact. If an investigation genuinely
needs a run-specific identifier, retain the focused value or artifact in an issue or PR comment and
explain how it will be used.

The runtime block records:

- commit at run time, stable source tree, context, namespace, private kubeconfig digest, local API
  server, and pinned Docker endpoint;
- both unique tags and local image IDs;
- byte sizes and SHA-256 identities for the source and recovery RuntimeEnv archives;
- whether Ray was cold-restarted, the pinned RayCluster UID, converged head/worker counts, and the
  retained Ray pod UID/container/image identity-set SHA-256;
- ready replica counts for all application Deployments;
- unauthenticated/authenticated status summary plus the fresh task ID, `SUCCEEDED`, and result `5`;
- the authenticated ordinary Admin probe's fixed newest-first attempt-history scope,
  exact bounded attempt-detail link, and live-status response, without exposing
  standalone attempt navigation or raw RuntimeEnv data;
- scalar booleans for the encrypted RuntimeEnv overlay, decrypted-marker canary, exact durable
  envelope and marker absence, corrupt-ciphertext and unknown-key rejection before Ray, retry
  preservation, protected-value log scan, and full `django-ray-secret` preservation. Encryption
  evidence never includes task IDs, hashes, key IDs, nonces, ciphertext, or envelopes;
- the successful workflow's first-attempt state, schema-v3 availability, topology/detail counts,
  exact three-leaf enqueue/result agreement, authenticated admin-reader count, and clean current
  publication storage;
- the deterministic failure workflow's first-attempt `FAILED` state, enqueue-derived three-leaf
  count, schema-v3 availability, pending/running/succeeded/failed node counts, authenticated
  sanitized graph route, single failure origin, incoming failure edge, ancestor-path and successful
  sibling context, and clean current publication storage;
- the recovery showcase's exact `FAILED`, `FAILED`, `SUCCEEDED` attempt sequence, three distinct
  fenced run identities and consecutive generations, explicit `recovery-showcase` profile,
  bounded content-addressed recovery archive, execution on generic Ray images, stable retry-safe
  plan fingerprint, early/middle/final topology and state counts, current attempt-3 result, and all
  three archived Admin projections;
- the explicit terminal-only success and deterministic failure, each on attempt 1 with one
  revision-1 schema-v3 summary, omitted detail, declared counts matching its persisted materialized
  plan, null legacy progress and detail revisions, zero retained detail rows, and no advertised
  admin action;
- probe path/Host, web restart count, and Prometheus pool counts;
- the preservation statement. The full base64 `django-ray-secret.data` mapping is digested privately
  during preflight and compared again immediately before evidence; neither digest nor Secret value is
  emitted.

After a full pass, retain a concise semantic summary in the material commit and PR. It must include:

- the exact `uv run make k8s-final-gate` command and arguments plus its pass/fail result;
- the explicit cold-Ray choice: `required` means replacement was performed and verified, while
  `skip` means the existing pinned topology was preserved;
- confirmation that the emitted `source_tree` matched `git rev-parse HEAD^{tree}` after any
  message-only amend, without copying the tree hash into history; and
- the behavior and preservation outcomes relevant to the change, such as application readiness,
  authenticated API status, smoke-task state and result, first-attempt schema-v3 workflow success
  and deterministic failure, the three-attempt replay-to-success recovery sequence, authenticated
  sanitized Admin graphs for current and archived attempts, probes, Prometheus targets, preserved
  Ray topology, Secret, PostgreSQL data, or PVCs.

For example, portable commit validation can say:

```text
- `uv run make k8s-final-gate` with
  `K8S_CONTEXT=docker-desktop`, `K8S_NAMESPACE=django-ray`, and
  `K8S_RAY_RESTART=required`: passed; the emitted source tree matched
  HEAD, all application workloads were ready, authenticated API smoke
  returned 200, the task succeeded with result 5, the schema-v3 nested
  workflow passed in both default-full success and deterministic
  first-attempt failure modes, terminal-only success and failure each
  retained one omitted-detail summary with no legacy or normalized
  detail storage, the recovery showcase replayed from entry across two
  fenced failed attempts and completed on its third attempt with all
  three archived Admin graphs verified, encrypted RuntimeEnv storage delivered
  its marker through cold Ray while retaining only a canonical envelope,
  corrupt and unknown-key rows failed before Ray without a retry mutation,
  the full application Secret remained unchanged, and the
  authenticated admin graph retained the incoming
  full-reporting failure path, all Ray pods were cold-replaced, and
  data-bearing resources were preserved.
```

The PR should carry the same facts in natural Markdown without artificial 72-column wrapping. The
summary is intentionally useful without access to the original local cluster; raw identifiers are
not. `source_commit_at_run` remains only an invocation reference because a message-only amend changes
the commit SHA while preserving the tested tree. Any tracked file or tree change invalidates the run
and requires a new gate.

Do not paraphrase a partial run as passing. A preflight-only result is useful preparation but is not
final-gate evidence.

## Failure diagnostics and recovery

Failures are labeled by layer: `preflight`, `images`, `apply`, `setup`, `workloads`, `ray`, `rollouts`,
`app-convergence`, `image-identity`, `runtime-env`, `probes`, `api-smoke`,
`runtime-env-encryption`, `workflow-progress`, `workflow-admin`, `prometheus`, or
`final-identity`. After a Kubernetes mutation,
the command prints only bounded status plus the relevant tail of setup, Ray, application, or
Prometheus logs. Every line uses the same redacting emitter; sensitive kubeconfig and Secret command
failures suppress their captured output before the credential values could be registered, while
captured static kubeconfig and API credentials are registered before later command output is handled.
Non-command exception details are redacted before they are tail-bounded to 16,000 characters. A
truncated detail starts with `[truncated redacted error; original_characters=<count>]` so retained
diagnostics never present a silent partial value as the complete failure. Relevant Kubernetes
diagnostics run before private-workspace cleanup. Workspace creation and cleanup errors use the same
bounded, redacted failure contract; if cleanup also fails after a primary gate error, the primary
layer remains authoritative and the cleanup error is retained as separately bounded context.

Fix the named layer and rerun the same source tree with a new unique tag. Normal recovery must not
use `k8s-reset`, delete the namespace, delete PostgreSQL, delete a PVC, prune Docker, or remove other
local images. The gate itself never performs those actions. It only:

- applies namespace-confined prerequisites first and defers application/Ray workloads until setup;
- preserves the existing `Secret/django-ray-secret` rather than applying its checked-in placeholder;
- rolls the namespaced Prometheus Deployment so target checks use the applied configuration;
- deletes/recreates `Job/django-setup` with bounded waits;
- optionally deletes individually verified `RayCluster/ray` head/worker pod names with a bounded
  wait; and
- restarts the three named task-manager Deployments.

If a manifest change would require destructive data migration or cluster-wide mutation, stop and
review that change separately. It is outside this local final gate.

## Optional rendered browser check

Use a browser after the automated gate when a dashboard/template/static-assets row triggered it.
Confirm the version, token controls, statistics, Metrics, Executions, and browser console. Paste the
token manually into the page only in a trusted local session, reload, and confirm a protected action
works without another paste. Then select **Forget token**, reload again, and confirm protected actions
require a token. Do not automate token retrieval into browser logs, screenshots, recordings, or
artifacts, and do not include the token in a URL.

## Initial reference evidence

The July 21, 2026 recovery remains the reference that motivated this gate:

- Issues #144, #145, #146, and #147 captured the independently testable dashboard
  authentication, generic-Ray bootstrap, probe Host, and Prometheus ownership failures.
- PRs #148, #149, #150, and #151 independently repaired dashboard authentication, generic-Ray
  bootstrap, probe Host headers, and Prometheus target ownership.
- Rebase-merged `main` at `1cef8e6042ed0fe811cc9ee99b8332a75c887c75` was rebuilt as
  `django-ray:main-1cef8e6`.
- The setup Job rebuilt `/runtime-env/django-ray-source.zip` at 293,956 bytes; one Ray head and four
  workers were cold-replaced; every generic interpreter lacked `django_ray`.
- Fresh task `80310cfa-f453-4be4-a73e-86d0d9c92266` reached `SUCCEEDED` with result `5`, while the
  protected endpoints returned `401` without authentication.
- Both web probes used `Host: django-ray.localhost`; the web pod was Ready with zero restarts; the
  Prometheus result was `django-ray=1`, `ray-head=1`, and `ray-workers=4`, with no stale worker pool.

That historical evidence proves the recovery, not future commits. Each triggered change needs its
own clean-checkout gate result.
