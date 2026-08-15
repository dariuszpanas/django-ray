"""Unit tests for structured logging."""

from __future__ import annotations

import logging

import django_ray.logging as logging_module
from django_ray.logging import (
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

    def test_disabled_log_level_does_not_evaluate_message_text(self):
        class RaisingMessage:
            calls = 0

            def __str__(self) -> str:
                self.calls += 1
                raise AssertionError("disabled logging must remain lazy")

        logger = get_logger("test.disabled-lazy-formatting")
        logger.logger.setLevel(logging.INFO)
        message = RaisingMessage()

        logger.debug(message)

        assert message.calls == 0

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

    def test_json_serialization_failure_uses_fallback(self, monkeypatch):
        """Unserializable structured context should still produce a log message."""
        logger = get_logger("test.serialization")

        def fail_json(*args, **kwargs):
            raise TypeError("cannot serialize")

        monkeypatch.setattr(logging_module.json, "dumps", fail_json)

        message, kwargs = logger.process("Fallback message", {"extra": {"key": "value"}})

        assert message == "Fallback message | {'key': 'value'}"
        assert kwargs["extra"] == {"key": "value"}

    def test_redacts_nested_extra_and_format_arguments(self, caplog, settings):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"customer[_-]?email", r"api[_-]?key"]}
        logger = get_logger("test.redaction")

        with caplog.at_level(logging.INFO):
            logger.info(
                "Task metadata",
                extra={
                    "payload": {
                        "customer_email": "ada@example.test",
                        "safe": "visible",
                    }
                },
            )
            logger.info("credential=%s", "api-key=secret")

        assert "ada@example.test" not in caplog.text
        assert "api-key=secret" not in caplog.text
        assert "[REDACTED]" in caplog.text

    def test_exception_logs_keep_redacted_description_without_traceback(self, caplog, settings):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"access-token"]}
        logger = get_logger("test.redacted-exception")

        with caplog.at_level(logging.ERROR):
            try:
                raise RuntimeError("access-token=secret-value")
            except RuntimeError:
                logger.exception("Task failed")

        assert "secret-value" not in caplog.text
        assert "RuntimeError" not in caplog.text
        assert "[REDACTED]" in caplog.text
        assert "Traceback" not in caplog.text

    def test_exception_type_name_uses_the_same_redaction_boundary(self, caplog, settings):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"TenantCanaryError"]}
        error_type = type("TenantCanaryError", (RuntimeError,), {})
        logger = get_logger("test.redacted-exception-type")

        with caplog.at_level(logging.ERROR):
            try:
                raise error_type("ordinary provider failure")
            except error_type:
                logger.exception("Task failed")

        assert "TenantCanaryError" not in caplog.text
        assert "ordinary provider failure" not in caplog.text
        assert "[REDACTED]" in caplog.text

    def test_exception_pattern_can_span_type_and_message(self, caplog, settings):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"BoundaryCanaryError: provider marker"]}
        error_type = type("BoundaryCanaryError", (RuntimeError,), {})
        logger = get_logger("test.redacted-exception-boundary")

        with caplog.at_level(logging.ERROR):
            try:
                raise error_type("provider marker")
            except error_type:
                logger.exception("Task failed")

        assert "BoundaryCanaryError" not in caplog.text
        assert "provider marker" not in caplog.text
        assert "[REDACTED]" in caplog.text

    def test_exception_logging_survives_an_unrenderable_message(self, caplog):
        calls = 0

        class BrokenError(RuntimeError):
            def __str__(self) -> str:
                nonlocal calls
                calls += 1
                raise RuntimeError("secondary password=do-not-expose")

        logger = get_logger("test.unrenderable-exception")
        with caplog.at_level(logging.ERROR):
            try:
                raise BrokenError()
            except BrokenError:
                logger.exception("Cleanup failed")

        assert calls == 1
        assert "Cleanup failed" in caplog.text
        assert "BrokenError: exception message unavailable" in caplog.text
        assert "secondary password" not in caplog.text
        assert "Traceback" not in caplog.text

    def test_terminal_formatting_is_removed_without_dropping_format_arguments(self, caplog):
        logger = get_logger("test.terminal-formatting")

        with caplog.at_level(logging.ERROR):
            logger.error("\x1b[31mTask %s failed\x1b[39m\rnext line", 42)

        assert "Task 42 failed\nnext line" in caplog.text
        assert "\x1b" not in caplog.text

    def test_removed_terminal_sequences_consume_only_their_format_arguments(self, caplog):
        logger = get_logger("test.terminal-placeholder-formatting")

        with caplog.at_level(logging.ERROR):
            logger.error(
                "\x1b]hidden=%s\x1b\\Visible %s and \x1b[%smcolored %s\x1b[0m",
                "suppressed-field",
                "first-field",
                31,
                "second-field",
            )
            logger.error("malformed \x1b]field=%s", "retained-field")

        assert "Visible first-field and colored second-field" in caplog.text
        assert "suppressed-field" not in caplog.text
        assert "malformed field=retained-field" in caplog.text
        assert "\x1b" not in caplog.text

    def test_mapping_placeholders_and_mismatches_remain_logging_safe(self, caplog):
        logger = get_logger("test.mapping-placeholder-formatting")

        with caplog.at_level(logging.INFO):
            logger.info("Task %(task_id)s", {"task_id": "task-42"})
            logger.info("Mismatched %s %s", "only-one")

        assert "Task task-42" in caplog.text
        assert "Mismatched %s %s" in caplog.text

    def test_format_arguments_share_one_redaction_budget(self, monkeypatch):
        calls: list[object] = []

        def redact_once(value):
            calls.append(value)
            return ["first", "second"]

        monkeypatch.setattr(logging_module, "redact_value", redact_once)

        rendered = logging_module._format_redacted_message("%s %s", ("one", "two"))

        assert rendered == "first second"
        assert calls == [("one", "two")]

    def test_adapter_and_call_extra_share_one_redaction_budget(self, monkeypatch):
        import django_ray.redaction as redaction

        monkeypatch.setattr(redaction, "_REDACTION_VALUE_MAX_ITEMS", 6)
        logger = get_logger("test.aggregate-extra", adapter_value="visible")

        message, processed = logger.process(
            "Aggregate extra",
            {"extra": {"first": "one", "second": "two"}},
        )

        assert message == 'Aggregate extra | {"diagnostic": "[REDACTED]"}'
        assert processed["extra"] == {"diagnostic": "[REDACTED]"}

    def test_percent_characters_in_structured_context_do_not_reenter_interpolation(self, caplog):
        logger = get_logger("test.structured-percent")

        with caplog.at_level(logging.INFO):
            logger.info("Progress %s", "50%", extra={"reported": "75%"})

        assert "Progress 50%" in caplog.text
        assert '"reported": "75%"' in caplog.text

    def test_sensitive_terminal_formatted_extra_keys_use_a_fixed_marker(
        self,
        caplog,
        settings,
    ):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"customer_email"]}
        logger = get_logger("test.terminal-extra-keys")
        extra = {
            "pass\x1b]word": "default-key-value",
            "customer\x1b^_email": "custom-key-value",
            "safe\x1b[31m_key": "visible",
        }

        message, processed = logger.process("Structured keys", {"extra": extra})

        assert isinstance(message, str)
        assert processed["extra"] == {
            "<redacted>": "[REDACTED]",
            "safe_key": "visible",
        }
        assert "default-key-value" not in message
        assert "custom-key-value" not in message
        assert "password" not in message
        assert "customer_email" not in message
        assert "\x1b" not in message

        with caplog.at_level(logging.INFO):
            logger.info("Structured keys", extra=extra)

        assert "default-key-value" not in caplog.text
        assert "custom-key-value" not in caplog.text
        assert "password" not in caplog.text
        assert "customer_email" not in caplog.text
        assert "\x1b" not in caplog.text
