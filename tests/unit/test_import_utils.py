"""Unit tests for import utilities."""

from __future__ import annotations

import pytest

from django_ray.runtime.import_utils import import_callable


class TestImportCallable:
    """Tests for import_callable function."""

    def test_import_valid_callable(self) -> None:
        """Test importing a valid callable."""
        # Import a stdlib function
        result = import_callable("json.dumps")
        import json

        assert result is json.dumps

    def test_import_invalid_path_no_dot(self) -> None:
        """Test that paths without dots raise ImportError."""
        with pytest.raises(ImportError, match="must contain at least one dot"):
            import_callable("nodot")

    def test_import_nonexistent_module(self) -> None:
        """Test that nonexistent modules raise ImportError."""
        with pytest.raises(ImportError, match="Could not import module"):
            import_callable("nonexistent.module.func")

    def test_import_nonexistent_attribute(self) -> None:
        """Test that nonexistent attributes raise AttributeError."""
        with pytest.raises(AttributeError, match="has no attribute"):
            import_callable("json.nonexistent_function")

    def test_import_non_callable(self) -> None:
        """Test that non-callable imports raise TypeError."""
        with pytest.raises(TypeError, match="is not callable"):
            import_callable("json.__name__")
