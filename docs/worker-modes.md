# Worker Execution Modes

django-ray supports multiple execution modes to fit different use cases, from local development to production clusters.

## Overview

| Mode | Flag | Ray Required | Use Case |
|------|------|--------------|----------|
| **sync** | `--sync` | No | Testing, debugging |
| **local** | `--local` | Local Ray | Development |
| **cluster** | `--cluster=<addr>` | Remote Ray | Production |
| **ray-job** | *(default)* | Remote Ray | Production (isolation) |

## Sync Mode

**Flag:** `--sync`

Executes tasks directly in the worker process without Ray. Useful for testing and debugging.

```bash
python manage.py django_ray_worker --queue=default --sync
```

**Characteristics:**
- No Ray installation required
- Sequential execution (one task at a time)
- Full Django context available
- Easy to debug with breakpoints

**When to use:**
- Unit testing
- Debugging task logic
- CI/CD pipelines without Ray

## Local Mode

**Flag:** `--local`

Starts a local Ray instance and executes tasks via `@ray.remote`. Recommended for development.

```bash
python manage.py django_ray_worker --queue=default --local
```

**Characteristics:**
- Automatic local Ray cluster
- Ray Dashboard available at http://127.0.0.1:8265
- Tasks run in separate Ray workers
- Supports `parallel_map` and distributed utilities

**When to use:**
- Local development
- Testing distributed patterns
- Single-machine deployments

## Cluster Mode

**Flag:** `--cluster=<address>`

Connects to an existing Ray cluster and executes tasks via `@ray.remote`.

```bash
python manage.py django_ray_worker --queue=default --cluster=ray://ray-head:10001
```

**Characteristics:**
- Connects to remote Ray cluster
- Tasks distributed across cluster workers
- Full Ray features available
- Lower overhead than Ray Job API

**When to use:**
- Production with dedicated Ray cluster
- When you need distributed computing features
- High-throughput workloads

## Ray Job Mode (Default)

**Flag:** None (default behavior)

Uses Ray's Job Submission API for process isolation.

```bash
python manage.py django_ray_worker --queue=default
```

**Characteristics:**
- Each task runs in isolated process
- Better fault isolation
- Higher overhead per task
- Requires `RAY_ADDRESS` in settings

**When to use:**
- Production requiring strict isolation
- Long-running tasks
- Tasks with heavy dependencies

## Comparison

| Feature | Sync | Local | Cluster | Ray Job |
|---------|------|-------|---------|---------|
| Ray required | ❌ | ✅ | ✅ | ✅ |
| Process isolation | ❌ | Partial | Partial | ✅ |
| Distributed computing | ❌ | ✅ | ✅ | ✅ |
| `parallel_map` support | ❌ | ✅ | ✅ | ✅ |
| Overhead | Lowest | Low | Low | Higher |
| Debugging ease | Best | Good | Moderate | Harder |

## Distributed Computing Utilities

In **local** and **cluster** modes, you can use distributed computing utilities within your tasks:

```python
from django.tasks import task
from django_ray.runtime.distributed import parallel_map, scatter_gather

@task(queue_name="default")
def process_batch(item_ids: list[int]) -> list[dict]:
    """Process items in parallel using Ray."""
    
    def process_one(item_id: int) -> dict:
        # This runs on Ray workers
        return {"id": item_id, "processed": True}
    
    # Automatically parallelized across Ray cluster
    results = parallel_map(process_one, item_ids)
    return results
```

**Available utilities:**
- `parallel_map(func, items)` - Apply function to items in parallel
- `parallel_starmap(func, args_list)` - Apply function with argument tuples
- `scatter_gather(tasks)` - Execute heterogeneous tasks in parallel
- `get_num_workers()` - Get number of Ray workers
- `get_ray_resources()` - Get cluster resources

## Choosing a Mode

```
┌─────────────────────────────────────────────────────────┐
│                    Which mode to use?                    │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │  Testing or debugging? │
              └────────────────────────┘
                     │           │
                    Yes          No
                     │           │
                     ▼           ▼
               ┌─────────┐  ┌───────────────────┐
               │  sync   │  │ Need distributed  │
               └─────────┘  │    computing?     │
                            └───────────────────┘
                                  │         │
                                 Yes        No
                                  │         │
                                  ▼         ▼
                           ┌──────────┐ ┌─────────┐
                           │ Have Ray │ │  sync   │
                           │ cluster? │ └─────────┘
                           └──────────┘
                              │     │
                             Yes    No
                              │     │
                              ▼     ▼
                        ┌─────────┐ ┌─────────┐
                        │ cluster │ │  local  │
                        └─────────┘ └─────────┘
```

## See Also

- [Getting Started](getting-started.md) - Basic setup
- [Configuration](configuration.md) - Worker configuration options
- [Kubernetes Deployment](deployment/kubernetes.md) - Production deployment

