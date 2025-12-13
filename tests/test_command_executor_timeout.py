import subprocess
import time

import pytest

from auto_coder.utils import CommandExecutor


def test_command_executor_idle_timeout():
    """Test that CommandExecutor kills a process that is idle for too long."""
    # Command that sleeps for 2 seconds, prints something, then sleeps for 2 seconds
    # We set idle_timeout to 1 second, so it should fail during the first sleep
    cmd = ["python3", "-c", "import time; time.sleep(2); print('hello'); time.sleep(2)"]

    start_time = time.time()
    result = CommandExecutor.run_command(cmd, idle_timeout=1, stream_output=True)
    duration = time.time() - start_time

    assert result.returncode == -1
    assert "Command timed out" in result.stderr
    # It should timeout after roughly 1 second
    assert 1.0 <= duration < 2.5


def test_command_executor_no_idle_timeout_if_active():
    """Test that CommandExecutor does NOT kill a process that outputs frequently enough."""
    # Command that prints every 0.5 seconds for 2 seconds
    # We set idle_timeout to 1 second, so it should NOT fail
    cmd = ["python3", "-c", "import time; \nfor i in range(4): \n    time.sleep(0.5); \n    print('hello', flush=True)"]

    start_time = time.time()
    result = CommandExecutor.run_command(cmd, idle_timeout=1, stream_output=True)
    duration = time.time() - start_time

    assert result.returncode == 0
    assert result.success is True
    # It should take at least 2 seconds
    assert duration >= 2.0
