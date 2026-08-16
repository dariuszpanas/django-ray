"""Compatibility exports for :mod:`django_ray.target.probe`."""

from django_ray.target import probe as _implementation

__all__ = _implementation.__all__

globals().update({name: getattr(_implementation, name) for name in __all__})
