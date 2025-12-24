import os

import pytest
from loguru import logger

from auto_coder.utils import CommandExecutor


def test_command_executor_leaks_secret():
    # We use a pattern that we know should be redacted
    # "ghp_" is a standard GitHub token prefix
    secret = "ghp_1234567890abcdef1234567890abcdef123456"
    cmd = ["echo", secret]

    logs = []
    # Capture logs
    handler_id = logger.add(lambda msg: logs.append(msg))

    try:
        # Override environment to force verbose logging
        old_verbose = os.environ.get("AUTOCODER_VERBOSE")
        os.environ["AUTOCODER_VERBOSE"] = "1"

        CommandExecutor.run_command(cmd)

    finally:
        logger.remove(handler_id)
        if old_verbose is None:
            del os.environ["AUTOCODER_VERBOSE"]
        else:
            os.environ["AUTOCODER_VERBOSE"] = old_verbose

    # Check if secret is in logs
    found = False
    for log in logs:
        if secret in log:
            found = True
            break

    assert not found, "Secret should be redacted in logs"

    # Verify [REDACTED] is present
    redacted_found = False
    for log in logs:
        if "[REDACTED]" in log:
            redacted_found = True
            break

    assert redacted_found, "Log should contain [REDACTED] placeholder"
