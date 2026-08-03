# Ray ecosystem support

django-ray 0.4 requires `ray[default]>=2.56.0`. That base dependency supports
django-ray's Ray Core, Ray Client, Ray Jobs, Dashboard, and State API paths. It does
not turn every optional Ray library into a tested django-ray integration.

A disposable Ray 2.56.0 base-install probe confirmed that `ray`,
`ray.job_submission`, and `ray.dag` import. The base environment did not provide a
usable Data, Train, Tune, RLlib, or Serve path without component dependencies. The
former `ray.workflow` package is removed. Importing `ray.dag` also does not make the
native Compiled Graph path eligible: django-ray keeps that strategy disabled unless
its separate capability and lifecycle gates pass.

This page is the adoption contract for those boundaries. It adds no django-ray
pass-through extras, component adapters, models, migrations, or public APIs. Install
optional Ray components in the application-owned image or RuntimeEnv that executes
them, using the same Ray version as the cluster.

## Choose the smallest supported path

- Use **Ray Core** for bounded, low-latency tasks and django-ray workflows.
- Use **Ray Jobs** for finite, coarse, or long-running drivers that should survive the
  submitting task manager's connection ending.
- Use the tested **Ray Data** recipe for a bounded batch transform with immutable
  inputs, attempt-scoped outputs, and a compact completion manifest.
- Treat **Train, Tune, and RLlib** as application-owned workloads. Run a finite driver
  through Ray Jobs and own its framework, storage, checkpoint, and restore policy.
- Operate **Serve** as a separate long-lived service. Django calls its stable data-plane
  API; a finite task does not own deployment health.
- Keep **Compiled Graph** experimental. The current package contains planning and
  capability groundwork, not an enabled execution strategy.

## Install and support matrix

**Base import** describes the verified `ray[default]` environment, not a promise that a
namespace which happens to import has all dependencies required for real work.

| Component | Install and django-ray 0.4 status |
|---|---|
| **Ray Core** | **Install:** `ray[default]`; base import **yes**.<br>**0.4 status:** First-class django-ray execution path. |
| **Ray Client** | **Install:** `ray[default]`; base import **yes**.<br>**0.4 status:** Cluster Core transport with a connection-owned lifetime. |
| **Ray Jobs** | **Install:** `ray[default]`; base import **yes**.<br>**0.4 status:** First-class django-ray execution path. |
| **Dashboard and State APIs** | **Install:** `ray[default]`; base import **yes**.<br>**0.4 status:** Live diagnostics only. |
| **Ray Workflows** | **Install:** no supported install; **removed upstream**.<br>**0.4 status:** Unrelated to django-ray workflows. |
| **Ray Data** | **Install:** the bundled recipe pins `ray[data]==2.56.0`; base does not guarantee a usable path.<br>**0.4 status:** Shipped application-owned Ray Job recipe with bounded Linux evidence. |
| **Ray Train** | **Install:** matching `ray[train]` plus a framework; base does not guarantee a usable path.<br>**0.4 status:** Documented application-owned workload; untested. |
| **Ray Tune** | **Install:** matching `ray[tune]` plus trainable/search dependencies; base does not guarantee a usable path.<br>**0.4 status:** Documented application-owned workload; untested. |
| **RLlib** | **Install:** matching `ray[rllib]` plus framework/environment dependencies; base does not guarantee a usable path.<br>**0.4 status:** Documented application-owned workload; untested. |
| **Ray Serve** | **Install:** matching `ray[serve]` in the serving environment; base does not guarantee a usable path.<br>**0.4 status:** Separate application/platform-owned service. |
| **Ray Serve LLM** | **Install:** matching `ray[serve,llm]` plus engine/model dependencies; base does not guarantee a usable path.<br>**0.4 status:** Deferred, evidence-gated service. |
| **Compiled Graph** | **Install:** base `ray.dag` imports; native canaries use matching `ray[cgraph]`.<br>**0.4 status:** Experimental groundwork; no enabled strategy. |

Ray supports combining extras, for example `ray[default,data]`. Lock the complete
workload dependency set and keep the Ray version aligned across the task manager, Ray
Job driver, and cluster. Prefer an immutable image for production. A RuntimeEnv can add
dependencies to a specific job, task, or actor, but it does not retroactively add them
to a driver that already imported application code.

## Ray Client or Ray Jobs?

