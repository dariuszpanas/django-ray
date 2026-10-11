# Bounded monitoring qualification

This separate hosted fixture observes the shipped monitoring topology with the
current application image. It runs no task/workflow workload and does not expand
the application qualification's budget. The exact-source Linux CI checkpoint
must pass before the fixture allocates its disposable Kind cluster.
The required `Qualification Gate` selects this reusable workflow for its owned
paths and monitoring/deployment inputs; a selected skipped or failed run blocks
the aggregate. Manual dispatch is diagnostic and cannot replace that PR result.

The renderer selects the shipped KubeRay Kind web, database, generic Ray head and
one worker, Prometheus, Grafana and dashboard importer. It preserves scrape
selectors, namespace-scoped discovery RBAC, dedicated metrics-token mounts,
private Services and web probe Host/path settings. Explicit fixture substitutions
are immutable resolved images, generated namespace/credentials, disposable bounded
volumes, one Ray worker and the resource limits below. No manager or existing PVC
is used. The source-selected image references, resolved digests, observed container
image IDs, source tree and non-secret rendered configuration digest stay in the
diagnostic artifact.
The observer temporarily receives the disposable Grafana administrator credential
inside the web pod. This is a fixture convenience, not production separation of
Grafana credentials from the application container.

## Admission and lifetime

Only a disposable GitHub-hosted Linux VM is admitted: at least four effective
CPUs, 15 GiB total memory (the 16 GiB runner class), 13 GiB currently available
memory and 20 GiB free disk. Admission runs before building and immediately before
Kind. Failure refuses allocation; it does not resize another environment.

| Component | CPU limit | Memory limit |
| --- | ---: | ---: |
| Ray head and worker | 1500m | 6656 MiB |
| Ray dashboard importer | 100m | 128 MiB |
| PostgreSQL | 200m | 512 MiB |
| Web and in-container observer | 250m | 768 MiB |
| Prometheus and Grafana | 700m | 768 MiB |
| KubeRay operator | 100m | 512 MiB |
| All admitted pods | 2850m | 9344 MiB |

Prometheus/Grafana retain their shipped requests of 150m/384 MiB combined.
The Kind cgroup is capped at 3.25 CPUs/12 GiB, leaving 400m/2944 MiB for its
control plane. A separate loopback-only registry is capped at 250m/128 MiB.
The host browser container is capped at 250m/512 MiB/256 PIDs, including its
96 MiB temporary filesystem and 128 MiB shared-memory ceiling. Combined caps
leave 250m CPU and more than 2 GiB for the host on the admitted runner. Affinity
and inherited cgroup limits participate in admission; rendered/live resource
totals include initialization containers. A namespace ResourceQuota prevents
extra or unbounded injected workloads before pod admission. The workflow verifies
actual Docker cgroup limits and retains only allowlisted limit fields.
A fixed ten-minute shared deadline covers namespace creation,
readiness and observations; namespace cleanup and outer infrastructure cleanup
have separate bounded deadlines. Resource limits are ceilings, not latency promises.

## Selected evidence

Run through `Monitoring Qualification`, or from its explicitly admitted runner:

```bash
python -m qualification.monitoring.run --images "$RUNNER_TEMP/monitoring-build/images.json" --output "$RUNNER_TEMP/monitoring-evidence"
```

The receipt requires two live generic Ray nodes, exactly one healthy Django,
head and worker scrape target, no obsolete worker scrape pool, isolated API and
metrics credentials (including refusal of execution access and preserved operator
access to metrics), Grafana anonymous/invalid/admin responses of 401/401/200,
the shipped `ray-cluster` dashboard and real imported `rayDefaultDashboard`, and
successful datasource UID proxy queries with actual target values. It also checks
the live web probe Host/path, database-backed readiness, private Services, no
Ingress, all six workload pods Ready with zero container/init restarts, immutable
image identities and namespace removal. The outer workflow separately confirms
that its cluster, registry and browser container are gone. Each exact-owned
object gets an independent removal and absence check even if another cleanup
fails; any failure makes the cleanup step fail after all attempts.

The fixture then replaces only Grafana, requires unchanged Ray pod identities,
observes that the provisioned dashboard returns while the generated dashboard is
absent, and runs the shipped importer's `--once` mode under the remaining deadline.
Both dashboard UIDs and real datasource query values must return afterward.
A separately capped browser uses only an owned loopback forward and a fixed
90-second render deadline, reserving 150 seconds of the shared budget for its
startup, rendering and cleanup. It loads the real imported Ray panel 41,
observes a visible canvas and numeric responses for that panel's CPU query,
and refuses a missing plugin. Success is checked after cleanup within the shared
deadline.
The generic source-locked Ray image independently verifies that panel's title,
UID and legacy `graph` definition. This proves one actual migration/render path,
not every dashboard, plugin or browser. Browser and forward cleanup are required.

The cold-Ray decision is **skip**: this finite platform observation does not
replace the separate application task/workflow cold-generation gate or the Beta
upgrade rehearsal. Grafana replacement does not count as a cold Ray generation.
Authentication checks are not a
general security certification: source-selected component versions require their
own supported-version and public-advisory review. Raw credentials, target errors,
response bodies and arbitrary container logs are never included in receipts.
