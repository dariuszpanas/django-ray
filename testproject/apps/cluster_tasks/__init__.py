"""Cluster Tasks App - Demonstrates remote Ray cluster execution.

This app shows how to use django-ray with a remote Ray cluster for:
- Production deployments on Kubernetes
- Multi-node distributed execution
- Large-scale task processing

Usage:
    # Connect to Ray cluster
    python manage.py django_ray_worker --cluster ray://ray-head:10001 --queue=default

    # Or set RAY_ADDRESS environment variable
    export RAY_ADDRESS=ray://ray-head:10001
    python manage.py django_ray_worker --queue=default

Kubernetes deployment:
    See k8s/ directory for deployment manifests.
"""