Ray Client is useful for interactive or bounded work while the submitting process owns
a stable connection. django-ray's cluster Ray Core mode therefore belongs to the task
manager process and its connection lifetime. Ray documents limitations for Train and
Tune over Ray Client and recommends Ray Jobs for long-running workloads.

Ray Jobs accepts a finite driver and lets it continue independently of the original
submission connection. A job still belongs to one Ray cluster and runs once; cluster
loss is not a checkpoint. django-ray owns durable submission identity, status
reconciliation, stop intent, and outer retry. The application still owns component
recovery and idempotent output publication.

Use a dedicated Ray Job queue for Data, Train, Tune, or RLlib. Keep the driver finite,
pin its RuntimeEnv or image, and return only the bounded durable result described below.

## Cross-ecosystem durable contract

Use this contract for finite Ray Data, Train, Tune, and RLlib workloads, and for custom
Ray Core code that creates component objects internally.

1. **Keep one finite outer durability boundary.** Enqueue one Django task and execute
   one finite component driver inside its `RayTaskExecution`. Split work into separate
   Django tasks only when stages need independent durable retries, cancellation,
   results, or operator control.
2. **Persist values, never live Ray objects.** Do not place a `Dataset`, `ObjectRef`,
   Train `Result`, Tune `ResultGrid`, RLlib `Algorithm`, Serve handle,
   `CompiledDAGRef`, actor, client, channel, or object-store reference in task
   arguments, results, models, or workflow-plan fields. Pickling a handle does not make
   it durable.
3. **Exchange bounded JSON and immutable URIs.** Inputs name immutable data, model,
   configuration, experiment, or checkpoint objects. Results contain a small versioned
   summary and immutable artifact URIs, with enough identity to validate schema,
   inputs, code/config revision, and producer without listing an unbounded inventory.
4. **Keep the ORM out of the distributed data plane.** Django models coordinate task
   ownership and business state. Ray's object store and external storage carry bulk
   data. Distributed workers should not contend on Django rows for data or progress
   transport.
5. **Publish at-least-once outputs explicitly.** Use a stable idempotency key and a
   versioned output prefix. Write data/checkpoints first, then publish a bounded
   manifest through a storage-specific create-only or conditional-commit primitive.
   Artifact completion is not durable task success. Reuse the same identity only when
   the authoritative current task is `SUCCEEDED` and the manifest/output still
   validate. A later attempt or generation uses a new fenced namespace unless a
   component-specific protocol proves another safe rule.
6. **Compose retries and checkpoints deliberately.** django-ray owns outer attempts;
   the component may also retry workers or restore checkpoints. Bound the combined
   attempts and elapsed time. Restore only when input, code, dependency, and output
   identities match; otherwise create a new versioned run.
7. **Isolate capacity and dependencies.** Use dedicated queues, explicit task-manager
   concurrency, and Ray resource requests for data, CPU training, GPU training, tuning,
   or reinforcement learning. Do not let a general task manager accidentally admit an
   unbounded experiment.
8. **Treat cancellation as intent, not rollback.** Stop component work and release
   actors in `finally` paths, but assume an interrupted attempt may already have
   written artifacts. Only an idempotent output protocol and validated completion
   manifest make a retry safe.

### Completion manifest

Keep the durable result small even when a workload writes many objects. Store a full
inventory in the component's artifact format and publish a bounded summary such as:

```json
{
  "schema": "myapp.ray-workload-result/v1",
  "operation_key": "orders-2026-08-03-model-v42",
  "attempt_namespace": "task-uuid/generation-3/attempt-3",
  "input_digest": "sha256:...",
  "code_revision": "release-2026-08-03",
  "status": "artifact_complete",
  "artifact_uri": "s3://example/runs/orders-2026-08-03-model-v42/g3-a3/manifest.json",
  "checkpoint_uri": "s3://example/runs/orders-2026-08-03-model-v42/g3-a3/checkpoint/",
  "metrics": {"records": 120000, "failures": 0}
}
```

The operation key identifies the requested business work; the attempt namespace fences
one execution generation and attempt. The artifact URI must be immutable for both. The
manifest says the artifact is complete; it does not claim the Django task committed
`SUCCEEDED`. Bound metric keys, strings, and encoded JSON size before returning it as a
task result. Keep credentials out of URIs and manifests.

## Component boundaries

### Ray Core

