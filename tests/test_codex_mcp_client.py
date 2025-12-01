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


def test_fallback_exec_uses_options(monkeypatch):
    """Test that fallback exec calls use configured options."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.run", fake_run)

    # Track all Popen calls
    popen_calls = []

    def track_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        # Return appropriate mock based on command
        if len(popen_calls) == 1:
            # First call is for MCP session
            return _make_fake_popen()
        else:
            # Second call is for fallback exec
            fake_proc = mock.MagicMock()
            fake_proc.communicate.return_value = ("test output", None)
            fake_proc.returncode = 0
            fake_proc.stdout = mock.MagicMock()
            return fake_proc

    with mock.patch("src.auto_coder.codex_mcp_client.subprocess.Popen", side_effect=track_popen):
        with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
            # Mock config with custom options
            mock_config = mock.MagicMock()
            mock_backend = mock.MagicMock()
            mock_backend.model = "codex-mcp"
            mock_backend.options = ["--custom-exec-flag", "--another-exec-flag"]
            mock_config.get_backend_config.return_value = mock_backend
            monkeypatch.setattr("src.auto_coder.codex_mcp_client.get_llm_config", mock.MagicMock(return_value=mock_config))

            client = CodexMCPClient()
            try:
                # Call _run_llm_cli to trigger fallback exec
                client._run_llm_cli("test prompt")
            except Exception:
                # Ignore errors from the test setup
                pass

    # Verify exec command includes configured options
    assert len(popen_calls) >= 2
    args, _ = popen_calls[1]
    cmd = args[0]
    assert cmd[:2] == ["codex", "exec"]
    # Check that custom options are included
    assert "--custom-exec-flag" in cmd
    assert "--another-exec-flag" in cmd
    # Check that default options are not present when custom options are configured
    assert "-s" not in cmd
    assert "workspace-write" not in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


def test_fallback_exec_uses_defaults_when_no_options(monkeypatch):
    """Test that fallback exec uses default options when no options configured."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.run", fake_run)

    # Track all Popen calls
    popen_calls = []

    def track_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        # Return appropriate mock based on command
        if len(popen_calls) == 1:
            # First call is for MCP session
            return _make_fake_popen()
        else:
            # Second call is for fallback exec
            fake_proc = mock.MagicMock()
            fake_proc.communicate.return_value = ("test output", None)
            fake_proc.returncode = 0
            fake_proc.stdout = mock.MagicMock()
            return fake_proc

    with mock.patch("src.auto_coder.codex_mcp_client.subprocess.Popen", side_effect=track_popen):
        with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
            # Mock config without options
            mock_config = mock.MagicMock()
            mock_backend = mock.MagicMock()
            mock_backend.model = "codex-mcp"
            mock_backend.options = None
            mock_config.get_backend_config.return_value = mock_backend
            monkeypatch.setattr("src.auto_coder.codex_mcp_client.get_llm_config", mock.MagicMock(return_value=mock_config))

            client = CodexMCPClient()
            try:
                # Call _run_llm_cli to trigger fallback exec
                client._run_llm_cli("test prompt")
            except Exception:
                # Ignore errors from the test setup
                pass

    # Verify exec command includes default options
    assert len(popen_calls) >= 2
    args, _ = popen_calls[1]
    cmd = args[0]
    assert cmd == ["codex", "exec", "-s", "workspace-write", "--dangerously-bypass-approvals-and-sandbox", "test prompt"]


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
