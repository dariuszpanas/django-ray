# Application qualification workloads

`api.py` contains the source-owned HTTP assertion layer used by
`scripts/local_kuberay_gate.py`. A bounded assertion Job can import this module using only the
Python standard library. It does not import the host gate, Django, Ray, or Kubernetes tooling.

`verify_application_api` receives an HTTP transport, a credential supplier and the task deadline.
It retains the existing gate checks: unauthenticated rejection, safe OpenAPI routes, authenticated
statistics and protocol metrics, canonical enqueue identity, bounded task polling and provenance,
durable result `5`, and rejected execution deletion with unchanged detail. The host gate calls the
same function, so these assertions have one implementation.

The caller owns URL scope, redirects, credential handling, per-request timeouts, response byte
limits and required response headers. The function specifies the OpenAPI and task-status limits
and required polling headers through its transport protocol; omitted limits use the transport's
bounded default. Supply a positive finite task timeout. The credential supplier is invoked only
after unauthenticated protection and schema checks pass.

The returned `ApiEvidence` records observations from this layer. A caller may instead supply a
compatible mutable evidence object, as the host gate does, to retain partial progress and the
enqueued task identity after a later assertion fails. Success requires the function to return;
partial flags do not establish a passing layer. Credentials and raw response bodies are not stored
in that evidence object.

The API function proves one layer. The namespace workload below adds generic-node, manager and
encrypted RuntimeEnv assertions. Workflow recovery and the remaining deployment stages stay
tracked in django-ray issue #455. Source/image identity and bounded run evidence must be established
by the workload and its executor before a test result can serve as application proof.

## Running the API layer

From a source checkout or an assertion image containing this directory:

```sh
python -m qualification.application.run_api \
  --base-url http://django-web:8000 \
  --token-file /run/application-credentials/DJANGO_API_TOKEN \
  --task-timeout 180
```

The caller provides the admitted application service origin and a mounted application token file.
The token must contain 32Ã¢â‚¬â€œ512 ASCII token68 characters, without a trailing newline. It is read only
after the unauthenticated and schema assertions pass. HTTP(S) origins cannot include credentials,
paths, queries or fragments. The transport ignores ambient proxies, does not follow redirects,
verifies HTTPS with the standard system trust store, and enforces response byte limits for success
and error responses. Each blocking socket operation has a ten-second timeout. Task polling accepts
a positive finite timeout of at most 600 seconds; the enclosing Job must also set a hard execution
deadline because socket timeouts do not bound an entire slow-streaming response.

Exit zero means the shared API function returned successfully. A failed assertion exits one. Both
outcomes emit a single JSON receipt containing the layer name, status, elapsed time, request count,
last HTTP status and typed partial observations. Failure messages and raw bodies are omitted because
they may contain application data. Partial observations remain diagnostic even when the layer
fails. The receipt always reports `complete_application_gate: false`; it does not establish image
identity, Ray generation, manager ownership, cleanup or any other gate layer.

The command starts no services and has no Kubernetes client. `core.yaml` wires the API layer
into a bounded application stage; its admission, image binding and evidence requirements follow below.

## Generic Ray nodes and cold generations

`generic_nodes.py` supplies a separate assertion Job layer. It connects only to an explicit Ray
Client address and sends a by-value function to every live node using hard node affinity, zero
logical CPUs, no task retries and an empty RuntimeEnv. The function imports only Python's standard
library and Ray. A generic node must have no preinstalled `django_ray`, must see the exact bounded
source and recovery archives, and must contain the application image's `remote.py` in both archive
layouts. Required task modules, Ray version, Python implementation and the full Python major/minor/patch
tuple must match. The admitted hosted build first discovers CPython from the pinned stock Ray
image in a fixed, bounded, network-disabled process, then passes that exact patch to the application
build and verifies its interpreter. This matches the current cohort contract; it does not claim
that a version string proves source bytes or native-extension ABI compatibility. The remote task
assertions and exact locked archive checks supply their separate evidence.

For example, with archives mounted read-only at the same paths in the assertion image and generic
Ray nodes:

```sh
python -m qualification.application.generic_nodes \
  --address ray://ray-head:10001 \
  --source-archive /runtime/project.zip \
  --recovery-archive /runtime/recovery.zip \
  --remote-source /app/src/django_ray/runtime/remote.py \
  --receipt /receipts/before.json
```

After the executor has deleted the first RayCluster, confirmed removal and created its replacement,
run a second Job with the same source, image, archives and arguments, adding
`--previous-receipt /receipts/before.json` and selecting `--receipt /receipts/after.json`. Every new
node ID must differ from every previous ID. Membership must remain unchanged during each probe,
and the source/archive/runtime observations must match across generations. The second receipt
includes the SHA-256 of the exact first receipt bytes. Existing output files are never overwritten.
Use a fresh, namespace-owned receipt volume for each run and require both Jobs to succeed.

