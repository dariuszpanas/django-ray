"""Sync Tasks App - Demonstrates synchronous task execution.

This app shows how to use django-ray in sync mode for:
- Unit testing without Ray overhead
- Local development debugging
- CI/CD pipelines without Ray cluster

Usage:
    python manage.py django_ray_worker --sync --queue=sync

The sync mode executes tasks directly in the worker process,
making it easy to debug and test task logic.
"""
