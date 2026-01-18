"""Local Ray App - Demonstrates local Ray instance execution.

This app shows how to use django-ray with a local Ray instance for:
- Development with full Ray features
- CPU-intensive tasks that benefit from parallelism
- Testing Ray-specific behavior before cluster deployment

Usage:
    python manage.py django_ray_worker --local --queue=default

The local mode starts Ray automatically with a dashboard at
http://127.0.0.1:8265 for monitoring tasks.
"""

