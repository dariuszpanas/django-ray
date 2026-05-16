# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-15

### Added

- Pluggable oversized result storage backends:
  - `RESULT_STORAGE_BACKEND="digest"` (default metadata-only pointer)
  - `RESULT_STORAGE_BACKEND="filesystem"` with `RESULT_STORAGE_FILESYSTEM_PATH`
  - `RESULT_STORAGE_BACKEND="s3"` with `RESULT_STORAGE_S3_BUCKET` (+ optional prefix/region/endpoint)
  - `RESULT_STORAGE_BACKEND="gcs"` with `RESULT_STORAGE_GCS_BUCKET` (+ optional prefix)
- Filesystem-backed `result_reference` format (`resultfs://...`) for retrievable oversized payloads.
- Object-storage `result_reference` formats (`s3://...`, `gs://...`) for retrievable oversized payloads.
- Result storage reference documentation: `docs/reference/result-storage.md`.
- Opt-in live Ray cluster fault-injection integration suite:
  - `tests/integration/test_live_failure_injection.py`
  - configured by `DJANGO_RAY_LIVE_CLUSTER_TESTS`, `DJANGO_RAY_LIVE_RAY_ADDRESS`, `DJANGO_RAY_LIVE_MIN_NODES`.
- Optional package extras for result storage SDKs:
  - `django-ray[s3]`
  - `django-ray[gcs]`
  - `django-ray[object-storage]`
- Explicit documentation of at-least-once execution semantics and idempotency
  requirements for side-effecting tasks.
- Dedicated manual GitHub Actions workflow for live cluster fault tests:
  - `.github/workflows/live-cluster.yml`
  - default CI now excludes `live_cluster` tests.
- Zensical documentation toolchain:
  - `zensical.toml` site configuration
  - strict docs build workflow: `.github/workflows/docs.yml`
  - Make targets for docs build/serve.
- Read the Docs configuration for building the Zensical site as a custom static HTML build.
- KubeRay operator integration assets for Kubernetes:
  - `k8s/overlays/kuberay-kind/ray-cluster-kuberay.yaml` (`RayCluster` + dashboard service)
  - `k8s/overlays/kuberay-kind/` local kind overlay (removes legacy static Ray Deployments from base)
  - Make targets for install/deploy/delete/status using KubeRay.

### Changed

- Worker oversized result handling now routes through configurable storage backends and
  falls back to digest-only references if backend persistence fails at runtime.
- `RayTaskBackend.get_result()` now reloads retrievable oversized payloads from
  `result_reference` when `result_data` is empty.
- Ray Job reconciliation now adopts orphaned persisted jobs from inactive workers before
  falling back to timeout-based stuck recovery.
- Ray Core reconnect cleanup now routes stale in-flight tasks through the normal retry policy.
- Ray-dependent integration tests now skip cleanly when local embedded Ray cannot start.
- Versioned docs deployment is deferred; docs currently build as a single Zensical site.

## [0.1.1] - 2026-01-20

### Fixed

- Documentation and README link corrections.

## [0.1.0] - 2026-01-19

Initial release.

### Added

- **Django Tasks Integration**: Ray-based backend for Django 6's native Tasks framework
- **Multiple Execution Modes**:
  - `--sync`: Direct execution without Ray (for testing)
  - `--local`: Local Ray cluster via `@ray.remote`
  - `--cluster`: Remote Ray cluster via `@ray.remote`
  - Default: Ray Job Submission API (process isolation)
- **Database-Backed Reliability**:
  - Task state tracking in PostgreSQL/SQLite
  - Automatic retries with exponential backoff
  - Configurable retry exception denylist
  - Stuck task detection and recovery
- **Worker Management**:
  - `django_ray_worker` management command
  - Worker lease coordination for distributed deployments
  - Graceful shutdown handling
  - Concurrent task processing
- **Django Admin Integration**:
  - Task execution monitoring
  - Manual retry and cancel actions
  - Ray Dashboard deep links
  - Color-coded task states
- **Kubernetes Deployment**:
  - Kustomize manifests for K8s deployment
  - TLS support for Ray cluster communication
  - PostgreSQL and Ray cluster configuration
  - Prometheus/Grafana monitoring setup
- **Distributed Computing Utilities**:
  - `parallel_map` for parallel task execution
  - `scatter_gather` for heterogeneous parallel tasks
  - Full Django ORM access from Ray workers

### Requirements

- Python 3.12 or 3.13
- Django 6.0+
- Ray 2.53.0+
- PostgreSQL (recommended) or SQLite

[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dariuszpanas/django-ray/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/dariuszpanas/django-ray/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dariuszpanas/django-ray/releases/tag/v0.1.0
