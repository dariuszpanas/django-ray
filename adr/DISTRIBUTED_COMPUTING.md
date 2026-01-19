# Distributed Computing in Django-Ray

## Architecture Overview

Django-Ray supports two levels of parallelism:

### Level 1: Task Queue Parallelism
Multiple Django-Ray workers process tasks from the queue concurrently. Each worker connects to Ray as a client and submits tasks.

```
                    ┌─────────────────┐
                    │   Task Queue    │
                    │   (Database)    │
                    └────────┬────────┘
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │ Django-Ray   │   │ Django-Ray   │   │ Django-Ray   │
   │  Worker 1    │   │  Worker 2    │   │  Worker 3    │
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          │                  │                  │
          └──────────────────┼──────────────────┘
                             ▼
                    ┌─────────────────┐
                    │   Ray Cluster   │
                    │  (Execution)    │
                    └─────────────────┘
```

### Level 2: Distributed Computing Within Tasks

**This is the key insight**: A task running on Ray CAN spawn additional Ray tasks to leverage the entire cluster!

```python
from django_ray.runtime.distributed import parallel_map, is_ray_available

@task(queue_name="default")
def distributed_search(pattern: str, sources: list[str]) -> dict:
    # This spawns Ray tasks across the ENTIRE cluster!
    results = parallel_map(search_single_source, sources, pattern=pattern)
    return aggregate(results)
```

When this task runs:
1. Django-Ray worker claims task from DB
2. Worker submits task to Ray (runs on 1 Ray worker)
3. **Inside the task**, `parallel_map()` spawns N additional Ray tasks
4. These N tasks run across ALL Ray workers in parallel
5. Results are gathered and returned

## API Reference

### `parallel_map(func, items, **kwargs)`

Execute a function over items in parallel across the Ray cluster.

```python
from django_ray.runtime.distributed import parallel_map

def process_item(item, multiplier=1):
    return item * multiplier

# Runs across all available Ray workers!
results = parallel_map(process_item, [1, 2, 3, 4, 5], multiplier=10)
# Returns [10, 20, 30, 40, 50]
```

**Parameters:**
- `func`: Function to apply (must be picklable)
- `items`: List of items to process
- `num_cpus`: CPUs per task (default: 1.0)
- `num_gpus`: GPUs per task (default: 0.0)
- `max_concurrency`: Limit concurrent tasks (default: unlimited)
- `**kwargs`: Additional arguments passed to func

### `parallel_starmap(func, items)`

Like `parallel_map` but unpacks argument tuples.

```python
from django_ray.runtime.distributed import parallel_starmap

def add(a, b):
    return a + b

results = parallel_starmap(add, [(1, 2), (3, 4), (5, 6)])
# Returns [3, 7, 11]
```

### `scatter_gather(tasks)`

Execute multiple different functions in parallel.

```python
from django_ray.runtime.distributed import scatter_gather

def fetch_users(): ...
def fetch_orders(): ...  
def fetch_products(): ...

# All three run in parallel across the cluster
users, orders, products = scatter_gather([
    (fetch_users, (), {}),
    (fetch_orders, (), {}),
    (fetch_products, (), {}),
])
```

### Utility Functions

```python
from django_ray.runtime.distributed import (
    is_ray_available,  # Check if Ray is connected
    get_num_workers,   # Number of Ray worker nodes
    get_total_cpus,    # Total CPUs in cluster
    get_ray_resources, # Full resource dictionary
)
```

## Automatic Fallback

All distributed utilities automatically fall back to sequential execution when Ray is not available:

```python
# Works in both cases:
# - With Ray: Runs in parallel across cluster
# - Without Ray: Runs sequentially (useful for testing)
results = parallel_map(my_func, items)
```

## Example: Distributed Search

```python
from django.tasks import task
from django_ray.runtime.distributed import (
    parallel_starmap,
    is_ray_available,
    get_total_cpus,
)

def search_source(source: str, pattern: str) -> dict | None:
    """Runs on individual Ray workers."""
    if pattern in source:
        return {"source": source, "found": True}
    return None

@task(queue_name="default")
def distributed_search(pattern: str, sources: list[str]) -> dict:
    """Searches all sources in parallel across the cluster."""
    
    # Create argument tuples
    args = [(source, pattern) for source in sources]
    
    # This distributes across ALL Ray workers!
    all_results = parallel_starmap(search_source, args)
    
    # Filter and return
    matches = [r for r in all_results if r is not None]
    
    return {
        "pattern": pattern,
        "sources_searched": len(sources),
        "matches": len(matches),
        "distributed": is_ray_available(),
        "cluster_cpus": get_total_cpus(),
    }
```

## Performance Characteristics

| Scenario | 100 sources | Sequential | 8 CPU Cluster |
|----------|-------------|------------|---------------|
| Search (10ms each) | 1000ms | ~125ms | **8x faster** |
| HTTP fetch (50ms each) | 5000ms | ~625ms | **8x faster** |

The speedup scales with cluster size. A 32-CPU cluster would be ~32x faster for embarrassingly parallel workloads.

## Best Practices

1. **Use for I/O or CPU-bound work**: Network requests, data processing, etc.
2. **Keep functions picklable**: No closures over complex objects
3. **Batch small items**: For tiny operations, batch them first
4. **Handle failures**: Individual tasks might fail; handle None/exceptions
5. **Test without Ray**: The fallback ensures your code works everywhere

