# Getting Started

This guide will help you integrate django-ray into your Django project.

## Requirements

- Python 3.12 or 3.13
- Django 6.0+
- Ray 2.53.0+
- PostgreSQL (recommended) or SQLite

## Installation

Install django-ray using pip:

```bash
pip install django-ray
```

Or with uv:

```bash
uv add django-ray
```

For PostgreSQL support:

```bash
pip install django-ray[postgres]
```

## Quick Setup

### 1. Add to INSTALLED_APPS

```python
# settings.py
INSTALLED_APPS = [
    # Django apps...
    "django.contrib.admin",
    "django.contrib.auth",
    # ...
    
    # Add django-ray
    "django_ray",
    
    # Your apps...
]
```

### 2. Configure Settings

Add the django-ray configuration to your settings:

```python
# settings.py
DJANGO_RAY = {
    "RAY_ADDRESS": "auto",  # or "ray://localhost:10001" for cluster
    "DEFAULT_CONCURRENCY": 10,
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 60,
    # Exceptions that won't trigger auto-retry
    "RETRY_EXCEPTION_DENYLIST": [
        "myapp.exceptions.PermanentError",
    ],
}

# Optional: Ray Dashboard URL for admin links
RAY_DASHBOARD_URL = "http://localhost:8265"
```

### 3. Run Migrations

```bash
python manage.py migrate django_ray
```

### 4. Define a Task

Create a task using Django's `@task` decorator:

```python
# myapp/tasks.py
from django.tasks import task

@task(queue_name="default")
def send_welcome_email(user_id: int) -> dict:
    """Send welcome email to a new user."""
    from myapp.models import User
    from myapp.email import send_email
    
    user = User.objects.get(id=user_id)
    send_email(
        to=user.email,
        subject="Welcome!",
        body=f"Hello {user.name}, welcome to our platform!"
    )
    return {"sent_to": user.email}
```

### 5. Enqueue a Task

```python
# In your view or anywhere in your code
from myapp.tasks import send_welcome_email

# Enqueue the task
result = send_welcome_email.enqueue(user_id=123)

# Get the task ID for tracking
print(f"Task ID: {result.id}")
```

### 6. Start the Worker

In a separate terminal, start the worker:

```bash
# Local Ray (recommended for development)
python manage.py django_ray_worker --queue=default --local

# Or sync mode for testing (no Ray required)
python manage.py django_ray_worker --queue=default --sync
```

## Verifying the Setup

### Check Task Status

You can check task status via the Django admin or programmatically:

```python
from django_ray.models import RayTaskExecution

# Get all executions
executions = RayTaskExecution.objects.all()

# Filter by state
succeeded = RayTaskExecution.objects.filter(state="SUCCEEDED")
failed = RayTaskExecution.objects.filter(state="FAILED")
```

### Django Admin

Access the Django admin at `/admin/django_ray/` to:

- View all task executions
- Monitor task states
- Filter by state, queue, or date
- View error messages and tracebacks for failed tasks
- Retry failed tasks (select tasks and use "Retry selected tasks" action)
- Cancel queued or running tasks (select tasks and use "Cancel selected tasks" action)

## Next Steps

- [Configuration](configuration.md) - Learn about all configuration options
- [Worker Modes](worker-modes.md) - Understand different execution modes
- [Task Definition](tasks.md) - Advanced task patterns
- [Deployment](deployment/kubernetes.md) - Deploy to production

