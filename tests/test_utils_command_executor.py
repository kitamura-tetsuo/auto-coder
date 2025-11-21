import os
import sys
from types import SimpleNamespace

import pytest

from src.auto_coder import utils


def test_run_command_respects_stream_flag(monkeypatch):
    calls = {}

    monkeypatch.delenv("AUTOCODER_STREAM_COMMANDS", raising=False)

    def fake_run(cmd, capture_output, text, timeout, cwd, env=None):
        calls["called"] = True
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fail_popen(*_args, **_kwargs):
        pytest.fail("Popen should not be used when stream_output=False")

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    monkeypatch.setattr(utils.subprocess, "Popen", fail_popen)

    result = utils.CommandExecutor.run_command(["echo", "hi"], stream_output=False)

    assert result.success is True
    assert result.stdout == "ok"
    assert calls.get("called") is True


def test_run_command_streams_output(monkeypatch):
    # Ensure subprocess.run is not used in streaming mode
    def fail_run(*_args, **_kwargs):
        pytest.fail("subprocess.run should not be used when stream_output=True")

    monkeypatch.delenv("AUTOCODER_STREAM_COMMANDS", raising=False)
    monkeypatch.setattr(utils.subprocess, "run", fail_run)

    command = [
        sys.executable,
        "-c",
        "import sys; print('STDOUT'); print('STDERR', file=sys.stderr)",
    ]

    result = utils.CommandExecutor.run_command(command, timeout=5, stream_output=True)

    assert result.returncode == 0
    assert result.stdout == "STDOUT\n"
    assert result.stderr == "STDERR\n"


def test_should_stream_when_debugger_attached(monkeypatch):
    monkeypatch.delenv("AUTOCODER_STREAM_COMMANDS", raising=False)
    monkeypatch.setattr(sys, "gettrace", lambda: object())
    assert utils.CommandExecutor._should_stream_output(None) is True


def test_should_stream_when_env_forced(monkeypatch):
    monkeypatch.delenv("AUTOCODER_STREAM_COMMANDS", raising=False)
    monkeypatch.setattr(sys, "gettrace", lambda: None)
    assert utils.CommandExecutor._should_stream_output(None) is False

    monkeypatch.setenv("AUTOCODER_STREAM_COMMANDS", "1")
    assert utils.CommandExecutor._should_stream_output(None) is True


@pytest.mark.parametrize("marker", utils.CommandExecutor.DEBUGGER_ENV_MARKERS)
def test_should_stream_for_debugger_markers(monkeypatch, marker):
    monkeypatch.delenv("AUTOCODER_STREAM_COMMANDS", raising=False)
    monkeypatch.setattr(sys, "gettrace", lambda: None)
    for env_key in utils.CommandExecutor.DEBUGGER_ENV_MARKERS:
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setenv(marker, "1")
    assert utils.CommandExecutor._should_stream_output(None) is True


def test_is_running_in_debugger_false(monkeypatch):
    """Test is_running_in_debugger returns False when no debugger is detected."""
    monkeypatch.setattr(sys, "gettrace", lambda: None)
    for env_key in utils.CommandExecutor.DEBUGGER_ENV_MARKERS:
        monkeypatch.delenv(env_key, raising=False)

    assert utils.CommandExecutor.is_running_in_debugger() is False


def test_is_running_in_debugger_true_gettrace(monkeypatch):
    """Test is_running_in_debugger returns True when sys.gettrace is set."""
    monkeypatch.setattr(sys, "gettrace", lambda: object())
    for env_key in utils.CommandExecutor.DEBUGGER_ENV_MARKERS:
        monkeypatch.delenv(env_key, raising=False)

    assert utils.CommandExecutor.is_running_in_debugger() is True


@pytest.mark.parametrize("marker", utils.CommandExecutor.DEBUGGER_ENV_MARKERS)
def test_is_running_in_debugger_true_env_markers(monkeypatch, marker):
    """Test is_running_in_debugger returns True when debugger env markers are set."""
    monkeypatch.setattr(sys, "gettrace", lambda: None)
    for env_key in utils.CommandExecutor.DEBUGGER_ENV_MARKERS:
        monkeypatch.delenv(env_key, raising=False)

    monkeypatch.setenv(marker, "1")
    assert utils.CommandExecutor.is_running_in_debugger() is True


def test_transient_env_sets_and_cleans_up(monkeypatch):
    """Test that TransientEnv sets and cleans up environment variables."""
    monkeypatch.delenv("TEST_VAR", raising=False)

    with utils.TransientEnv({"TEST_VAR": "test_value"}):
        assert os.environ.get("TEST_VAR") == "test_value"

    # Variable should be cleaned up
    assert os.environ.get("TEST_VAR") is None


def test_transient_env_restores_existing_value(monkeypatch):
    """Test that TransientEnv restores existing environment variable values."""
    monkeypatch.setenv("TEST_VAR", "original_value")

    with utils.TransientEnv({"TEST_VAR": "new_value"}):
        assert os.environ.get("TEST_VAR") == "new_value"

    # Variable should be restored to original value
    assert os.environ.get("TEST_VAR") == "original_value"

    # Clean up
    monkeypatch.delenv("TEST_VAR", raising=False)


def test_transient_env_with_multiple_vars(monkeypatch):
    """Test TransientEnv with multiple environment variables."""
    monkeypatch.delenv("VAR1", raising=False)
    monkeypatch.delenv("VAR2", raising=False)
    monkeypatch.setenv("VAR3", "original")

    with utils.TransientEnv({"VAR1": "value1", "VAR2": "value2", "VAR3": "value3"}):
        assert os.environ.get("VAR1") == "value1"
        assert os.environ.get("VAR2") == "value2"
        assert os.environ.get("VAR3") == "value3"

    # New variables should be cleaned up
    assert os.environ.get("VAR1") is None
    assert os.environ.get("VAR2") is None

    # Existing variable should be restored
    assert os.environ.get("VAR3") == "original"

    # Clean up
    monkeypatch.delenv("VAR3", raising=False)


def test_transient_env_cleanup_on_exception(monkeypatch):
    """Test that TransientEnv cleans up even when an exception occurs."""
    monkeypatch.delenv("TEST_VAR", raising=False)

    try:
        with utils.TransientEnv({"TEST_VAR": "test_value"}):
            assert os.environ.get("TEST_VAR") == "test_value"
            raise RuntimeError("Test exception")
    except RuntimeError:
        pass

    # Variable should still be cleaned up despite the exception
    assert os.environ.get("TEST_VAR") is None


def test_transient_env_nested(monkeypatch):
    """Test nested TransientEnv contexts."""
    monkeypatch.delenv("VAR1", raising=False)
    monkeypatch.delenv("VAR2", raising=False)

    with utils.TransientEnv({"VAR1": "value1"}):
        assert os.environ.get("VAR1") == "value1"

        with utils.TransientEnv({"VAR1": "value2", "VAR2": "value2"}):
            assert os.environ.get("VAR1") == "value2"
            assert os.environ.get("VAR2") == "value2"

        # After inner context exits, VAR1 should be restored to value1, VAR2 should be gone
        assert os.environ.get("VAR1") == "value1"
        assert os.environ.get("VAR2") is None

    # After outer context exits, both should be gone
    assert os.environ.get("VAR1") is None
    assert os.environ.get("VAR2") is None