**Durable exchange.** Use django-ray's versioned JSON/input-storage and result-storage
contracts. `ObjectRef` values, actors, and other native handles remain inside one
attempt.

**Owner and recovery.** django-ray owns the finite attempt, timeout, cancellation
intent, and outer retry. Ray owns tasks and actors created during that attempt. Direct
cluster work remains tied to the task manager's Ray Client connection.

**Evidence and limit.** Local and remote Core paths and dynamic workflows are tested.
django-ray does not promise durable actor recovery or persistence of native handles.

### Ray Jobs

**Durable exchange.** The task stores bounded inputs/results plus the immutable Ray Job
submission identity required for reconciliation. Never persist a `JobSubmissionClient`
or in-memory driver handle.

**Owner and recovery.** django-ray owns submission, polling, stop intent, and retry;
Ray owns the accepted finite driver. A stop request is asynchronous, and a completed
driver may have published effects before its final status is reconciled.

**Evidence and limit.** Ray Job submission, timeout, cancellation, reconciliation, and
RuntimeEnv paths are tested. Jobs are cluster-bound and are not checkpoints.

### Dashboard and State APIs

Use Dashboard, State CLI/SDK, logs, and metrics to investigate live Ray state. Correlate
those observations with django-ray task and attempt IDs, but never replace a durable
task state, result, or audit record with a live snapshot. State output may be stale,
partial, truncated, or already garbage-collected. django-ray does not proxy Dashboard
or derive durable control flow from State API responses.

### Ray Workflows

