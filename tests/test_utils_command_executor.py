import hashlib
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.auto_coder import utils


def _stdin_digest_command():
    return [
        sys.executable,
        "-c",
        ("import hashlib,os,sys; data=sys.stdin.buffer.read(); " "print(sys.stdin.isatty()); print(len(data)); print(hashlib.sha256(data).hexdigest()); " "print(os.getcwd()); print(os.getenv('COMMAND_EXECUTOR_INPUT_TEST', 'missing'))"),
    ]


@pytest.mark.parametrize(
    "stdin_text",
    [
        "",
        " 日本語 😀 '@' \\\r\nno-final-newline ",
        "payload-" * (2 * 1024 * 1024 // len("payload-")),
    ],
    ids=["empty", "unicode-and-special-characters", "two-megabytes"],
)
def test_run_command_delivers_exact_finite_utf8_stdin(stdin_text, tmp_path):
    result = utils.CommandExecutor.run_command(
        _stdin_digest_command(),
        stdin_text=stdin_text,
        cwd=str(tmp_path),
        env_overrides={"COMMAND_EXECUTOR_INPUT_TEST": "isolated"},
        timeout=10,
        stream_output=False,
    )

    lines = result.stdout.splitlines()
    expected = stdin_text.encode("utf-8")
    assert result.success is True
    assert result.stderr == ""
    assert lines == [
        "False",
        str(len(expected)),
        hashlib.sha256(expected).hexdigest(),
        str(tmp_path),
        "isolated",
    ]


@pytest.mark.parametrize("stream_output", [True, False])
def test_run_command_drains_output_while_delivering_large_stdin(stream_output):
    output_size = 256 * 1024
    payload = "input-data-" * (2 * 1024 * 1024 // len("input-data-"))
    observations = []
    command = [
        sys.executable,
        "-c",
        (
            "import hashlib,sys,time; size=int(sys.argv[1]); "
            "sys.stdout.write('o'*size+'\\n'); sys.stdout.flush(); "
            "sys.stderr.write('e'*size+'\\n'); sys.stderr.flush(); "
            "data=bytearray(); "
            "\nwhile True:\n chunk=sys.stdin.buffer.read(4096)\n if not chunk: break\n data.extend(chunk)\n time.sleep(.0001)\n"
            "print(hashlib.sha256(data).hexdigest())"
        ),
        str(output_size),
    ]

    result = utils.CommandExecutor.run_command(
        command,
        stdin_text=payload,
        timeout=15,
        stream_output=stream_output,
        on_stream=lambda stream, chunk: observations.append((stream, chunk)),
    )

    assert result.success is True
    assert result.stdout.startswith("o" * output_size + "\n")
    assert result.stdout.endswith(hashlib.sha256(payload.encode()).hexdigest() + "\n")
    assert result.stderr == "e" * output_size + "\n"
    assert observations


@pytest.mark.parametrize("timeout_argument", ["timeout", "idle_timeout"])
def test_run_command_timeout_bounds_pending_input(timeout_argument):
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.monotonic()

    result = utils.CommandExecutor.run_command(
        command,
        stdin_text="x" * (2 * 1024 * 1024),
        stream_output=False,
        **{timeout_argument: 1},
    )

    assert result.success is False
    assert result.returncode == -1
    assert "timed out" in result.stderr
    assert time.monotonic() - started < 5


def test_run_command_reports_incomplete_input_delivery():
    command = [sys.executable, "-c", "import os,sys,time; os.close(sys.stdin.fileno()); time.sleep(.2)"]

    result = utils.CommandExecutor.run_command(
        command,
        stdin_text="x" * (2 * 1024 * 1024),
        timeout=5,
        stream_output=False,
    )

    assert result.success is False
    assert result.returncode == -1
    assert "stdin input delivery failed" in result.stderr


def test_run_command_rejects_pty_input_before_launch(tmp_path):
    marker = tmp_path / "started"
    result = utils.CommandExecutor.run_command(
        [sys.executable, "-c", "from pathlib import Path; Path(sys.argv[1]).touch()", str(marker)],
        stdin_text="input",
        use_pty=True,
    )

    assert result == utils.CommandResult(False, "", "stdin_text is incompatible with use_pty", -1)
    assert marker.exists() is False


def test_run_command_rejects_unencodable_input_before_launch(tmp_path):
    marker = tmp_path / "started"
    result = utils.CommandExecutor.run_command(
        [sys.executable, "-c", "from pathlib import Path; Path(sys.argv[1]).touch()", str(marker)],
        stdin_text="\ud800",
    )

    assert result.success is False
    assert result.returncode == -1
    assert "stdin input preparation failed" in result.stderr
    assert marker.exists() is False


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
