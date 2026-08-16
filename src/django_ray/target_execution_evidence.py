"""Compatibility exports for :mod:`django_ray.target.execution_evidence`."""

from django_ray.target import execution_evidence as _implementation

__all__ = _implementation.__all__

globals().update({name: getattr(_implementation, name) for name in __all__})
