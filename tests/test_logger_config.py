import pytest
from loguru import logger

from src.auto_coder.logger_config import log_calls
from src.auto_coder.security_utils import redact_string


@log_calls
def sensitive_function(api_key: str):
    return "processed"


def test_log_calls_redaction(caplog):
    """Test that @log_calls decorator redacts sensitive arguments."""
    # Pattern matching a sensitive key (from security_utils)
    secret = "sk-" + "a" * 48

    # We need to capture loguru logs. Loguru bypasses standard logging, so we need a sink.
    messages = []

    def sink(message):
        messages.append(message.record["message"])

    logger.remove()
    logger.add(sink, level="DEBUG")

    sensitive_function(secret)

    # Verify the secret is NOT in the logs
    for msg in messages:
        assert secret not in msg, f"Secret leaked in log: {msg}"
        if "sensitive_function" in msg and "CALL" in msg:
            assert "[REDACTED]" in msg, f"Redaction marker missing in log: {msg}"

    # Clean up logger (though pytest usually handles this, loguru is global)
    logger.remove()
