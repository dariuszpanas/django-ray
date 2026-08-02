"""Cluster Tasks App - Demonstrates remote Ray cluster execution.

This app shows how to use django-ray with a remote Ray cluster for:
- Trusted local Kubernetes evaluation and maintainer validation
- Multi-node distributed execution
- Large-scale task processing

Usage:
    # Connect to Ray cluster
    python manage.py django_ray_worker --cluster ray://ray-head:10001 --queue=default

    # Or set RAY_ADDRESS environment variable
    export RAY_ADDRESS=ray://ray-head:10001
    python manage.py django_ray_worker --queue=default

Kubernetes evaluation:
    Read docs/deployment/kubernetes.md before using the k8s/ manifests. The
    checked-in topology is not a production-ready deployment.
"""