Do not confuse django-ray's `Workflow` abstraction with the former `ray.workflow`
package. Ray deprecated that experimental library in 2.44 and removed it after 2.47;
the [Ray 2.56.0 source](https://github.com/ray-project/ray/blob/ray-2.56.0/python/ray/workflow/__init__.py)
raises on import.

There is no compatible workflow ID, storage directory, checkpoint, DAG, or resume
interchange. Re-express the business operation as a django-ray task/workflow and prove
its input, output, replay, and external-effect contract as a new adoption project.

### Ray Data

**Durable exchange.** Pass immutable dataset and transform identities; return an
immutable output URI and bounded manifest, never a `Dataset`.

**Owner and recovery.** One finite Data pipeline runs inside one dedicated Ray Job.
The application owns reads, writes, attempt namespaces, and cleanup. The shipped recipe
recomputes into a new attempt namespace rather than selectively resuming partitions.

**Evidence and limit.** The [Ray Data batch-job guide](ray-data.md) and
`testproject/apps/cluster_tasks/ray_data_job.py` provide a tested application-owned
recipe. Hermetic contracts, blocking real-Ray Linux probes on Python 3.12 and 3.14,
and a bounded Debian Python 3.12 rehearsal pass. The Windows rehearsal is recorded as
failing native evidence. This is not generic Dataset/connector support, and multi-node
shared-storage behavior remains unproven.

### Ray Train

**Durable exchange.** Pass immutable data/configuration and optional checkpoint URIs;
return checkpoint/artifact URIs and bounded final metrics, never a Train `Result`.

**Owner and recovery.** Run one finite Trainer from a Ray Job with exact Train mode,
framework, accelerator, and storage dependencies. Train owns its worker group and
component recovery. The application decides whether an outer retry restores a
compatible checkpoint or starts a new run.

**Evidence and limit.** django-ray has no tested trainer/framework tuple. Ray's current
fault-tolerance guidance distinguishes worker, node, and driver recovery; do not make a
generic recovery promise without an end-to-end recipe.

### Ray Tune

**Durable exchange.** Pass a bounded experiment specification plus immutable data and
checkpoint URIs; return the experiment URI and bounded selected-result summary, never
a `ResultGrid`.

**Owner and recovery.** Tune owns trials, schedulers, searchers, trial retries, and
experiment state. Persistent storage is required for `Tuner.restore()`. Decide whether
the outer retry restores compatible unfinished work or starts a new experiment
identity; a completed experiment is not an editable continuation.

**Evidence and limit.** django-ray does not test a search algorithm, scheduler,
trainable, or storage backend. Add one bounded recipe before claiming support for a
specific combination.

### RLlib

**Durable exchange.** Pass a bounded algorithm/environment specification and immutable
data/checkpoint URIs; return a checkpoint/model URI and bounded metrics, never an
`Algorithm`.

**Owner and recovery.** The application creates the algorithm, trains/evaluates for an
explicit iteration or time bound, checkpoints, and stops live state within one Ray Job.
Resume only from a compatible immutable checkpoint.

**Evidence and limit.** django-ray has no algorithm, framework, environment, or
multi-agent evidence. A concrete adopter tuple should drive the first recipe.

### Ray Serve and Serve LLM

**Durable exchange.** Django calls a stable HTTP contract and stores bounded business
metadata or immutable model/config URIs. Never persist deployment, replica, router,
engine, or Serve handles.

**Owner and recovery.** Serve is long-lived desired state. Ray Serve owns application,
deployment, replica, routing, health, and autoscaling state inside one cluster. On
Kubernetes, a platform-applied `RayService` is the desired-state record and KubeRay
owns cluster rollout; the platform or GitOps layer owns approval, traffic switching,
and rollback. A public Django gateway can own normal Django request validation and
business permissions while it calls the private Serve data plane.

**Evidence and limit.** The [Ray Serve boundary](design/ray-serve-boundary.md) is a
design decision, not deployment support. Issue #284 owns the copyable Django gateway
recipe. Serve LLM remains deferred until a real model/engine/hardware tuple has bounded
evidence.

### Compiled Graph

**Durable exchange.** Only bounded invocation values and ordinary durable outputs may
cross the task boundary. `CompiledDAGRef`, channels, actors, and `ObjectRef` values stay
with the compiling process.

**Owner and recovery.** One process owns compilation, repeated invocation, bounded
in-flight results, one-time result consumption, draining, and teardown inside one
finite run.

**Evidence and limit.** Base `ray.dag` imports, but Ray classifies Compiled Graph as
beta and its native dependencies/capability vary by platform. django-ray's
[Compiled Graph compatibility policy](compiled-graph-compatibility.md) has no verified
native capability row and enables no product strategy. The pinned Ray 2.56.0 KubeRay
pilot can complete functional probes but still fails the residual-resource cleanup
invariant; import or canary success cannot promote it.

## Adoption checklist

Before deploying an optional component workload:

1. Choose a finite workload; keep online serving on the separate Serve lifecycle.
2. Lock the exact Ray/component/framework/accelerator/storage dependency tuple in an
   immutable image or RuntimeEnv that matches the cluster.
3. Use a dedicated queue with explicit task-manager concurrency and Ray resources.
4. Define bounded JSON input, immutable output/checkpoint namespaces, an idempotency
   key, restore policy, completion manifest, and cleanup owner.
5. Test first attempt, outer retry, component recovery, timeout/cancellation, partial
   output, cluster loss, and manifest reuse against the real component and storage.
6. Add a focused recipe and end-to-end evidence before calling that tuple supported.

## Official Ray references

- [Installing Ray](https://docs.ray.io/en/latest/ray-overview/installation.html)
- [Ray Client](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/ray-client.html)
- [Ray Job submission](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/doc/ray.job_submission.JobSubmissionClient.submit_job.html)
- [Runtime environments](https://docs.ray.io/en/latest/ray-core/handling-dependencies.html)
- [State API](https://docs.ray.io/en/latest/ray-observability/state/state-api.html)
- [Ray Data execution configuration](https://docs.ray.io/en/latest/data/execution-configurations.html)
- [Ray Train fault tolerance](https://docs.ray.io/en/latest/train/user-guides/fault-tolerance.html)
- [Ray Tune restore](https://docs.ray.io/en/latest/tune/api/doc/ray.tune.Tuner.restore.html)
- [RLlib checkpoints](https://docs.ray.io/en/latest/rllib/checkpoints.html)
- [Ray Serve architecture](https://docs.ray.io/en/latest/serve/architecture.html)
- [KubeRay RayService](https://docs.ray.io/en/latest/serve/production-guide/kubernetes.html)
- [Ray Serve LLM](https://docs.ray.io/en/latest/serve/llm/index.html)
- [Compiled Graph overview](https://docs.ray.io/en/latest/ray-core/compiled-graph/ray-compiled-graph.html)
- [Compiled Graph limitations](https://docs.ray.io/en/latest/ray-core/compiled-graph/troubleshooting.html)
- [Ray Workflows removal in 2.56.0](https://github.com/ray-project/ray/blob/ray-2.56.0/python/ray/workflow/__init__.py)