The default task-result timeout is 120 seconds, configurable up to 300; the default node count is
two, configurable from one to three. Each archive is capped at 32 MiB, the remote source member at
1 MiB and each receipt at 16 KiB. Enclosing Jobs must also enforce a hard deadline: Ray Client
connection, file I/O and shutdown do not share the result timeout. The probe requests cancellation
of its submitted tasks and disconnects its own client on failure. It refuses to adopt an existing Ray connection.
No local Ray cluster is started.

Exit zero requires all assertions and exclusive receipt-file creation to succeed. A failed command
prints a fixed failed receipt with no partial node observations or raw dependency exception. Every
receipt reports `complete_application_gate: false`. The executor still owns immutable source/image
binding, Pod/image and RayCluster lifecycle evidence, resource admission, bounded receipt collection
and cleanup. The module has no Kubernetes authority and does not replace the host gate.
Its existence or resource-free unit tests do not establish a passing live generic-node layer.

## Offline application core stage

`core.yaml` is a native public Chainsaw Test. Its sixteen steps start disposable PostgreSQL 17,
prepare the sample web application and locked recovery archives, and start one current core task
manager with stock Ray 2.58.0 head/worker nodes. Two serial Jobs require authenticated API execution,
exact protocol-3 task/attempt/current-manager ownership and resolved claim history,
authenticated encrypted RuntimeEnv snapshots
and the remote decrypted canary. The manager is a finite Job with no restart or retry. Before cold
replacement, `retire_manager.py` requests retirement of the exact successful manager incarnation
while its original Ray session remains available. It requires the worker's independently owned
cleanup confirmation, no owned live tasks, unresolved claims, OPEN Jobs cleanup or capabilities,
and an inactive exact lease. SQL zero alone is insufficient. Chainsaw then requires successful
manager Job exit and foreground deletion before deleting RayCluster and its dependents.

The identical bounded manager Job is recreated against the new RayCluster. The second smoke must
use a distinct later manager incarnation and verified cluster session; original terminal task,
attempt, intent, binding and claim bytes must remain unchanged. The public collector independently
checks the old manager Pod is absent and the current lease hostname belongs to the newly created
Job's exact Pod and candidate image. Only the web and database remain through this transition.
Both generic-node receipts require identical archives/runtime and disjoint node identities. This
proves orderly manager replacement, not reconnection of the original pinned Core process or the
broader coordinated release upgrade. All receipts retain `complete_application_gate: false`.
The existing 600-second assertion Job and 1800-second outer deadlines, resource ceilings and
namespace cleanup remain; each manager Job also has an 1800-second deadline.

`settings_qualification` retains production validation and replaces the project RuntimeEnv with the
locked recovery ZIP; `thin` inherits it. No pip download or `PYTHONPATH=src` is used in that profile.
The task environment pins the validated Ray Client target so generic workers retain the same
application configuration when their Pod supplies a node-local Ray address. Database and encryption
settings remain inherited from the disposable Pod's configuration and Secret.
Ordinary sample settings remain unchanged. The stage does not certify their dependency-download
behavior, workflow recovery, negative encryption cases, or a complete release upgrade.

### Public prerequisites and invocation

