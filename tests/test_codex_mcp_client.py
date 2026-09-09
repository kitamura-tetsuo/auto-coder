import subprocess
import sys
import threading
import types
from unittest import mock

import pytest

from src.auto_coder import __version__ as AUTO_CODER_VERSION
from src.auto_coder.codex_mcp_client import CodexMCPClient


def _make_fake_popen(stdout=None, stderr=None):
    fake_proc = mock.MagicMock()
    fake_proc.pid = 123
    fake_proc.poll.return_value = None
    fake_proc.stdout = stdout if stdout is not None else mock.MagicMock()
    fake_proc.stdin = mock.MagicMock()
    fake_proc.stderr = stderr
    return fake_proc


def test_handshake_timeout_uses_configured_limit(monkeypatch):
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = _make_fake_popen()
    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    # Mock config
    mock_config = mock.MagicMock()
    mock_backend = mock.MagicMock()
    mock_backend.model = "codex-mcp"
    mock_config.get_backend_config.return_value = mock_backend
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.get_llm_config", mock.MagicMock(return_value=mock_config))

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")) as mock_rpc:
        client = CodexMCPClient()

    assert client._initialized is False
    mock_rpc.assert_called_with(
        method="initialize",
        params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "auto-coder", "version": AUTO_CODER_VERSION},
        },
        timeout=client._handshake_timeout,
    )
    assert mock_popen.call_count == 1
    _, kwargs = mock_popen.call_args
    assert kwargs.get("bufsize") == 0


def test_options_loaded_from_config(monkeypatch):
    """Test that options are loaded from config in __init__."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = _make_fake_popen()
    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    # Mock config with options
    mock_config = mock.MagicMock()
    mock_backend = mock.MagicMock()
    mock_backend.model = "codex-mcp"
    mock_backend.options = ["--flag1", "--flag2"]
    mock_backend.options_for_noedit = ["--noedit-flag"]
    mock_config.get_backend_config.return_value = mock_backend
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.get_llm_config", mock.MagicMock(return_value=mock_config))

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
        client = CodexMCPClient()

    # Verify options are loaded from config
    assert client.options == ["--flag1", "--flag2"]
    assert client.options_for_noedit == ["--noedit-flag"]


def test_options_applied_to_mcp_session(monkeypatch):
    """Test that options are applied to MCP session startup command."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = _make_fake_popen()
    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    # Mock config with options
    mock_config = mock.MagicMock()
    mock_backend = mock.MagicMock()
    mock_backend.model = "codex-mcp"
    mock_backend.options = ["--custom-flag", "--another-flag"]
    mock_config.get_backend_config.return_value = mock_backend
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.get_llm_config", mock.MagicMock(return_value=mock_config))

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
        client = CodexMCPClient()

    # Verify MCP command includes configured options
    assert mock_popen.call_count == 1
    args, _ = mock_popen.call_args
    cmd = args[0]
    assert cmd == ["codex", "mcp", "--custom-flag", "--another-flag"]


@pytest.mark.parametrize("options", [["--custom-exec-flag", "--another-exec-flag"], None])
@pytest.mark.timeout(10)
def test_fallback_exec_uses_configured_options_and_finishes_streams(monkeypatch, _use_real_commands, options):
    """Exercise finite stdin and real EOF without leaving mock reader loops alive."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.run", fake_run)
    original_popen = subprocess.Popen
    threads_before = set(threading.enumerate())
    popen_calls = []
    children = []

    def track_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        if len(popen_calls) == 1:
            return _make_fake_popen()
        # Keep the executor's real pipes and stdin writer, substituting only the CLI.
        child = original_popen(
            [sys.executable, "-c", "import sys; data = sys.stdin.read(); sys.stdout.write(data)"],
            **kwargs,
        )
        children.append(child)
        return child

    mock_config = mock.MagicMock()
    mock_backend = mock.MagicMock()
    mock_backend.model = "codex-mcp"
    mock_backend.options = options
    mock_config.get_backend_config.return_value = mock_backend
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.get_llm_config", mock.MagicMock(return_value=mock_config))

    try:
        with mock.patch("src.auto_coder.codex_mcp_client.subprocess.Popen", side_effect=track_popen):
            with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
                client = CodexMCPClient()
                output = client._run_llm_cli("test prompt")
        assert output == "test prompt"
        assert len(popen_calls) == 2
        args, _ = popen_calls[1]
        assert args[0] == ["codex", "exec", *(options or []), "-"]
        assert [child.poll() for child in children] == [0]
        assert {thread for thread in threading.enumerate() if thread.name.startswith("CommandStream-")} <= threads_before
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_rpc_call_times_out_without_stdout_ready(monkeypatch):
    client = CodexMCPClient.__new__(CodexMCPClient)
    client.proc = mock.MagicMock()
    client.proc.poll.return_value = None
    client._stdin = mock.MagicMock()
    client._stdout = mock.MagicMock()
    client._default_timeout = 0.25
    client._req_id = 0
    client._initialized = False

    def never_ready(_timeout):
        return False

    client._wait_for_stdout = never_ready

    with pytest.raises(TimeoutError):
        client._rpc_call("prompts/call", timeout=0.01)

    client._stdin.write.assert_called()
