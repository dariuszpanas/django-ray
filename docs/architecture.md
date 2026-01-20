# Architecture

This document provides a high-level overview of django-ray's architecture.

## System Overview

django-ray integrates Django 6 Tasks with Ray by providing:

- **A Worker** that claims tasks from the database and submits them to Ray
- **A metadata layer** in the database that tracks execution attempts and state
- **Pluggable execution adapters** for different Ray execution modes

### Design Principles

- **Database is the system of record** for task state
- **Ray is the execution fabric** for distributed computing
- **At-least-once execution** with idempotency encouraged

## Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        Your Application                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Django Web / API                       │   │
│  │  • Define tasks with @task decorator                      │   │
│  │  • Enqueue tasks with .enqueue()                          │   │
│  │  • Read status/results                                    │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     PostgreSQL Database                          │
│  ┌─────────────────────┐  ┌─────────────────────────────────┐   │
│  │  Django Task Data   │  │  django-ray Metadata            │   │
│  │  (canonical state)  │  │  • RayTaskExecution             │   │
│  │                     │  │  • TaskWorkerLease              │   │
│  └─────────────────────┘  └─────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                    │                         │
                    ▼                         ▼
┌───────────────────────────────┐   ┌─────────────────────────────┐
│     django-ray Worker         │   │       Ray Cluster           │
│  ┌─────────────────────────┐  │   │  ┌────────────────────────┐ │
│  │ Task Claimer (polling)  │  │   │  │  Ray Head (Dashboard)  │ │
│  │ Task Runner (submit)    │──┼──►│  └────────────────────────┘ │
│  │ State Reconciler        │  │   │  ┌────────────────────────┐ │
│  └─────────────────────────┘  │   │  │  Ray Worker            │ │
│  • Connects to DB             │   │  │  • Django ORM access   │ │
│  • Connects to Ray cluster    │   │  │  • Executes task code  │ │
└───────────────────────────────┘   │  └────────────────────────┘ │
                                    │  ┌────────────────────────┐ │
                                    │  │  Ray Worker            │ │
                                    │  │  • Django ORM access   │ │
                                    │  │  • Executes task code  │ │
                                    │  └────────────────────────┘ │
                                    └─────────────────────────────┘
```

**Key points:**
- Ray workers have full Django environment (same settings as web/worker pods)
- Ray workers can access Django ORM directly within tasks
- Database is shared across all components

## Data Model

### RayTaskExecution

Tracks each task execution attempt:

| Field | Description |
|-------|-------------|
| `id` | UUID primary key |
| `task_id` | Django task identifier |
| `task_name` | Callable import path |
| `state` | QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, LOST |
| `queue_name` | Queue assignment |
| `attempt_number` | Current attempt (1-based) |
| `args_json` | Serialized positional arguments |
| `kwargs_json` | Serialized keyword arguments |
| `result_data` | JSON result on success |
| `error_message` | Error message on failure |
| `error_traceback` | Full traceback on failure |
| `ray_job_id` | Ray Job ID (if using Job API) |
| `created_at` | Task creation time |
| `started_at` | Execution start time |
| `finished_at` | Execution end time |

### TaskWorkerLease

Tracks active workers for distributed coordination:

| Field | Description |
|-------|-------------|
| `worker_id` | Unique worker identifier |
| `hostname` | Worker hostname |
| `pid` | Process ID |
| `queue_name` | Queue being processed |
| `is_active` | Whether worker is active |
| `last_heartbeat_at` | Last heartbeat time |

## Task State Machine

```
         ┌─────────┐
         │ QUEUED  │◄──────────────────┐
         └────┬────┘                   │
              │                        │
              ▼                        │ (retry)
         ┌─────────┐                   │
         │ RUNNING │───────────────────┤
         └────┬────┘                   │
              │                        │
       ┌──────┼──────┐                 │
       │      │      │                 │
       ▼      ▼      ▼                 │
┌──────────┐ ┌──────┐ ┌───────────┐    │
│SUCCEEDED │ │FAILED│ │ CANCELLED │    │
└──────────┘ └──┬───┘ └───────────┘    │
                │                      │
                └──────────────────────┘
                (if retries remaining)
```

## Worker Loop

The worker runs a continuous loop:

```python
while running:
    # 1. Heartbeat
    update_lease()
    
    # 2. Claim tasks
    tasks = claim_available_tasks(limit=concurrency - in_flight)
    
    # 3. Submit to Ray
    for task in tasks:
        submit_to_ray(task)
    
    # 4. Reconcile
    for handle in in_flight_handles:
        status = check_ray_status(handle)
        update_task_state(status)
    
    # 5. Recovery
    detect_and_recover_stuck_tasks()
    
    sleep(poll_interval)
```

## Execution Adapters

### RayJobRunner

Uses Ray's Job Submission API:
- Process isolation per task
- Higher overhead
- Good for long-running tasks

### RayCoreRunner

Uses `@ray.remote` directly:
- Lower overhead
- Tasks share Ray worker processes
- Good for high-throughput

## Serialization

Tasks are serialized for execution:

1. **Import path**: `"myapp.tasks.process_data"`
2. **Arguments**: JSON-serialized `args` and `kwargs`
3. **Execution**: Worker imports callable and invokes with deserialized args

### Entrypoint

```python
# django_ray.runtime.entrypoint
def execute_task(callable_path, args_json, kwargs_json):
    # 1. Setup Django
    django.setup()
    
    # 2. Import callable
    func = import_callable(callable_path)
    
    # 3. Deserialize arguments
    args = json.loads(args_json)
    kwargs = json.loads(kwargs_json)
    
    # 4. Execute
    result = func(*args, **kwargs)
    
    # 5. Return serialized result
    return json.dumps({"success": True, "result": result})
```

## Distributed Computing

Within tasks, use Ray's distributed primitives:

```python
from django_ray.runtime.distributed import parallel_map

@task(queue_name="default")
def process_batch(items):
    # Parallel execution across Ray cluster
    return parallel_map(process_item, items)
```

## See Also

- [Worker Modes](worker-modes.md) - Execution modes
- [Configuration](configuration.md) - Settings
- [Contributing](contributing.md) - Development guide

