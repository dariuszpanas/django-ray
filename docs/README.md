# django-ray Documentation

Welcome to the django-ray documentation. django-ray is a Ray-based backend for Django Tasks that enables distributed task execution with database-backed reliability.

## What is django-ray?

django-ray is a **library** that provides:

- `RayTaskBackend` - A Django Tasks backend
- `RayTaskExecution` model - Task execution tracking in your database
- `TaskWorkerLease` model - Worker coordination for distributed deployments
- `django_ray_worker` command - Management command to process tasks
- Django Admin integration - Monitor and manage tasks

> **Note**: This repository also contains a `testproject/` directory with example code demonstrating django-ray features. The testproject (including its REST API) is **not part of the django-ray library** - it's provided for learning and testing purposes only.

## User Guide

- [Getting Started](getting-started.md) - Installation and basic setup
- [Configuration](configuration.md) - All configuration options
- [Worker Modes](worker-modes.md) - Understanding execution modes
- [Performance](performance.md) - Choosing execution and batching strategies
- [Observability](observability.md) - Versioned services, metrics, and live admin updates
- [Compatibility](compatibility.md) - Supported versions and dependency policy
- [Compiled Graph Compatibility](compiled-graph-compatibility.md) - Fail-closed native
  capability policy and canary evidence
- [Task Definition](tasks.md) - Defining and enqueueing tasks
- [Queues](queues.md) - Working with task queues
- [Retry & Error Handling](retry.md) - Configuring retries and handling failures

## Deployment

- [Kubernetes Deployment](deployment/kubernetes.md) - Deploy to Kubernetes
- [Docker](deployment/docker.md) - Running with Docker
- [TLS Configuration](deployment/tls.md) - Securing Ray cluster communication
- [Operator Runbook](runbook.md) - Incident diagnosis and manual recovery

## Reference

- [CLI Reference](reference/cli.md) - Command-line interface
- [Settings Reference](reference/settings.md) - All settings
- [Durable Input Storage](reference/input-storage.md) - Oversized JSON input storage and cleanup
- [Result Storage](reference/result-storage.md) - Oversized result backends and retrieval
- [Handle Compatibility](reference/handle-compatibility.md) - Ray Core handle formats and migration policy
- [API Reference](reference/api.md) - How to build your own API (with testproject examples)

## Development

- [Contributing](contributing.md) - How to contribute
- [Architecture](architecture.md) - System design overview
- [Workflow Plan Contract](workflow-plans.md) - Versioned plan vocabulary,
  classification, identity, and strategy eligibility
- [ADR-0001: Workflow Plans](design/adr-0001-workflow-plan-contract.md) - Why
  execution plans and strategies remain separate from Django task identity
- [ADR-0002: Compiled Sessions](design/adr-0002-compiled-session-ownership.md) -
  Initial local/direct CPU-pilot owner, deferred production topology, within-run reuse,
  admission, invalidation, and drain rules
- [ADR-0003: Compiled Invocation Lifecycle](design/adr-0003-compiled-invocation-lifecycle.md) -
  Ray-free session/invocation reducer, absolute deadlines, fallback and replay cutoffs,
  one-shot output ownership, and bounded cleanup diagnostics
- [ADR-0004: Bounded Workflow Progress](design/adr-0004-bounded-workflow-progress.md) -
  Always-bounded summaries, immutable topology pages, normalized latest-state detail,
  fenced publication, pagination, retention, and legacy snapshot rollout
- [ADR-0005: Bounded Workflow Progress Preparation](design/adr-0005-bounded-workflow-preparation.md) -
  Selected non-production contract and prototype for exact one-shot preparation through
  a private, budgeted SQLite spill workspace; #141/#142 still own runtime integration
- [Ray Serve Integration Boundary](design/ray-serve-boundary.md) - Deferred ownership,
  lifecycle, security, and packaging decision
- [Changelog](changelog.md) - Release history

