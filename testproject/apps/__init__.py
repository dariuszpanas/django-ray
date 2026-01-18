"""Django apps demonstrating different django-ray execution modes.

Each app in this package demonstrates a different way to use django-ray
with Django 6's native task framework:

- sync_tasks: Synchronous execution for testing (no Ray)
- local_ray: Local Ray instance for development
- cluster_tasks: Remote Ray cluster for production
- ml_pipeline: Machine learning pattern with data processing

Example usage:
    # Sync mode (testing)
    python manage.py django_ray_worker --sync --queue=sync

    # Local Ray (development)
    python manage.py django_ray_worker --local --queue=default

    # Cluster mode (production)
    python manage.py django_ray_worker --cluster ray://head:10001 --queue=default
"""