Use an explicitly admitted Linux Kubernetes environment with public KubeRay 1.6.2 already installed,
a compatible `kubectl`, [Chainsaw 0.2.15](https://github.com/kyverno/chainsaw/releases/tag/v0.2.15),
and Python 3.12+. No private test service, schema, image registry API or executor is required.
Never create or resize a shared cluster merely to run this test. The path-selected
[Application Qualification workflow](../../.github/workflows/application-qualification.yml) waits
for the current source Linux `CI Gate`, then hosts this stage on a disposable GitHub Actions Linux
Kind cluster with a 3-CPU/12-GiB node limit. Its local image registry has a separate 0.25-CPU/128-MiB
limit. Builds precede cluster creation on the hosted 4-CPU/16-GiB VM; normal docs-only PRs do not
trigger this supplemental workload. It also supports manual `workflow_dispatch`. The three PVCs require same-node ReadWriteOnce storage;
pass Kind's `standard` StorageClass or Docker Desktop's `hostpath` explicitly.

Build the root Dockerfile and `qualification/application/Dockerfile` from the same clean committed
Git archive, with Python 3.12/Linux amd64 matching the pinned stock Ray image. The derived image
inherits the application entrypoint and adds these assertion modules. For example, after admitting
build capacity and selecting a registry you can write to:

```sh
candidate=$(git rev-parse HEAD)
build=$(mktemp -d)
git archive "$candidate" | tar -x -C "$build"
docker build --platform linux/amd64 --provenance=false -t "$REGISTRY/django-ray-base:$candidate" "$build"
docker push "$REGISTRY/django-ray-base:$candidate"
base=$(docker image inspect "$REGISTRY/django-ray-base:$candidate" --format '{{index .RepoDigests 0}}')
docker build --platform linux/amd64 --provenance=false -f "$build/qualification/application/Dockerfile" \
  --build-arg DJANGO_RAY_RUNTIME_IMAGE="$base" \
  -t "$REGISTRY/django-ray-core:$candidate" "$build"
docker push "$REGISTRY/django-ray-core:$candidate"
image=$(docker image inspect "$REGISTRY/django-ray-core:$candidate" --format '{{index .RepoDigests 0}}')
python -m qualification.application.run_chainsaw --context "$KUBE_CONTEXT" \
  --image "$image" --storage-class standard --output "$EVIDENCE_DIRECTORY"
```

The output directory must be new and outside the clean checkout. The caller retains the Git archive,
build identity and source/package verification with the receipts; an image label alone is not source
proof. The wrapper never builds, pulls or pushes images, installs an operator or changes cluster-wide
configuration. Registry access and image availability on the admitted nodes are caller prerequisites.
Build archives may be removed after retaining their identity; no unrelated Docker images are pruned.

The runner requires an explicit context and the immutable digest of a concrete Linux-amd64 image
manifest. The single-platform builds disable provenance so an attestation-bearing image index does
not obscure comparison with the manifest digest reported by the container runtime. It creates one fresh
`django-ray-core-*` namespace and six random application Secret values via stdin, without storing
them in evidence or exposing them in command arguments. The two ordered Django secret parts compose
the production secret; the separate unpadded base64url key selects authenticated encryption under
key ID `qualification`, with Django-secret fallback disabled.

`chainsaw.yaml` pins serial execution, foreground deletion and operation timeouts. The runner uses
[public external values](https://kyverno.github.io/chainsaw/0.2.3/configuration/options/values/)
for the image and StorageClass. Automatic Chainsaw teardown is delayed with `skipDelete` so receipts
can be collected; the explicit mid-test RayCluster delete still runs. The wrapper then checks the
namespace UID, deletes only its owned namespace and requires observed absence. It returns nonzero
if the test, receipt validation or cleanup fails. An interrupted host process may require manual
cleanup of the exact recorded namespace; no cluster deletion or shared resource cleanup is attempted.

### Resource and evidence boundaries

Source inventory, conservatively counting both serial Jobs and init peaks, is **2.6 CPU, 9728 MiB
memory, 3200 MiB ephemeral storage, seven Pods and 1408 MiB PVC requests**. Every source container has
non-root identity, no service-account token, a read-only root filesystem and explicit limits.
KubeRay, Kubernetes, image builds and the host-side Chainsaw process are additional reservations.
Storage requests do not prove physical disk enforcement. The caller admits total capacity and
network reachability/isolation; this test does not install or qualify a network-policy provider.

The test has a 1800-second outer deadline, each assertion Job a 600-second deadline, and owned
namespace deletion a 180-second allowance. These are ceilings, not expected durations.

The runner retrieves at most 64 KiB from each exact setup/assertion container's log. It requires
zero successful-container restarts, exit zero, the requested immutable image with its manifest
digest in the observed container image ID, and every expected
passing JSON receipt at no more than 16 KiB. It retains `setup.json`, `before-nodes.json`,
`before-core.json`, `after-nodes.json`, `after-core.json`, producer Pod/image identities, Chainsaw's
XML report and `summary.json`. Chainsaw streams progress to the foreground; hosted runs retain it
in the public Actions job log. Receipts are transported from the modules' existing stdout; they are
not inferred from Job status. The cold receipt's predecessor digest must match the retained first
node receipt. All receipts report `complete_application_gate: false`. Failure output is diagnostic;
a missing receipt, timeout, failed cleanup or partial report cannot establish a passing stage.
Before failure cleanup, the runner also retains at most 100 RayCluster, Pod and Event records
per resource, capped at 64 KiB per file. These include status and event messages, never resource
specs or Secrets. It captures current and previous logs from at most six Ray, web or manager Pods,
at most 32 KiB per log, before teardown. Failed core receipts retain request count, last HTTP status
and validated API progress without response bodies or exception text. Hosted failures retain the
last 200 operator log lines, capped at 64 KiB.

The [affected-scenario policy](../../docs/deployment/local-kuberay-gate.md) determines which product
assertions a change needs. This stage needs actual exact-source Linux execution after the full Linux
CI checkpoint. Its source tests and a schema-valid native Test do not establish that live result.

The stock Ray head reserves 5 GiB and the worker 1.5 GiB. A 3-GiB head exhausted its memory
during full dashboard startup, which loads nine subprocess modules in the pinned Ray version.
The dashboard stays enabled because its State and Job APIs are part of the qualification.
The runner checks Pod status every 15 seconds and aborts on nonzero container termination,
retaining diagnostics before cleanup. This avoids spending the readiness timeout retrying
an OOM-killed or failed container.

For fixture debugging, push a branch without an open PR and manually dispatch the same workflow:

```sh
gh workflow run application-qualification.yml --ref "$BRANCH" -f mode=diagnostic
```

This runs only the bounded application job, without waiting for or launching the full CI matrix.
The job summary and artifact explicitly identify diagnostic intent. Diagnostic execution does not
satisfy final acceptance: publish the completed candidate to its PR, where the default acceptance
mode requires current-source Linux CI before repeating the application test.
