"""Regression tests for StructuredLogAdapter kwargs, exc_info, LogRecord safety, and propagation."""

from __future__ import annotations

import logging

import django_ray.logging as logging_module
from django_ray.logging import configure_default_logging, get_logger


class TestStructuredLoggingFixes:
    """Verify F-041 and F-094 structured logging fixes."""

    def test_documented_keyword_argument_api_works_without_type_error(self, caplog) -> None:
        logger = get_logger("test.kwarg-api", component="worker")

        with caplog.at_level(logging.INFO):
            logger.info("Starting", worker_id="abc123", task_id=42)

        assert "Starting |" in caplog.text
        assert '"component": "worker"' in caplog.text
        assert '"worker_id": "abc123"' in caplog.text
        assert '"task_id": 42' in caplog.text

    def test_keyword_arguments_merge_with_extra_and_redact_secrets(self, caplog, settings) -> None:
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"api[_-]?key"]}
        logger = get_logger("test.kwarg-redact", component="worker")

        with caplog.at_level(logging.INFO):
            logger.info(
                "Submitted",
                extra={"queue": "default"},
                token="api_key=super-secret",
                attempt=2,
            )

        assert "super-secret" not in caplog.text
        assert '"queue": "default"' in caplog.text
        assert '"attempt": 2' in caplog.text
        assert "[REDACTED]" in caplog.text

    def test_exc_info_exception_instance_is_not_dropped(self, caplog) -> None:
        logger = get_logger("test.exc-instance")
        err = ValueError("invalid payload")

        with caplog.at_level(logging.ERROR):
            logger.error("Task failed", exc_info=err)

        assert "Task failed" in caplog.text
        assert "ValueError: invalid payload" in caplog.text
        # Default redaction boundary suppresses raw multi-line tracebacks.
        assert "Traceback" not in caplog.text

    def test_preserve_exc_info_opt_in_retains_normalized_tuple_for_sentry(self, caplog) -> None:
        logger = get_logger("test.sentry-exc", preserve_exc_info=True)
        captured_err: RuntimeError | None = None
        try:
            raise RuntimeError("sentry visible failure")
        except RuntimeError as err:
            captured_err = err
            with caplog.at_level(logging.ERROR):
                logger.error("Task crashed", exc_info=err)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert isinstance(record.exc_info, tuple)
        assert record.exc_info[0] is RuntimeError
        assert record.exc_info[1] is captured_err
        assert "RuntimeError: sentry visible failure" in record.getMessage()

    def test_reserved_log_record_attributes_are_prefixed_without_key_error(self, caplog) -> None:
        logger = get_logger("test.reserved-attrs")

        with caplog.at_level(logging.INFO):
            logger.info(
                "Context with reserved names",
                name="custom-name",
                module="billing",
                filename="tasks.py",
                message="inner",
                extra={"lineno": 99, "args": "custom-args"},
            )

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.name == "test.reserved-attrs"
        assert record.__dict__["ray_name"] == "custom-name"
        assert record.__dict__["ray_module"] == "billing"
        assert record.__dict__["ray_filename"] == "tasks.py"
        assert record.__dict__["ray_message"] == "inner"
        assert record.__dict__["ray_lineno"] == 99
        assert record.__dict__["ray_args"] == "custom-args"
        assert '"name": "custom-name"' in caplog.text

    def test_configure_default_logging_does_not_hijack_root_handlers_or_disable_propagate(
        self,
    ) -> None:
        pkg_logger = logging.getLogger("django_ray")
        root_logger = logging.getLogger()

        orig_pkg_handlers = list(pkg_logger.handlers)
        orig_pkg_level = pkg_logger.level
        orig_pkg_propagate = pkg_logger.propagate
        orig_root_handlers = list(root_logger.handlers)

        try:
            # Case 1: Root logger already has a handler (e.g. Django LOGGING / Sentry / pytest).
            pkg_logger.handlers.clear()
            pkg_logger.propagate = True
            pkg_logger.setLevel(logging.NOTSET)
            sentinel_handler = logging.NullHandler()
            root_logger.handlers[:] = [sentinel_handler]

            configure_default_logging(logging.WARNING)
            assert pkg_logger.handlers == []
            assert pkg_logger.propagate is True
            assert pkg_logger.level == logging.WARNING

            # Case 2: Neither django_ray nor root has handlers (standalone worker).
            root_logger.handlers.clear()
            pkg_logger.handlers.clear()
            pkg_logger.propagate = True

            configure_default_logging(logging.INFO)
            assert len(pkg_logger.handlers) == 1
            assert isinstance(pkg_logger.handlers[0], logging.StreamHandler)
            assert pkg_logger.propagate is True

            # Case 3: Explicit propagate=False is honored when requested.
            configure_default_logging(logging.INFO, propagate=False)
            assert pkg_logger.propagate is False
        finally:
            pkg_logger.handlers[:] = orig_pkg_handlers
            pkg_logger.setLevel(orig_pkg_level)
            pkg_logger.propagate = orig_pkg_propagate
            root_logger.handlers[:] = orig_root_handlers

    def test_log_performs_single_message_redaction_pass(self, monkeypatch, caplog) -> None:
        calls: list[object] = []
        original_redact = logging_module.redact_text

        def counting_redact(value: object) -> str:
            calls.append(value)
            return original_redact(value)

        monkeypatch.setattr(logging_module, "redact_text", counting_redact)
        logger = get_logger("test.single-redact")

        with caplog.at_level(logging.INFO):
            logger.info("Processed %s items", 5, worker_id="w-1")

        assert calls == ["Processed 5 items"]
        assert "Processed 5 items" in caplog.text
