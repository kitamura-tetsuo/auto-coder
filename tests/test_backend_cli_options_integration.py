"""
Integration tests for all LLM backends with configured CLI options.

This test suite verifies that all 7 backends (Codex, Claude, Gemini, Qwen, Auggie, Jules, Codex-MCP)
correctly use configured CLI options from llm_config.toml in their command construction.
"""

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.auggie_client import AuggieClient
from src.auto_coder.claude_client import ClaudeClient
from src.auto_coder.codex_client import CodexClient
from src.auto_coder.codex_mcp_client import CodexMCPClient
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.jules_client import JulesClient
from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


class RecordingPopen:
    """Stub Popen implementation that records invocations for testing."""

    calls = []

    def __init__(self, cmd, stdout=None, stderr=None, text=False, bufsize=1, **kwargs):
        type(self).calls.append(list(cmd))
        self.stdout = io.StringIO("test output\n")
        self._cmd = list(cmd)

    def wait(self, timeout=None):
        return 0


def _patch_subprocess_for_auggie_jules(monkeypatch):
    """Patch subprocess for Auggie and Jules clients."""
    monkeypatch.setattr(
        "src.auto_coder.auggie_client.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("src.auto_coder.auggie_client.subprocess.Popen", RecordingPopen)

    monkeypatch.setattr(
        "src.auto_coder.jules_client.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("src.auto_coder.jules_client.subprocess.Popen", RecordingPopen)


def _patch_subprocess_for_codex_mcp(monkeypatch):
    """Patch subprocess for Codex-MCP client."""
    RecordingPopen.calls = []

    class MockPopen(RecordingPopen):
        """Mock Popen with required attributes."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pid = 12345
            self.stderr = io.StringIO("")

    monkeypatch.setattr(
        "src.auto_coder.codex_mcp_client.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("src.auto_coder.codex_mcp_client.subprocess.Popen", MockPopen)
    monkeypatch.setattr(
        "src.auto_coder.codex_mcp_client._is_running_under_pytest",
        lambda: True,
    )


class TestBackendCLIOptions:
    """Test suite for verifying CLI options usage across all backends."""

    # ================================
    # Codex Client Tests
    # ================================

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_codex_with_configured_options(self, mock_run_command, mock_run):
        """Test that CodexClient uses options from config."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(success=True, stdout="test output", stderr="", returncode=0)

        # Mock config to provide options
        with patch("src.auto_coder.codex_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "codex"
            mock_backend_config.options = ["-o", "timeout", "30", "-o", "stream", "true"]
            mock_backend_config.api_key = None
            mock_backend_config.base_url = None
            mock_backend_config.openai_api_key = None
            mock_backend_config.openai_base_url = None
            mock_backend_config.model_provider = None
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = CodexClient(backend_name="codex")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure includes options
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Check that options are in the command
        assert "-o" in cmd
        assert "timeout" in cmd
        assert "30" in cmd
        assert "-o" in cmd[cmd.index("30") + 1 :]
        assert "stream" in cmd[cmd.index("30") + 1 :]
        assert "true" in cmd[cmd.index("30") + 1 :]

        # Verify the command starts with expected elements
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "-s" in cmd
        assert "workspace-write" in cmd

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_codex_with_empty_options(self, mock_run_command, mock_run):
        """Test that CodexClient handles empty options."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(success=True, stdout="test output", stderr="", returncode=0)

        # Mock config with empty options
        with patch("src.auto_coder.codex_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "codex"
            mock_backend_config.options = []
            mock_backend_config.api_key = None
            mock_backend_config.base_url = None
            mock_backend_config.openai_api_key = None
            mock_backend_config.openai_base_url = None
            mock_backend_config.model_provider = None
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = CodexClient(backend_name="codex")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure without options
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Verify basic command structure
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "-s" in cmd
        assert "workspace-write" in cmd

        # Verify no options flags are present
        assert "-o" not in cmd

    # ================================
    # Claude Client Tests
    # ================================

    @patch("subprocess.run")
    @patch("src.auto_coder.claude_client.CommandExecutor.run_command")
    def test_claude_with_configured_options(self, mock_run_command, mock_run):
        """Test that ClaudeClient uses options from config."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(success=True, stdout="test output", stderr="", returncode=0)

        # Mock config to provide options
        with patch("src.auto_coder.claude_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "sonnet"
            mock_backend_config.options = ["--model-version", "3.5"]
            mock_backend_config.options_for_noedit = ["--no-edit"]
            mock_backend_config.api_key = None
            mock_backend_config.base_url = None
            mock_backend_config.openai_api_key = None
            mock_backend_config.openai_base_url = None
            mock_backend_config.settings = None
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = ClaudeClient(backend_name="claude")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure includes options
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Check that options are in the command
        assert "--model-version" in cmd
        assert "3.5" in cmd

        # Verify the command starts with expected elements
        assert cmd[0] == "claude"

    @patch("subprocess.run")
    @patch("src.auto_coder.claude_client.CommandExecutor.run_command")
    def test_claude_with_empty_options(self, mock_run_command, mock_run):
        """Test that ClaudeClient handles empty options."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(success=True, stdout="test output", stderr="", returncode=0)

        # Mock config with empty options
        with patch("src.auto_coder.claude_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "sonnet"
            mock_backend_config.options = []
            mock_backend_config.options_for_noedit = []
            mock_backend_config.api_key = None
            mock_backend_config.base_url = None
            mock_backend_config.openai_api_key = None
            mock_backend_config.openai_base_url = None
            mock_backend_config.settings = None
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = ClaudeClient(backend_name="claude")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure without options
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Verify basic command structure
        assert cmd[0] == "claude"

        # Verify no custom options are present
        assert "--model-version" not in cmd

    # ================================
    # Gemini Client Tests
    # ================================

    @patch("subprocess.run")
    @patch("src.auto_coder.gemini_client.genai")
    @patch("src.auto_coder.gemini_client.CommandExecutor.run_command")
    def test_gemini_with_configured_options(self, mock_run_command, mock_genai, mock_run):
        """Test that GeminiClient uses options from config."""
        mock_run.return_value.returncode = 0
        mock_genai.configure = MagicMock()
        mock_run_command.return_value = CommandResult(success=True, stdout="test output", stderr="", returncode=0)

        # Mock config to provide options
        with patch("src.auto_coder.gemini_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "gemini-2.5-pro"
            mock_backend_config.options = ["-o", "temperature", "0.7", "-o", "max-tokens", "2048"]
            mock_backend_config.options_for_noedit = ["-o", "read-only"]
            mock_backend_config.api_key = None
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = GeminiClient(backend_name="gemini")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure includes options
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Check that options are in the command
        assert "-o" in cmd
        assert "temperature" in cmd
        assert "0.7" in cmd
        assert "-o" in cmd[cmd.index("0.7") + 1 :]
        assert "max-tokens" in cmd[cmd.index("0.7") + 1 :]
        assert "2048" in cmd[cmd.index("0.7") + 1 :]

        # Verify the command starts with expected elements
        assert cmd[0] == "gemini"

    @patch("subprocess.run")
    @patch("src.auto_coder.gemini_client.genai")
    @patch("src.auto_coder.gemini_client.CommandExecutor.run_command")
    def test_gemini_with_empty_options(self, mock_run_command, mock_genai, mock_run):
        """Test that GeminiClient handles empty options."""
        mock_run.return_value.returncode = 0
        mock_genai.configure = MagicMock()
        mock_run_command.return_value = CommandResult(success=True, stdout="test output", stderr="", returncode=0)

        # Mock config with empty options
        with patch("src.auto_coder.gemini_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "gemini-2.5-pro"
            mock_backend_config.options = []
            mock_backend_config.options_for_noedit = []
            mock_backend_config.api_key = None
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = GeminiClient(backend_name="gemini")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure without options
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Verify basic command structure
        assert cmd[0] == "gemini"

        # Verify no options flags are present
        assert "-o" not in cmd

    # ================================
    # Qwen Client Tests
    # ================================

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_qwen_with_configured_options(self, mock_run_command, mock_run):
        """Test that QwenClient uses options from config."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(success=True, stdout="test output", stderr="", returncode=0)

        # Mock config to provide options
        with patch("src.auto_coder.qwen_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "qwen3-coder-plus"
            mock_backend_config.options = ["-o", "stream", "false", "--debug"]
            mock_backend_config.api_key = None
            mock_backend_config.base_url = None
            mock_backend_config.openai_api_key = None
            mock_backend_config.openai_base_url = None
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = QwenClient(backend_name="qwen")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure includes options
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Check that options are in the command
        assert "-o" in cmd
        assert "stream" in cmd
        assert "false" in cmd
        assert "--debug" in cmd

        # Verify the command starts with expected elements
        assert cmd[0] == "qwen"

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_qwen_with_empty_options(self, mock_run_command, mock_run):
        """Test that QwenClient handles empty options."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(success=True, stdout="test output", stderr="", returncode=0)

        # Mock config with empty options
        with patch("src.auto_coder.qwen_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "qwen3-coder-plus"
            mock_backend_config.options = []
            mock_backend_config.api_key = None
            mock_backend_config.base_url = None
            mock_backend_config.openai_api_key = None
            mock_backend_config.openai_base_url = None
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = QwenClient(backend_name="qwen")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure without options
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Verify basic command structure
        assert cmd[0] == "qwen"

        # Verify no custom options are present
        assert "-o" not in cmd
        assert "--debug" not in cmd

    # ================================
    # Auggie Client Tests
    # ================================

    def test_auggie_with_configured_options(self, monkeypatch):
        """Test that AuggieClient uses options from config."""
        RecordingPopen.calls = []
        _patch_subprocess_for_auggie_jules(monkeypatch)

        # Mock config to provide options
        with patch("src.auto_coder.auggie_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "GPT-5"
            mock_backend_config.options = ["-o", "creativity", "high", "-o", "verbosity", "detailed"]
            mock_backend_config.options_for_noedit = ["-o", "read-only"]
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = AuggieClient(backend_name="auggie")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure includes options
        assert len(RecordingPopen.calls) == 1
        cmd = RecordingPopen.calls[0]

        # Check that options are in the command
        assert "-o" in cmd
        assert "creativity" in cmd
        assert "high" in cmd
        assert "-o" in cmd[cmd.index("high") + 1 :]
        assert "verbosity" in cmd[cmd.index("high") + 1 :]
        assert "detailed" in cmd[cmd.index("high") + 1 :]

        # Verify the command starts with expected elements
        assert cmd[0] == "auggie"
        assert "--model" in cmd
        assert "GPT-5" in cmd

    def test_auggie_with_empty_options(self, monkeypatch):
        """Test that AuggieClient handles empty options."""
        RecordingPopen.calls = []
        _patch_subprocess_for_auggie_jules(monkeypatch)

        # Mock config with empty options
        with patch("src.auto_coder.auggie_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "GPT-5"
            mock_backend_config.options = []
            mock_backend_config.options_for_noedit = []
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = AuggieClient(backend_name="auggie")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure without options
        assert len(RecordingPopen.calls) == 1
        cmd = RecordingPopen.calls[0]

        # Verify basic command structure
        assert cmd[0] == "auggie"
        assert "--model" in cmd
        assert "GPT-5" in cmd

        # Verify no custom options are present
        assert "-o" not in cmd

    # ================================
    # Jules Client Tests
    # ================================

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_jules_with_configured_options(self, mock_run_command, mock_run):
        """Test that JulesClient uses options from config."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(success=True, stdout="Session started: test_session_123", stderr="", returncode=0)

        # Mock config to provide options
        with patch("src.auto_coder.jules_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.options = ["-o", "session-timeout", "3600", "-o", "verbose"]
            mock_backend_config.options_for_noedit = ["-o", "no-edit-mode"]
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = JulesClient(backend_name="jules")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure includes options
        assert mock_run_command.called
        # Find the call with 'session start'
        start_call = None
        for call_args in mock_run_command.call_args_list:
            cmd = call_args[0][0]
            if "start" in cmd:
                start_call = cmd
                break

        assert start_call is not None

        # Check that options are in the command
        assert "-o" in start_call
        assert "session-timeout" in start_call
        assert "3600" in start_call
        assert "-o" in start_call[start_call.index("3600") + 1 :]
        assert "verbose" in start_call[start_call.index("3600") + 1 :]

        # Verify the command starts with expected elements
        assert start_call[0] == "jules"
        assert "session" in start_call
        assert "start" in start_call

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_jules_with_empty_options(self, mock_run_command, mock_run):
        """Test that JulesClient handles empty options."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(success=True, stdout="Session started: test_session_123", stderr="", returncode=0)

        # Mock config with empty options
        with patch("src.auto_coder.jules_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.options = []
            mock_backend_config.options_for_noedit = []
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            client = JulesClient(backend_name="jules")
            output = client._run_llm_cli("test prompt")

        # Verify the command structure without options
        assert mock_run_command.called
        # Find the call with 'session start'
        start_call = None
        for call_args in mock_run_command.call_args_list:
            cmd = call_args[0][0]
            if "start" in cmd:
                start_call = cmd
                break

        assert start_call is not None

        # Verify basic command structure
        assert start_call[0] == "jules"
        assert "session" in start_call
        assert "start" in start_call

        # Verify no custom options are present
        assert "-o" not in start_call

    # ================================
    # Codex-MCP Client Tests
    # ================================

    def test_codex_mcp_with_configured_options(self, monkeypatch):
        """Test that CodexMCPClient uses options from config."""
        _patch_subprocess_for_codex_mcp(monkeypatch)

        # Mock config to provide options
        with patch("src.auto_coder.codex_mcp_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "codex-mcp"
            mock_backend_config.options = ["-o", "timeout", "60", "-o", "retry", "3"]
            mock_backend_config.options_for_noedit = ["-o", "read-only"]
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            # Mock the process cleanup
            with patch.object(CodexMCPClient, "close"):
                client = CodexMCPClient(backend_name="codex-mcp")
                # Trigger MCP fallback to codex exec
                try:
                    output = client._run_llm_cli("test prompt")
                except Exception:
                    pass  # Expected to fail in test environment

        # Verify the command structure includes options
        assert len(RecordingPopen.calls) >= 1
        # Find the call with 'codex exec'
        exec_call = None
        for call in RecordingPopen.calls:
            if len(call) > 1 and call[0] == "codex" and call[1] == "exec":
                exec_call = call
                break

        assert exec_call is not None

        # Check that options are in the command
        assert "-o" in exec_call
        assert "timeout" in exec_call
        assert "60" in exec_call
        assert "-o" in exec_call[exec_call.index("60") + 1 :]
        assert "retry" in exec_call[exec_call.index("60") + 1 :]
        assert "3" in exec_call[exec_call.index("60") + 1 :]

        # Verify the command starts with expected elements
        assert exec_call[0] == "codex"
        assert "exec" in exec_call

    def test_codex_mcp_with_empty_options(self, monkeypatch):
        """Test that CodexMCPClient handles empty options."""
        _patch_subprocess_for_codex_mcp(monkeypatch)

        # Mock config with empty options
        with patch("src.auto_coder.codex_mcp_client.get_llm_config") as mock_get_config:
            mock_config = Mock()
            mock_backend_config = Mock()
            mock_backend_config.model = "codex-mcp"
            mock_backend_config.options = []
            mock_backend_config.options_for_noedit = []
            mock_backend_config.usage_markers = []
            mock_backend_config.validate_required_options.return_value = []
            mock_config.get_backend_config.return_value = mock_backend_config
            mock_get_config.return_value = mock_config

            # Mock the process cleanup
            with patch.object(CodexMCPClient, "close"):
                client = CodexMCPClient(backend_name="codex-mcp")
                # Trigger MCP fallback to codex exec
                try:
                    output = client._run_llm_cli("test prompt")
                except Exception:
                    pass  # Expected to fail in test environment

        # Verify the command structure without options
        assert len(RecordingPopen.calls) >= 1
        # Find the call with 'codex exec'
        exec_call = None
        for call in RecordingPopen.calls:
            if len(call) > 1 and call[0] == "codex" and call[1] == "exec":
                exec_call = call
                break

        assert exec_call is not None

        # Verify basic command structure
        assert exec_call[0] == "codex"
        assert "exec" in exec_call

        # Verify no custom options are present
        assert "-o" not in exec_call
