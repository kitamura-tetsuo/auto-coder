import asyncio
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from src.auto_coder.logger_config import log_calls


def test_log_calls_redaction():
    # Define a function to decorate
    @log_calls
    def sensitive_function(token, password):
        return f"Result: {token} {password}"

    # Capture logs
    logs = []
    handler_id = logger.add(lambda msg: logs.append(msg), level="DEBUG")

    secret_token = "ghp_SECRETTOKEN123456789"
    secret_password = "sk-SECRETKEY1234567890123456789012345678901234567890123456"

    try:
        sensitive_function(secret_token, password=secret_password)
    finally:
        logger.remove(handler_id)

    # Combine all logs
    log_output = "".join(logs)

    # Check for secrets
    if secret_token in log_output or secret_password in log_output:
        pytest.fail("Secrets leaked in logs! Redaction missing in @log_calls")

    assert "[REDACTED]" in log_output, "Expected redaction placeholder"


def test_log_calls_redaction_async():
    # Define an async function to decorate
    @log_calls
    async def sensitive_async_function(token, password):
        return f"Result: {token} {password}"

    async def run_test():
        # Capture logs
        logs = []
        handler_id = logger.add(lambda msg: logs.append(msg), level="DEBUG")

        secret_token = "ghp_SECRETTOKEN123456789ASYNC"
        secret_password = "sk-SECRETKEY1234567890123456789012345678901234567890123456ASYNC"

        try:
            await sensitive_async_function(secret_token, password=secret_password)
        finally:
            logger.remove(handler_id)

        # Combine all logs
        log_output = "".join(logs)

        # Check for secrets
        if secret_token in log_output or secret_password in log_output:
            pytest.fail("Async secrets leaked in logs! Redaction missing in @log_calls")

        assert "[REDACTED]" in log_output, "Expected redaction placeholder"

    asyncio.run(run_test())
