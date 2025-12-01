"""
Tests for Jules client functionality.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.jules_client import JulesClient
from src.auto_coder.utils import CommandResult


class TestJulesClient:
    """Test cases for JulesClient class."""

    @patch("subprocess.run")
    def test_init_checks_cli(self, mock_run):
        """JulesClient should check jules --version at init."""
        mock_run.return_value.returncode = 0
        client = JulesClient()
        assert client.backend_name == "jules"
        assert client.timeout is None
        assert len(client.active_sessions) == 0

    @patch("subprocess.run")
    def test_init_with_backend_name(self, mock_run):
        """JulesClient should use provided backend name."""
        mock_run.return_value.returncode = 0
        client = JulesClient(backend_name="custom-jules")
        assert client.backend_name == "custom-jules"

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_init_loads_config_and_options(self, mock_get_config, mock_run):
        """JulesClient should load configuration from config file."""
        mock_run.return_value.returncode = 0

        # Mock config to provide options
        from unittest.mock import Mock

        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = ["--flag1", "value1", "--flag2"]
        mock_backend_config.options_for_noedit = ["--no-edit-flag"]
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()

        # Verify config was queried
        mock_config.get_backend_config.assert_called_once_with("jules")
        assert client.options == ["--flag1", "value1", "--flag2"]
        assert client.options_for_noedit == ["--no-edit-flag"]

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_init_with_empty_config_options(self, mock_get_config, mock_run):
        """JulesClient should handle empty options from config."""
        mock_run.return_value.returncode = 0

        # Mock config with no options
        from unittest.mock import Mock

        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = None
        mock_backend_config.options_for_noedit = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()

        # Should default to empty lists
        assert client.options == []
        assert client.options_for_noedit == []

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_init_with_backend_name_uses_correct_config(self, mock_get_config, mock_run):
        """JulesClient should use correct backend name for config lookup."""
        mock_run.return_value.returncode = 0

        # Mock config
        from unittest.mock import Mock

        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = ["--custom"]
        mock_backend_config.options_for_noedit = []
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient(backend_name="custom-jules")

        # Should use custom-jules for config lookup
        mock_config.get_backend_config.assert_called_once_with("custom-jules")
        assert client.options == ["--custom"]

    @patch("subprocess.run")
    def test_init_raises_error_when_jules_not_available(self, mock_run):
        """JulesClient should raise RuntimeError when jules CLI is not available."""
        mock_run.side_effect = FileNotFoundError("jules command not found")
        with pytest.raises(RuntimeError, match="Jules CLI not available"):
            JulesClient()

    @patch("subprocess.run")
    def test_init_raises_error_on_timeout(self, mock_run):
        """JulesClient should raise RuntimeError on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired("jules", 10)
        with pytest.raises(RuntimeError, match="Jules CLI not available"):
            JulesClient()

    @patch("subprocess.run")
    def test_init_raises_error_on_nonzero_return(self, mock_run):
        """JulesClient should raise RuntimeError when jules --version returns non-zero."""
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "jules: command not found"
        with pytest.raises(RuntimeError, match="Jules CLI not available"):
            JulesClient()

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_start_session(self, mock_run_command, mock_run):
        """Test starting a new Jules session."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "Session started: abc123\n", "", 0)

        client = JulesClient()
        session_id = client.start_session("Test prompt")

        assert session_id == "abc123"
        assert session_id in client.active_sessions
        assert client.active_sessions[session_id] == "Test prompt"

        # Verify correct command was called
        mock_run_command.assert_called_once()
        call_args = mock_run_command.call_args[0][0]
        assert call_args == ["jules", "session", "start"]

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_start_session_with_options_from_config(self, mock_run_command, mock_get_config, mock_run):
        """Test that options from config are added to start_session command."""
        mock_run.return_value.returncode = 0

        # Mock config to provide options
        from unittest.mock import Mock

        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = ["--verbose", "--timeout", "30"]
        mock_backend_config.options_for_noedit = []
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        mock_run_command.return_value = CommandResult(True, "Session started: abc123\n", "", 0)

        client = JulesClient()
        _ = client.start_session("Test prompt")

        # Verify correct command was called with options
        mock_run_command.assert_called_once()
        call_args = mock_run_command.call_args[0][0]
        assert call_args[0] == "jules"
        assert call_args[1] == "session"
        assert call_args[2] == "start"
        assert "--verbose" in call_args
        assert "--timeout" in call_args
        assert "30" in call_args

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_start_session_without_options(self, mock_run_command, mock_get_config, mock_run):
        """Test that session starts correctly when no options are configured."""
        mock_run.return_value.returncode = 0

        # Mock config with no options
        from unittest.mock import Mock

        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        mock_run_command.return_value = CommandResult(True, "Session started: abc123\n", "", 0)

        client = JulesClient()
        _ = client.start_session("Test prompt")

        # Verify correct command was called without extra options
        mock_run_command.assert_called_once()
        call_args = mock_run_command.call_args[0][0]
        assert call_args == ["jules", "session", "start"]

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_start_session_with_session_started_format(self, mock_run_command, mock_run):
        """Test starting session with 'Session started:' format."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "Session started: session-xyz-789\n", "", 0)

        client = JulesClient()
        session_id = client.start_session("Test prompt")

        assert session_id == "session-xyz-789"

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_start_session_with_session_id_format(self, mock_run_command, mock_run):
        """Test starting session with 'session_id:' format."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "session_id: my-session-123\n", "", 0)

        client = JulesClient()
        session_id = client.start_session("Test prompt")

        assert session_id == "my-session-123"

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_start_session_with_regex_alphanumeric(self, mock_run_command, mock_run):
        """Test starting session when alphanumeric ID is extracted via regex."""
        mock_run.return_value.returncode = 0
        # Output without clear "session started" or "session_id:" markers
        mock_run_command.return_value = CommandResult(True, "Output\nabc123def456\n", "", 0)

        client = JulesClient()
        session_id = client.start_session("Test prompt")

        # Should extract alphanumeric ID via regex
        assert session_id == "abc123def456"

        # Fallback: if no pattern matches, should generate timestamp-based ID
        # This test covers the case where regex matches
        assert len(session_id) > 0

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_start_session_fallback_to_generated_id(self, mock_run_command, mock_run):
        """Test fallback to generated session ID when extraction fails."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "No session ID in output\n", "", 0)

        client = JulesClient()
        import time

        before_time = int(time.time())
        session_id = client.start_session("Test prompt")
        after_time = int(time.time())

        # Should generate timestamp-based ID
        assert session_id.startswith("session_")
        session_timestamp = int(session_id.split("_")[1])
        assert before_time <= session_timestamp <= after_time

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_start_session_fallback_on_failure(self, mock_run_command, mock_run):
        """Test that start_session falls back to generated ID when command fails."""
        mock_run.return_value.returncode = 0
        # Command fails but doesn't raise exception - just returns error
        mock_run_command.return_value = CommandResult(False, "", "Command failed", 1)

        client = JulesClient()
        # Should not raise error, but fall back to generated session ID
        session_id = client.start_session("Test prompt")

        # Should have generated a timestamp-based session ID
        assert session_id.startswith("session_")
        assert session_id in client.active_sessions

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_send_message(self, mock_run_command, mock_run):
        """Test sending a message to an existing session."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "Response from Jules\n", "", 0)

        client = JulesClient()
        # Manually add a session
        client.active_sessions["test-session"] = "Previous prompt"

        response = client.send_message("test-session", "New message")

        assert response == "Response from Jules"

        # Verify correct command was called
        mock_run_command.assert_called_once()
        call_args = mock_run_command.call_args[0][0]
        assert call_args == ["jules", "session", "send", "--session", "test-session"]

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_send_message_with_options_from_config(self, mock_run_command, mock_get_config, mock_run):
        """Test that options from config are added to send_message command."""
        mock_run.return_value.returncode = 0

        # Mock config to provide options
        from unittest.mock import Mock

        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = ["--debug", "--log-level", "info"]
        mock_backend_config.options_for_noedit = []
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        mock_run_command.return_value = CommandResult(True, "Response from Jules\n", "", 0)

        client = JulesClient()
        client.active_sessions["test-session"] = "Previous prompt"

        response = client.send_message("test-session", "New message")

        assert response == "Response from Jules"

        # Verify correct command was called with options
        mock_run_command.assert_called_once()
        call_args = mock_run_command.call_args[0][0]
        assert call_args[0] == "jules"
        assert call_args[1] == "session"
        assert call_args[2] == "send"
        assert call_args[3] == "--session"
        assert call_args[4] == "test-session"
        assert "--debug" in call_args
        assert "--log-level" in call_args
        assert "info" in call_args

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_send_message_raises_error_on_failure(self, mock_run_command, mock_run):
        """Test that send_message raises error on command failure."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "Command failed", 1)

        client = JulesClient()
        client.active_sessions["test-session"] = "Prompt"

        with pytest.raises(RuntimeError, match="Failed to send message to Jules session"):
            client.send_message("test-session", "Message")

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_end_session_success(self, mock_run_command, mock_run):
        """Test ending a session successfully."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "", "", 0)

        client = JulesClient()
        client.active_sessions["test-session"] = "Prompt"

        result = client.end_session("test-session")

        assert result is True
        assert "test-session" not in client.active_sessions

        # Verify correct command was called
        mock_run_command.assert_called_once()
        call_args = mock_run_command.call_args[0][0]
        assert call_args == ["jules", "session", "end", "--session", "test-session"]

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_end_session_failure(self, mock_run_command, mock_run):
        """Test that end_session returns False on failure."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "Command failed", 1)

        client = JulesClient()
        client.active_sessions["test-session"] = "Prompt"

        result = client.end_session("test-session")

        assert result is False
        # Session should still be tracked on failure
        assert "test-session" in client.active_sessions

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_end_session_removes_from_active_sessions_on_success(self, mock_run_command, mock_run):
        """Test that session is removed from active_sessions only on success."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "", "", 0)

        client = JulesClient()
        client.active_sessions["session1"] = "Prompt1"
        client.active_sessions["session2"] = "Prompt2"

        # End session1 successfully
        client.end_session("session1")

        # session1 should be removed, session2 should remain
        assert "session1" not in client.active_sessions
        assert "session2" in client.active_sessions

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_run_llm_cli_single_run(self, mock_run_command, mock_run):
        """Test _run_llm_cli starts session, sends message, and ends session."""
        mock_run.return_value.returncode = 0

        # Mock start_session
        mock_run_command.side_effect = [
            CommandResult(True, "Session started: abc123\n", "", 0),  # start_session
            CommandResult(True, "Response from Jules\n", "", 0),  # send_message
            CommandResult(True, "", "", 0),  # end_session
        ]

        client = JulesClient()
        response = client._run_llm_cli("Test prompt")

        assert response == "Response from Jules"
        assert "abc123" not in client.active_sessions  # Session should be cleaned up

        # Should have called run_command 3 times: start, send, end
        assert mock_run_command.call_count == 3

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_run_llm_cli_ensures_session_cleanup_on_error(self, mock_run_command, mock_run):
        """Test that _run_llm_cli cleans up session even when send_message fails."""
        mock_run.return_value.returncode = 0

        # start_session succeeds, send_message fails, end_session should still be called
        mock_run_command.side_effect = [
            CommandResult(True, "Session started: abc123\n", "", 0),  # start_session
            CommandResult(False, "", "Command failed", 1),  # send_message (fails)
            CommandResult(True, "", "", 0),  # end_session (should still be called)
        ]

        client = JulesClient()

        with pytest.raises(RuntimeError):
            client._run_llm_cli("Test prompt")

        # Session should be cleaned up despite the error
        assert "abc123" not in client.active_sessions

        # Should have called run_command 3 times despite the error
        assert mock_run_command.call_count == 3

    @patch("subprocess.run")
    @patch("src.auto_coder.jules_client.CommandExecutor.run_command")
    def test_extract_session_id_from_output(self, mock_run_command, mock_run):
        """Test _extract_session_id method with various formats."""
        mock_run.return_value.returncode = 0

        client = JulesClient()

        # Test format 1: "Session started: <id>"
        session_id = client._extract_session_id("Session started: abc123")
        assert session_id == "abc123"

        # Test format 2: "session_id: <id>"
        session_id = client._extract_session_id("session_id: xyz789")
        assert session_id == "xyz789"

        # Test format 3: case insensitive
        session_id = client._extract_session_id("SESSION STARTED: MySession123")
        assert session_id == "mysession123"

        # Test format 4: with dashes and underscores
        session_id = client._extract_session_id("Session started: session-abc_123-def")
        # The regex matches the full alphanumeric sequence with dashes/underscores
        assert session_id == "session-abc_123-def"

        # Test format 5: alphanumeric extraction via regex
        session_id = client._extract_session_id("Some output abc123def456 xyz")
        assert session_id == "abc123def456"

        # Test format 6: no match
        session_id = client._extract_session_id("No session ID here")
        assert session_id is None

        # Test format 7: empty string
        session_id = client._extract_session_id("")
        assert session_id is None

        # Test format 8: None
        session_id = client._extract_session_id(None)
        assert session_id is None
