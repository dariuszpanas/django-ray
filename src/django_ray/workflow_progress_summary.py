"""Compatibility exports for :mod:`django_ray.workflow.progress.summary`."""

from django_ray.workflow.progress import summary as _implementation

__all__ = _implementation.__all__

globals().update({name: getattr(_implementation, name) for name in __all__})
