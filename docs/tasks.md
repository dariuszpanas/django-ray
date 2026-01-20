# Defining Tasks

django-ray uses Django 6's native Tasks framework. Tasks are defined using the `@task` decorator and enqueued using `.enqueue()`.

## Basic Task Definition

```python
# myapp/tasks.py
from django.tasks import task

@task(queue_name="default")
def send_email(to: str, subject: str, body: str) -> dict:
    """Send an email."""
    # Your email sending logic here
    return {"sent_to": to, "subject": subject}
```

## Task Decorator Options

```python
@task(
    queue_name="default",      # Queue to use
    priority=0,                # Higher = processed first
)
def my_task():
    pass
```

## Enqueueing Tasks

### Basic Enqueueing

```python
from myapp.tasks import send_email

# Enqueue with positional args
result = send_email.enqueue("user@example.com", "Hello", "World")

# Enqueue with keyword args
result = send_email.enqueue(
    to="user@example.com",
    subject="Hello",
    body="World"
)

# Get the task ID
task_id = result.id
```

### Queue Selection

```python
# Use a specific queue
result = send_email.using(queue_name="high-priority").enqueue(
    to="vip@example.com",
    subject="Urgent",
    body="Important message"
)
```

### Checking Task Status

```python
# Get task result
result = send_email.enqueue(to="user@example.com", subject="Hi", body="Hello")

# Check status
print(result.status)  # TaskResultStatus.READY, RUNNING, SUCCESSFUL, FAILED

# Wait for completion and get result (blocking)
if result.status == TaskResultStatus.SUCCESSFUL:
    return_value = result.return_value
```

## Task Arguments

### JSON-Serializable Arguments

Task arguments must be JSON-serializable:

```python
@task(queue_name="default")
def process_data(
    user_id: int,
    items: list[str],
    options: dict[str, Any],
) -> dict:
    return {"processed": len(items)}

# Valid
process_data.enqueue(
    user_id=123,
    items=["a", "b", "c"],
    options={"verbose": True}
)
```

### What's Serializable

✅ **Supported:**
- Strings, integers, floats, booleans
- Lists and tuples (become lists)
- Dictionaries with string keys
- None
- Nested combinations of the above

❌ **Not Supported:**
- Django model instances (pass IDs instead)
- Datetime objects (pass ISO strings or timestamps)
- Custom classes (unless they have `__json__` method)
- Functions, lambdas

### Working with Models

```python
# ❌ Don't pass model instances
@task(queue_name="default")
def bad_task(user: User):  # Won't work!
    pass

# ✅ Pass IDs and fetch in the task
@task(queue_name="default")
def good_task(user_id: int) -> dict:
    from myapp.models import User
    user = User.objects.get(id=user_id)
    return {"email": user.email}
```

## Return Values

Task return values are stored in the database and must be JSON-serializable:

```python
@task(queue_name="default")
def analyze_data(data_id: int) -> dict:
    # Process data...
    return {
        "status": "completed",
        "records_processed": 1000,
        "summary": {"mean": 42.5, "std": 3.2}
    }
```

### Large Results

For large results, consider storing them externally:

```python
@task(queue_name="default")
def generate_report(report_id: int) -> dict:
    # Generate large report...
    report_data = generate_large_report()
    
    # Store in S3/external storage
    s3_key = f"reports/{report_id}.json"
    upload_to_s3(s3_key, report_data)
    
    # Return reference only
    return {"s3_key": s3_key, "size_bytes": len(report_data)}
```

## Error Handling

### Exceptions in Tasks

Exceptions are captured and stored:

```python
@task(queue_name="default")
def risky_task(value: int) -> int:
    if value < 0:
        raise ValueError("Value must be positive")
    return value * 2
```

The exception type, message, and traceback are stored in `RayTaskExecution`.

### Custom Error Handling

```python
@task(queue_name="default")
def task_with_cleanup(resource_id: int) -> dict:
    resource = acquire_resource(resource_id)
    try:
        result = process(resource)
        return {"success": True, "result": result}
    except Exception as e:
        # Log error, cleanup, etc.
        logger.error(f"Task failed: {e}")
        raise  # Re-raise to mark task as failed
    finally:
        release_resource(resource)
```

## Task Patterns

### Batch Processing

```python
@task(queue_name="default")
def process_batch(item_ids: list[int]) -> dict:
    results = []
    for item_id in item_ids:
        result = process_item(item_id)
        results.append(result)
    return {"processed": len(results), "results": results}
```

### Chained Tasks

```python
@task(queue_name="default")
def step_one(data: dict) -> dict:
    result = transform(data)
    # Enqueue next step
    step_two.enqueue(result=result)
    return {"status": "step_one_complete"}

@task(queue_name="default")
def step_two(result: dict) -> dict:
    final = finalize(result)
    return {"status": "complete", "final": final}
```

### Fan-Out Pattern

```python
@task(queue_name="default")
def coordinator(items: list[int]) -> dict:
    # Enqueue individual tasks
    task_ids = []
    for item in items:
        result = process_item.enqueue(item_id=item)
        task_ids.append(result.id)
    return {"spawned_tasks": task_ids}

@task(queue_name="default")
def process_item(item_id: int) -> dict:
    # Process single item
    return {"item_id": item_id, "processed": True}
```

## Using Distributed Computing

In **local** or **cluster** mode, use Ray's distributed computing:

```python
from django.tasks import task
from django_ray.runtime.distributed import parallel_map

@task(queue_name="default")
def parallel_process(item_ids: list[int]) -> list[dict]:
    """Process items in parallel across Ray cluster."""
    
    def process_one(item_id: int) -> dict:
        # This runs on Ray workers
        result = heavy_computation(item_id)
        return {"id": item_id, "result": result}
    
    # Automatically parallelized
    return parallel_map(process_one, item_ids)
```

## See Also

- [Queues](queues.md) - Queue configuration and priorities
- [Retry & Error Handling](retry.md) - Configuring retries
- [Worker Modes](worker-modes.md) - Execution modes

