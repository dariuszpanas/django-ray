# Sanitized Adopter Observation Records (v1)

This document records sanitized external deployment observations collected under
[GitHub issue #606](https://github.com/dariuszpanas/django-ray/issues/606) for the
[django-ray 1.0 readiness checklist](../one-zero-readiness.md#adoption-evidence).

## Sanitization and privacy attestation

- **Reviewer role**: Maintainer (`@dariuszpanas`)
- **Attestation**: This record contains no secrets, task arguments, task results,
  `RuntimeEnv` contents, tokens, customer identities, private infrastructure
  names, hostnames, cluster UIDs, or raw logs. All workload volumes and durations
  are expressed as coarse categories.

---

## Record 1: `adopter-internal-01` (Fixed-size Linux KubeRay deployment)

| Field | Observation |
|---|---|
| **Record ID** | `adopter-internal-01` |
| **Observation window** | Through 2026-11-01 (ongoing internal production tool) |
| **django-ray versions** | `0.6.x` upgraded to `0.7.1` |
| **Runtime tuple** | Python 3.12, Django 6.0.8+, Ray 2.58.0+, PostgreSQL |
| **Platform & topology** | Linux Kubernetes with KubeRay (`RayCluster` fixed worker replica count, autoscaling disabled) |
| **Execution modes** | Cluster Ray Job (`ray_job`) and Cluster Ray Core (`ray_core`) |
| **Workload class** | Internal operations and data-processing tasks, including in-task Ray fan-out and multi-step workflows |
| **Upgrade path & state retention** | Coordinated stopped-writer upgrade (`0.6.x` → `0.7.1`) with queue drain during an agreed maintenance window; historical `RayTaskExecution` rows, results, and durable input/request references preserved without rollback |
| **Incidents & stability** | Zero runtime bugs, lost tasks, or stability blockers observed |
| **Operational findings & follow-up issues** | 1. Coordinated upgrade queue drain required a planned maintenance window; acceptable for internal tooling with agreed downtime, and tracked for ergonomic improvement in [#368](https://github.com/dariuszpanas/django-ray/issues/368).<br>2. Initial KubeRay pod/environment cold start was a minor one-time latency annoyance before steady-state operation.<br>3. Choosing between `ray_job` vs. `ray_core` and single-task Ray fan-out vs. `chain`/`group`/`map_step` workflows required operator domain knowledge; addressed with explicit decision tables in [#684](https://github.com/dariuszpanas/django-ray/issues/684). |

---

## Record 2: `adopter-internal-02` (Autoscaled Linux KubeRay deployment)

| Field | Observation |
|---|---|
| **Record ID** | `adopter-internal-02` |
| **Observation window** | Through 2026-11-01 (ongoing internal production tool) |
| **django-ray versions** | `0.6.x` upgraded to `0.7.1` |
| **Runtime tuple** | Python 3.12, Django 6.0.8+, Ray 2.58.0+, PostgreSQL |
| **Platform & topology** | Linux Kubernetes with KubeRay (`RayCluster` with `enableInTreeAutoscaling: true` and elastic worker group scaling) |
| **Execution modes** | Cluster Ray Job (`ray_job`) and Cluster Ray Core (`ray_core`) |
| **Workload class** | Elastic batch and fan-out processing across variable-size internal workloads |
| **Upgrade path & state retention** | Coordinated stopped-writer upgrade (`0.6.x` → `0.7.1`) with queue drain during an agreed downtime window; historical task rows, results, and artifacts retained without rollback |
| **Incidents & stability** | Smooth and stable execution across scale-up and scale-down cycles; zero runtime bugs or stoppers |
| **Operational findings & follow-up issues** | 1. Cold-start latency when scaling worker pods from zero or cold images was noticeable on initial bursts; mitigated by keeping `minReplicas >= 1` for latency-sensitive queues and pre-baking dependencies into the container image ([#684](https://github.com/dariuszpanas/django-ray/issues/684)).<br>2. Queue draining before upgrades was a minor operational annoyance during scheduled maintenance ([#368](https://github.com/dariuszpanas/django-ray/issues/368)).<br>3. LLM coding assistants and new operators benefited from a concise decision matrix covering `RUNNER` modes, fan-out patterns, and KubeRay autoscaling settings ([#684](https://github.com/dariuszpanas/django-ray/issues/684)). |
