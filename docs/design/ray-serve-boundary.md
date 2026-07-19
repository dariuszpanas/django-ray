# Ray Serve Integration Boundary

## Status and decision

**Decision: defer package integration and keep Ray Serve application-owned.**

django-ray will not add Serve fields to `RayTaskExecution`, a `django-ray[serve]`
Django app, deployment APIs, or a Serve reconciler at this time. Applications can call
an independently operated Serve endpoint as they would any other authenticated service.
Operators should continue to own Serve configuration and cluster rollout through their
existing deployment control plane, normally GitOps and KubeRay `RayService` on
Kubernetes.

The repository's Kubernetes example currently deploys a KubeRay `RayCluster`, not a
`RayService`. Adopting Serve would therefore change the infrastructure ownership model;
it is not a small extension of the example Django application.

If repeated production demand demonstrates that Django-specific deployment records and
approval workflows are valuable, the preferred boundary is a separately versioned
companion package. The companion would own its models, migrations, reconciliation
process, credentials contract, and Ray Serve dependency. No experimental API is
proposed without that evidence.

This is a design outcome, not a commitment to ship Serve orchestration.

## Why task execution is the wrong lifecycle

`RayTaskExecution` represents one finite, at-least-once task attempt chain. It has a
queue, retry policy, timeout, result, and terminal state. A Serve application is a
long-lived desired state made of deployments and replicas. Ray's Serve controller
continuously reconciles that state, replaces failed replicas, routes requests, and
controls autoscaling.

Putting Serve state on `RayTaskExecution` would create two conflicting owners for a
single deployment. A task could finish while a deployment remains unhealthy, or retry
after the first attempt already changed the cluster. Task timeout, cancellation, and
result semantics do not define deployment rollout or rollback semantics.

The durable boundaries therefore remain separate:

- django-ray owns Django task enqueueing, finite execution, retry, cancellation, and
  results.
- Ray Serve owns application, deployment, replica, proxy, and autoscaling state inside
  one Ray cluster.
- The platform control plane owns cluster creation, network exposure, secrets,
  deployment approval, traffic switching, and rollback.

## Concrete adopter workflow

Consider a team promoting model version `fraud-v42` from staging to production while
keeping `fraud-v41` available for rollback.

1. The model owner publishes immutable model artifacts and an application image or
   remote RuntimeEnv URI. A reviewed Serve config identifies the application import
   path, route, artifact version, resource requirements, replica or autoscaling limits,
   graceful shutdown settings, and health-check behavior.
2. CI validates the import path, config schema, model startup, health check, and a small
   inference contract. It does not deploy through a `RayTaskExecution`.
3. A platform-owned identity applies the reviewed config. On Kubernetes, a `RayService`
   resource remains the desired-state record and KubeRay owns the cluster rollout.
4. The release is not accepted merely because the apply call returned. Operators wait
   for the observed generation, proxies, application, and every required deployment to
   report healthy. A synthetic inference request must pass before a stabilization window
   for latency, error rate, saturation, and model-level checks.
5. Scaling-only or JSON `user_config` changes can use Serve's lightweight in-place
   update path. Import-path, RuntimeEnv, image, and cluster changes use a new cluster and
   controlled traffic switch for production safety.
6. Rollback reapplies the last-known-good immutable config or switches traffic back to
   the previous healthy cluster. The platform records who approved the rollback and
   which desired-state revision was restored.

Django may own the business approval screen or model-registry reference for this
workflow. It must not expose raw Serve or Dashboard write access to application users,
and it must not treat a background task's success as proof that the rollout is healthy.

## Ownership contract

| Owner | Controls | Does not control |
|---|---|---|
| Django application | Business approval, model metadata, caller authentication, request-level audit, calls to the stable Serve endpoint | Replica lifecycle, cluster credentials, raw Dashboard or Serve APIs |
| Ray Serve | Application goal state inside one cluster, deployments, replicas, routing, health checks, graceful replacement, autoscaling | Infrastructure capacity, external identity, cross-cluster traffic switching, business approval |
| Platform operators or GitOps | Ray clusters, `RayService`, images, namespaces, secrets, ingress, service identities, rollout gates, observability, rollback | Django task retry/result semantics |

There must be one deployment writer for each Serve application and environment. A
Django UI may propose or approve a revision, but a platform reconciler applies it using
a dedicated service identity.

## Persistence and reconciliation boundary

The current recommendation creates no django-ray persistence. Git or the platform's
declarative resource is authoritative for desired configuration; Serve and KubeRay
report observed state.

If a companion package is justified later, it must use separate records such as an
application release and reconciliation attempt. The minimum persisted identity would
include:

- environment and opaque cluster reference;
- Serve application name and desired config digest;
- immutable image or remote RuntimeEnv identity;
- desired revision, observed revision, and last-known-good revision;
- rollout state, health summary, timestamps, and bounded diagnostics;
- approval and operator audit references without embedded credentials.

Its reconciler must be independently deployable, idempotent, generation-fenced, and
single-writer. It may correlate a release with a Django business object, but it must not
foreign-key deployment lifecycle to `RayTaskExecution` or reuse task retry,
cancellation, timeout, and terminal states.

## Failure, health, upgrade, and rollback semantics

- **Apply failure:** preserve the previous desired revision as last known good. Record a
  bounded error, make no success claim, and require an explicit retry or new revision.
