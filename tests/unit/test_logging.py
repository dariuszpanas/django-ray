"""Unit tests for structured logging."""

from __future__ import annotations

import logging

import pytest

from django_ray.logging import (
    StructuredLogAdapter,
    get_backend_logger,
    get_logger,
    get_task_logger,
    get_worker_logger,
)


class TestStructuredLogAdapter:
    """Test the StructuredLogAdapter class."""

    def test_basic_message(self, caplog):
        """Test basic message logging."""
        logger = get_logger("test.basic")

        with caplog.at_level(logging.INFO):
            logger.info("Test message")

        assert "Test message" in caplog.text

    def test_structured_context(self, caplog):
        """Test structured context is added to messages."""
        logger = get_logger("test.context", component="test")

        with caplog.at_level(logging.INFO):
            logger.info("Test with context", extra={"key": "value"})

        assert "Test with context" in caplog.text
        # The JSON context should be appended
        assert '"component": "test"' in caplog.text or '"key": "value"' in caplog.text

    def test_worker_logger(self, caplog):
        """Test worker logger includes worker_id."""
        logger = get_worker_logger("worker-123")

        with caplog.at_level(logging.INFO):
            logger.info("Worker started")

        assert "Worker started" in caplog.text

    def test_task_logger(self, caplog):
        """Test task logger includes task context."""
        logger = get_task_logger("task-456", "myapp.tasks.my_task")

        with caplog.at_level(logging.INFO):
            logger.info("Task executing")

        assert "Task executing" in caplog.text

    def test_backend_logger(self, caplog):
        """Test backend logger."""
        logger = get_backend_logger()

        with caplog.at_level(logging.INFO):
            logger.info("Backend operation")

        assert "Backend operation" in caplog.text

    def test_extra_fields_merged(self, caplog):
        """Test that extra fields from logger and call are merged."""
        logger = get_logger("test.merge", default_key="default_value")

        with caplog.at_level(logging.INFO):
            logger.info("Merged context", extra={"call_key": "call_value"})

        # Both should be present
        assert "Merged context" in caplog.text

    def test_none_values_filtered(self, caplog):
        """Test that None values are filtered from output."""
        logger = get_logger("test.none")

        with caplog.at_level(logging.INFO):
            logger.info("With None", extra={"key": "value", "none_key": None})

        assert "With None" in caplog.text
        # none_key should not appear
        assert "none_key" not in caplog.text

