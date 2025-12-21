"""Unit tests for serialization utilities."""

from __future__ import annotations

import pytest

from django_ray.runtime.serialization import (
    SerializationError,
    deserialize_args,
    serialize_args,
)


class TestSerializeArgs:
    """Tests for serialize_args function."""

    def test_serialize_simple_types(self) -> None:
        """Test serializing simple types."""
        assert serialize_args([1, 2, 3]) == "[1, 2, 3]"
        assert serialize_args({"key": "value"}) == '{"key": "value"}'
        assert serialize_args("string") == '"string"'
        assert serialize_args(42) == "42"
        assert serialize_args(None) == "null"

    def test_serialize_nested_structure(self) -> None:
        """Test serializing nested structures."""
        data = {"list": [1, 2], "nested": {"a": 1}}
        result = deserialize_args(serialize_args(data))
        assert result == data

    def test_serialize_non_serializable(self) -> None:
        """Test that non-serializable objects raise error."""
        with pytest.raises(SerializationError, match="Cannot serialize"):
            serialize_args(lambda x: x)


class TestDeserializeArgs:
    """Tests for deserialize_args function."""

    def test_deserialize_valid_json(self) -> None:
        """Test deserializing valid JSON."""
        assert deserialize_args("[1, 2, 3]") == [1, 2, 3]
        assert deserialize_args('{"key": "value"}') == {"key": "value"}

    def test_deserialize_invalid_json(self) -> None:
        """Test that invalid JSON raises error."""
        with pytest.raises(SerializationError, match="Cannot deserialize"):
            deserialize_args("not valid json")