- **Unhealthy deployment:** wait for Serve's application and deployment status, not
  only HTTP acceptance of the config. Replica health checks cause Serve to replace
  unhealthy replicas, but application-level readiness still needs an operator-defined
  stabilization gate.
- **Controller or node failure:** Serve can recover controller and replica actors, but
  full cluster recovery depends on the deployment platform. KubeRay and GCS fault
  tolerance are operational prerequisites for the corresponding guarantees.
- **Lightweight update:** replica count, autoscaling, selected request limits, graceful
  shutdown settings, and `user_config` may be updated in place when compatible.
- **Code or environment update:** changes to import paths, RuntimeEnv, actor resources,
  images, or cluster configuration require a replacement rollout. Production code
  updates should use a new cluster and traffic switch.
- **Rollback:** reapply an immutable last-known-good application config or route back to
  the previous cluster. Rollback is an explicit platform action, not an automatic Django
  task retry. The normal KubeRay replacement flow deletes the old cluster after cutover;
  an objective for rapid post-cutover rollback requires separately retained stable and
  candidate `RayService` resources plus external traffic switching.
- **Upgrade strategy:** use standard blue/green replacement as the conservative
  baseline. KubeRay's incremental RayService upgrade is currently alpha and does not
  support rollback, so it must not be an integration default.
- **Rolling compatibility:** the control plane must validate Ray, KubeRay, Serve config,
  application image, and model-artifact compatibility before rollout. It must retain
  the previous revision until the new revision passes health and stabilization gates.

## Security and trust boundaries

Ray executes trusted code and expects network isolation. Built-in token authentication
is defense in depth, not tenant isolation. A future integration must therefore require:

- private cluster and Dashboard/Serve control-plane endpoints behind network policy and
  an authenticated proxy;
- separate Ray clusters for durable task execution and online serving by default, so a
  Serve cluster replacement cannot invalidate in-flight Ray Core or Ray Job handles;
- separate Ray clusters for workloads that require security isolation;
- a least-privilege platform service identity for deployment writes;
- credentials sourced from the platform secret manager and never stored in Django
  models, task arguments, logs, or project configuration;
- application-level authentication and authorization at the Serve ingress;
- immutable audit events for proposal, approval, apply, health decision, and rollback;
- explicit quotas and resource policy before any multi-tenant claim.

Django user sessions must never be forwarded as authority to the Ray Dashboard or
Serve REST API. Control-plane responses and logs require the same bounding and
redaction rules as django-ray's existing operational surfaces.

Sharing a task and Serve cluster is acceptable only inside one explicit trust,
credential, capacity, availability, and failure domain, with evidence that cluster
replacement and Serve resource pressure cannot disrupt durable task reconciliation.

## Packaging options

| Option | Base impact | Lifecycle fit | Decision |
|---|---|---|---|
| `django-ray[serve]` app | Adds Django models, migrations, settings, and Serve dependencies to this release train | Couples finite tasks to persistent deployment orchestration | No-go |
| Companion package | Keeps dependencies, models, reconciliation, and compatibility policy independent | Correct boundary if multiple adopters need the same Django workflow | Defer pending evidence |
| Application-owned integration | Adds no django-ray dependency or public API; applications use their own HTTP client and deployment manifests | Matches current operator and Serve ownership | Recommended now |

django-ray currently requires `ray[default]`. In the resolved Ray 2.56.0 metadata,
`ray[serve]` adds five dependency declarations not present in the `default` extra:
`mmh3`, `watchfiles`, `uvicorn[standard]`, `starlette`, and `fastapi`. Their transitive
and platform-specific dependencies would also enter the resolver. Application-owned
integration leaves the django-ray base and compatibility matrix unchanged; a future
companion must declare and test the Serve extra itself.

## Adoption gates for reconsideration

Open a new implementation issue only when all of these are available:

1. At least two production adopters need the same Django-owned approval and audit
   workflow rather than application-owned GitOps.
2. Their target platforms, cluster topology, authentication method, rollback policy,
   and availability objectives are documented.
3. A stable public control surface can implement the required operations without
   relying on private Ray APIs. Declarative Serve config or `RayService` is preferred.
4. Separate models and a reconciler can be owned without changing
   `RayTaskExecution` semantics or making Serve a required dependency.
5. The test plan covers config compatibility, idempotency, generation fencing,
   unhealthy rollout, credential redaction, cluster failure, and rollback.

Until then, no package API, migration, prototype, or follow-up implementation issue is
warranted.

## Primary evidence

- [Ray Serve architecture](https://docs.ray.io/en/latest/serve/architecture.html)
- [Serve config files](https://docs.ray.io/en/latest/serve/production-guide/config.html)
- [Updating applications in place](https://docs.ray.io/en/latest/serve/advanced-guides/inplace-updates.html)
- [Serve monitoring and status](https://docs.ray.io/en/latest/serve/monitoring.html)
- [Serve fault tolerance](https://docs.ray.io/en/latest/serve/production-guide/fault-tolerance.html)
- [KubeRay RayService deployments](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/rayservice.html)
- [KubeRay incremental upgrade limits](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/rayservice-incremental-upgrade.html)
- [Ray security](https://docs.ray.io/en/latest/ray-security/index.html)
- [Ray Dashboard security](https://docs.ray.io/en/latest/cluster/configure-manage-dashboard.html)
