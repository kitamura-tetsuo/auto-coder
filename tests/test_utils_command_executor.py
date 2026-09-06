import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.auto_coder import utils


def test_run_command_respects_stream_flag(monkeypatch):
    monkeypatch.delenv("AUTOCODER_STREAM_COMMANDS", raising=False)

    mock_streaming = MagicMock(return_value=(0, "ok", ""))
    monkeypatch.setattr(utils.CommandExecutor, "_run_with_streaming", mock_streaming)

    utils.CommandExecutor.run_command(["echo", "hi"], stream_output=False)

    mock_streaming.assert_called_once()
    _args, kwargs = mock_streaming.call_args
    # Verify log_output=False was passed
    assert kwargs.get("log_output") is False


def test_run_command_streams_output(monkeypatch):
    # Ensure subprocess.run is not used in streaming mode
    def fail_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        # Only fail if the command matches the one we're testing (which uses sys.executable)
        # This prevents background threads (running git) from failing the test
        if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == sys.executable:
            pytest.fail("subprocess.run should not be used when stream_output=True")

        # Return dummy result for background calls
        return SimpleNamespace(returncode=0, stdout="", stderr="")

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


def test_should_stream_when_debugger_attached(monkeypatch, _use_real_streaming_logic):
    monkeypatch.delenv("AUTOCODER_STREAM_COMMANDS", raising=False)
    monkeypatch.setattr(sys, "gettrace", lambda: object())
    assert utils.CommandExecutor._should_stream_output(None) is True


def test_should_stream_when_env_forced(monkeypatch, _use_real_streaming_logic):
    monkeypatch.delenv("AUTOCODER_STREAM_COMMANDS", raising=False)
    monkeypatch.setattr(sys, "gettrace", lambda: None)
    assert utils.CommandExecutor._should_stream_output(None) is False

    monkeypatch.setenv("AUTOCODER_STREAM_COMMANDS", "1")
    assert utils.CommandExecutor._should_stream_output(None) is True


@pytest.mark.parametrize("marker", utils.CommandExecutor.DEBUGGER_ENV_MARKERS)
def test_should_stream_for_debugger_markers(monkeypatch, marker, _use_real_streaming_logic):
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


def test_run_command_without_pty_has_no_tty():
    """Without use_pty the child process sees pipes, not a terminal."""
    script = [
        sys.executable,
        "-c",
        "import sys; print(sys.stdout.isatty())",
    ]

    result = utils.CommandExecutor.run_command(script, stream_output=False)

    assert result.returncode == 0
    assert result.stdout.strip() == "False"


def test_run_command_with_pty_provides_interactive_terminal():
    """use_pty must give the child process a real terminal on stdin/stdout/stderr."""
    script = [
        sys.executable,
        "-c",
        "import sys; print(sys.stdin.isatty(), sys.stdout.isatty(), sys.stderr.isatty())",
    ]

    result = utils.CommandExecutor.run_command(script, stream_output=False, use_pty=True)

    assert result.returncode == 0
    assert result.stdout.strip() == "True True True"


def test_run_command_with_pty_merges_stderr_into_stdout():
    """A pty exposes a single stream, so stderr output is captured as stdout."""
    script = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('boom\\n'); sys.stderr.flush()",
    ]

    result = utils.CommandExecutor.run_command(script, stream_output=False, use_pty=True)

    assert result.returncode == 0
    assert "boom" in result.stdout
    assert result.stderr == ""


def test_run_command_with_pty_reports_non_zero_exit():
    """Exit codes must survive the pty path."""
    script = [sys.executable, "-c", "raise SystemExit(3)"]

    result = utils.CommandExecutor.run_command(script, stream_output=False, use_pty=True)

    assert result.returncode == 3
    assert result.success is False


def test_run_command_with_pty_strips_ansi_sequences():
    """Escape sequences from interactive UIs must not pollute captured output."""
    script = [
        sys.executable,
        "-c",
        r"print('\x1b[31mred\x1b[0m done')",
    ]

    result = utils.CommandExecutor.run_command(script, stream_output=False, use_pty=True)

    assert result.stdout.strip() == "red done"


def test_strip_ansi_sequences_removes_csi_and_osc():
    text = "\x1b[2J\x1b[1;32mhello\x1b[0m\x1b]0;title\x07 world"

    assert utils.strip_ansi_sequences(text) == "hello world"


def test_strip_ansi_sequences_keeps_plain_text():
    assert utils.strip_ansi_sequences("plain text") == "plain text"
    assert utils.strip_ansi_sequences("") == ""


def test_run_command_auto_resolves_git_dubious_ownership(monkeypatch):
    """CommandExecutor should auto-configure safe.directory and retry when git detects dubious ownership."""
    streaming_calls = []

    def mock_streaming(*args, **kwargs):
        streaming_calls.append((args, kwargs))
        if len(streaming_calls) == 1:
            return (
                128,
                "",
                "fatal: detected dubious ownership in repository at '/test/repo'\nTo add an exception for this directory, call:\n\n\tgit config --global --add safe.directory /test/repo\n",
            )
        return (0, "main\n", "")

    subprocess_calls = []

    def mock_subprocess_run(cmd, *args, **kwargs):
        subprocess_calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(utils.CommandExecutor, "_run_with_streaming", mock_streaming)
    monkeypatch.setattr(utils.subprocess, "run", mock_subprocess_run)

    result = utils.CommandExecutor.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], stream_output=False)

    assert result.success is True
    assert result.returncode == 0
    assert result.stdout == "main\n"
    assert len(streaming_calls) == 2
    assert ["git", "config", "--global", "--add", "safe.directory", "/test/repo"] in subprocess_calls


def test_run_command_git_dubious_ownership_subprocess_failure(monkeypatch):
    """CommandExecutor should handle errors gracefully if configuring safe.directory raises."""
    mock_streaming = MagicMock(
        return_value=(
            128,
            "",
            "fatal: detected dubious ownership in repository at '/test/repo'\n",
        )
    )

    def mock_subprocess_run(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(utils.CommandExecutor, "_run_with_streaming", mock_streaming)
    monkeypatch.setattr(utils.subprocess, "run", mock_subprocess_run)

    result = utils.CommandExecutor.run_command(["git", "status"], stream_output=False)

    assert result.success is False
    assert result.returncode == 128
    assert "dubious ownership" in result.stderr
