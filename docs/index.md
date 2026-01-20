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
- [Task Definition](tasks.md) - Defining and enqueueing tasks
- [Queues](queues.md) - Working with task queues
- [Retry & Error Handling](retry.md) - Configuring retries and handling failures

## Deployment

- [Kubernetes Deployment](deployment/kubernetes.md) - Deploy to Kubernetes
- [Docker](deployment/docker.md) - Running with Docker
- [TLS Configuration](deployment/tls.md) - Securing Ray cluster communication

## Reference

- [CLI Reference](reference/cli.md) - Command-line interface
- [Settings Reference](reference/settings.md) - All settings
- [API Reference](reference/api.md) - How to build your own API (with testproject examples)

## Development

- [Contributing](contributing.md) - How to contribute
- [Architecture](architecture.md) - System design overview
- [Changelog](changelog.md) - Release history

