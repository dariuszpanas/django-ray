"""Compatibility exports for :mod:`django_ray.target.coordination`."""

from django_ray.target import coordination as _implementation

__all__ = _implementation.__all__

globals().update({name: getattr(_implementation, name) for name in __all__})
