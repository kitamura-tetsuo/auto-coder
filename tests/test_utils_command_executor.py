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


def test_run_command_env_overrides(monkeypatch):
    """CommandExecutor should apply env overrides without mutating global os.environ."""
    monkeypatch.delenv("FAKE_PROVIDER_TOKEN", raising=False)

    script = [
        sys.executable,
        "-c",
        "import os; print(os.getenv('FAKE_PROVIDER_TOKEN', 'missing'))",
    ]

    result = utils.CommandExecutor.run_command(script, stream_output=False, env_overrides={"FAKE_PROVIDER_TOKEN": "scoped"})

    assert result.stdout.strip() == "scoped"
    assert "FAKE_PROVIDER_TOKEN" not in os.environ
