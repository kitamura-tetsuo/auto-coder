"""Tests for CodexMCPClient GraphRAG integration."""

import types
from unittest import mock

import pytest

from auto_coder.codex_mcp_client import CodexMCPClient
from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration


@pytest.fixture
def mock_graphrag_integration():
    """Create a mock GraphRAGMCPIntegration."""
    return mock.MagicMock(spec=GraphRAGMCPIntegration)


def test_codex_mcp_client_graphrag_disabled(monkeypatch):
    """Test CodexMCPClient with GraphRAG disabled."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = mock.MagicMock()
    fake_proc.pid = 123
    fake_proc.poll.return_value = None
    fake_proc.stdout = mock.MagicMock()
    fake_proc.stdin = mock.MagicMock()
    fake_proc.stderr = None

    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
        client = CodexMCPClient(enable_graphrag=False)

    assert client.enable_graphrag is False
    assert client.graphrag_integration is None


def test_codex_mcp_client_graphrag_enabled(monkeypatch):
    """Test CodexMCPClient with GraphRAG enabled."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = mock.MagicMock()
    fake_proc.pid = 123
    fake_proc.poll.return_value = None
    fake_proc.stdout = mock.MagicMock()
    fake_proc.stdin = mock.MagicMock()
    fake_proc.stderr = None

    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
        client = CodexMCPClient(enable_graphrag=True)

    assert client.enable_graphrag is True
    assert isinstance(client.graphrag_integration, GraphRAGMCPIntegration)


def test_run_llm_cli_graphrag_ensure_ready_success(monkeypatch):
    """Test _run_llm_cli with GraphRAG ensure_ready success."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = mock.MagicMock()
    fake_proc.pid = 123
    fake_proc.poll.return_value = None
    fake_proc.stdout = mock.MagicMock()
    fake_proc.stdin = mock.MagicMock()
    fake_proc.stderr = None

    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
        client = CodexMCPClient(enable_graphrag=True)

    # Mock GraphRAG integration
    mock_integration = mock.MagicMock()
    mock_integration.ensure_ready.return_value = True
    client.graphrag_integration = mock_integration

    # Mock codex exec
    mock_exec_proc = mock.MagicMock()
    mock_exec_proc.stdout = iter(["output line 1\n", "output line 2\n"])
    mock_exec_proc.wait.return_value = 0

    with mock.patch("subprocess.Popen", return_value=mock_exec_proc):
        result = client._run_llm_cli("test prompt")

    assert "output line 1" in result
    assert "output line 2" in result
    mock_integration.ensure_ready.assert_called_once()


def test_run_llm_cli_graphrag_ensure_ready_failure(monkeypatch):
    """Test _run_llm_cli with GraphRAG ensure_ready failure."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = mock.MagicMock()
    fake_proc.pid = 123
    fake_proc.poll.return_value = None
    fake_proc.stdout = mock.MagicMock()
    fake_proc.stdin = mock.MagicMock()
    fake_proc.stderr = None

    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
        client = CodexMCPClient(enable_graphrag=True)

    # Mock GraphRAG integration with failure
    mock_integration = mock.MagicMock()
    mock_integration.ensure_ready.return_value = False
    client.graphrag_integration = mock_integration

    # Mock codex exec
    mock_exec_proc = mock.MagicMock()
    mock_exec_proc.stdout = iter(["output line\n"])
    mock_exec_proc.wait.return_value = 0

    with mock.patch("subprocess.Popen", return_value=mock_exec_proc):
        result = client._run_llm_cli("test prompt")

    # Should continue despite GraphRAG failure
    assert "output line" in result
    mock_integration.ensure_ready.assert_called_once()


def test_run_llm_cli_graphrag_ensure_ready_exception(monkeypatch):
    """Test _run_llm_cli with GraphRAG ensure_ready exception."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = mock.MagicMock()
    fake_proc.pid = 123
    fake_proc.poll.return_value = None
    fake_proc.stdout = mock.MagicMock()
    fake_proc.stdin = mock.MagicMock()
    fake_proc.stderr = None

    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
        client = CodexMCPClient(enable_graphrag=True)

    # Mock GraphRAG integration with exception
    mock_integration = mock.MagicMock()
    mock_integration.ensure_ready.side_effect = Exception("GraphRAG error")
    client.graphrag_integration = mock_integration

    # Mock codex exec
    mock_exec_proc = mock.MagicMock()
    mock_exec_proc.stdout = iter(["output line\n"])
    mock_exec_proc.wait.return_value = 0

    with mock.patch("subprocess.Popen", return_value=mock_exec_proc):
        result = client._run_llm_cli("test prompt")

    # Should continue despite GraphRAG exception
    assert "output line" in result
    mock_integration.ensure_ready.assert_called_once()


def test_run_llm_cli_no_graphrag(monkeypatch):
    """Test _run_llm_cli without GraphRAG integration."""
    fake_run = mock.MagicMock(return_value=types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.run", fake_run)

    fake_proc = mock.MagicMock()
    fake_proc.pid = 123
    fake_proc.poll.return_value = None
    fake_proc.stdout = mock.MagicMock()
    fake_proc.stdin = mock.MagicMock()
    fake_proc.stderr = None

    mock_popen = mock.MagicMock(return_value=fake_proc)
    monkeypatch.setattr("auto_coder.codex_mcp_client.subprocess.Popen", mock_popen)

    with mock.patch.object(CodexMCPClient, "_rpc_call", side_effect=TimeoutError("timeout")):
        client = CodexMCPClient(enable_graphrag=False)

    # Mock codex exec
    mock_exec_proc = mock.MagicMock()
    mock_exec_proc.stdout = iter(["output line\n"])
    mock_exec_proc.wait.return_value = 0

    with mock.patch("subprocess.Popen", return_value=mock_exec_proc):
        result = client._run_llm_cli("test prompt")

    assert "output line" in result
