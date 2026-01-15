"""Tests for logger redaction functionality."""

from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from src.auto_coder.logger_config import get_logger, log_calls, setup_logger


class TestLoggerRedaction:
    """Test cases for logger redaction."""

    def setup_method(self):
        """Setup for each test method."""
        logger.remove()
        logger.configure(patcher=None)

    def teardown_method(self):
        """Cleanup after each test method."""
        logger.remove()

    def test_log_calls_redacts_sensitive_args(self, caplog):
        """Test that @log_calls redacts sensitive arguments."""

        # We need to capture loguru output.
        # Since loguru bypasses standard logging handlers which caplog uses,
        # we need to add a sink that captures the logs for inspection.
        captured_logs = []

        def sink(message):
            captured_logs.append(message.record)

        logger.add(sink, level="DEBUG")

        @log_calls
        def process_token(token):
            return "processed"

        secret = "ghp_1234567890abcdef1234567890abcdef12"
        process_token(secret)

        # Check logs
        assert len(captured_logs) >= 2
        call_log = next(r for r in captured_logs if "CALL" in r["message"])

        assert "[REDACTED]" in call_log["message"]
        assert secret not in call_log["message"]

    def test_log_calls_redacts_sensitive_return_value(self, caplog):
        """Test that @log_calls redacts sensitive return values."""

        captured_logs = []

        def sink(message):
            captured_logs.append(message.record)

        logger.add(sink, level="DEBUG")

        secret = "ghp_1234567890abcdef1234567890abcdef12"

        @log_calls
        def return_token():
            return secret

        return_token()

        # Check logs
        assert len(captured_logs) >= 2
        ret_log = next(r for r in captured_logs if "RET" in r["message"])

        assert "[REDACTED]" in ret_log["message"]
        assert secret not in ret_log["message"]
