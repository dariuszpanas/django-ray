# Working with Queues

Queues allow you to organize and prioritize tasks. Different queues can be processed by different workers with different concurrency settings.

## Defining Queues

Queues are defined in your Django settings:

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "django_ray.backends.RayTaskBackend",
        "QUEUES": [
            "default",
            "high-priority",
            "low-priority",
            "email",
            "reports",
        ],
    },
}
```

## Assigning Tasks to Queues

### In Task Definition

```python
@task(queue_name="email")
def send_email(to: str, subject: str, body: str):
    pass

@task(queue_name="reports")
def generate_report(report_id: int):
    pass

@task(queue_name="high-priority")
def urgent_notification(user_id: int, message: str):
    pass
```

### At Enqueue Time

```python
# Override the default queue
send_email.using(queue_name="high-priority").enqueue(
    to="vip@example.com",
    subject="Urgent",
    body="Important!"
)
```

## Running Workers for Specific Queues

### Single Queue

```bash
# Process only the email queue
python manage.py django_ray_worker --queue=email --local
```

### Multiple Queues

```bash
# Process multiple queues
python manage.py django_ray_worker --queue=default,high-priority --local
```

### All Queues

```bash
# Process all configured queues
python manage.py django_ray_worker --all-queues --local
```

## Queue Priorities

Tasks with higher priority values are processed first within a queue:

```python
# Higher priority (processed first)
urgent_task.using(priority=10).enqueue(data="urgent")

# Normal priority
normal_task.using(priority=0).enqueue(data="normal")

# Lower priority (processed last)
background_task.using(priority=-10).enqueue(data="background")
```

## Worker Deployment Patterns

### Dedicated Workers per Queue

```yaml
# High-priority worker (fast, low concurrency)
- name: worker-high-priority
  command: python manage.py django_ray_worker --queue=high-priority --local
  concurrency: 5

# Default worker (normal workload)
- name: worker-default
  command: python manage.py django_ray_worker --queue=default --local
  concurrency: 20

# Background worker (slow tasks, high concurrency)
- name: worker-background
  command: python manage.py django_ray_worker --queue=low-priority,reports --local
  concurrency: 50
```

### Shared Workers

```bash
# Single worker processing all queues
python manage.py django_ray_worker --all-queues --local
```

## Queue Isolation

For workload isolation, use separate queues:

```python
# CPU-intensive tasks
@task(queue_name="compute")
def heavy_computation(data: dict):
    pass

# I/O-bound tasks
@task(queue_name="io")
def fetch_external_data(url: str):
    pass

# Quick tasks
@task(queue_name="quick")
def send_notification(user_id: int):
    pass
```

Then run workers with appropriate resources:

```bash
# High-CPU worker for compute queue
python manage.py django_ray_worker --queue=compute --local

# High-concurrency worker for I/O queue
python manage.py django_ray_worker --queue=io --local --concurrency=100

# Quick tasks worker
python manage.py django_ray_worker --queue=quick --local --concurrency=50
```

## Monitoring Queue Depth

### Via API

```python
from django_ray.models import RayTaskExecution, TaskState

# Get queue depths
queues = (
    RayTaskExecution.objects
    .filter(state=TaskState.QUEUED)
    .values('queue_name')
    .annotate(count=Count('id'))
)
for q in queues:
    print(f"{q['queue_name']}: {q['count']} pending")
```

### Via Prometheus Metrics

The `/api/metrics` endpoint exposes queue depth:

```
django_ray_queue_depth{queue="default"} 42
django_ray_queue_depth{queue="email"} 10
django_ray_queue_depth{queue="reports"} 5
```

## Best Practices

### 1. Separate by Latency Requirements

```python
# Fast response needed
@task(queue_name="realtime")
def process_webhook(payload: dict):
    pass

# Can wait
@task(queue_name="batch")
def generate_monthly_report(month: int, year: int):
    pass
```

### 2. Separate by Resource Needs

```python
# Memory-intensive
@task(queue_name="memory-heavy")
def process_large_dataset(dataset_id: int):
    pass

# CPU-intensive
@task(queue_name="cpu-heavy")
def train_model(model_id: int):
    pass
```

### 3. Use Priorities Within Queues

```python
# Same queue, different priorities
@task(queue_name="notifications")
def send_sms(phone: str, message: str):
    pass

# Urgent SMS
send_sms.using(priority=10).enqueue(phone="+1234567890", message="Alert!")

# Normal SMS
send_sms.enqueue(phone="+1234567890", message="Hello")
```

## See Also

- [Getting Started](getting-started.md) - Basic setup
- [Worker Modes](worker-modes.md) - Execution modes
- [Configuration](configuration.md) - All settings

