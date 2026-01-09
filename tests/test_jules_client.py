"""
Tests for Jules client functionality.
"""

import json
import uuid
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

from src.auto_coder.jules_client import JulesClient


class TestJulesClient:
    """Test cases for JulesClient class."""

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_init_initializes_session(self, mock_get_config):
        """JulesClient should initialize HTTP session on creation."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()
        assert client.backend_name == "jules"
        assert client.timeout is None
        assert len(client.active_sessions) == 0
        assert client.session is not None
        assert client.base_url == "https://jules.googleapis.com/v1alpha"

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_init_with_backend_name(self, mock_get_config):
        """JulesClient should use provided backend name."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient(backend_name="custom-jules")
        assert client.backend_name == "custom-jules"

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_init_loads_config_and_options(self, mock_get_config):
        """JulesClient should load configuration from config file."""
        # Mock config to provide options
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = ["--flag1", "value1", "--flag2"]
        mock_backend_config.options_for_noedit = ["--no-edit-flag"]
        mock_backend_config.api_key = "test-api-key"
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()

        # Verify config was queried
        mock_config.get_backend_config.assert_called_once_with("jules")
        assert client.options == ["--flag1", "value1", "--flag2"]
        assert client.options_for_noedit == ["--no-edit-flag"]
        assert client.api_key == "test-api-key"
        assert "X-Goog-Api-Key" in client.session.headers
        assert client.session.headers["X-Goog-Api-Key"] == "test-api-key"

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_init_with_empty_config_options(self, mock_get_config):
        """JulesClient should handle empty options from config."""
        # Mock config with no options
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = None
        mock_backend_config.options_for_noedit = None
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()

        # Should default to empty lists
        assert client.options == []
        assert client.options_for_noedit == []

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_init_with_backend_name_uses_correct_config(self, mock_get_config):
        """JulesClient should use correct backend name for config lookup."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = ["--custom"]
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient(backend_name="custom-jules")

        # Should use custom-jules for config lookup
        mock_config.get_backend_config.assert_called_once_with("custom-jules")
        assert client.options == ["--custom"]

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_start_session(self, mock_post, mock_get_config):
        """Test starting a new Jules session."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock the POST response
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"sessionId": "test-session-123"}
        mock_post.return_value = mock_response

        client = JulesClient()
        session_id = client.start_session("Test prompt", "owner/repo", "main")

        assert session_id == "test-session-123"
        assert session_id in client.active_sessions
        assert client.active_sessions[session_id] == "Test prompt"

        # Verify correct API call was made
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://jules.googleapis.com/v1alpha/sessions"
        assert call_args[1]["json"]["prompt"] == "Test prompt"
        assert "title" not in call_args[1]["json"]

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_start_session_with_title(self, mock_post, mock_get_config):
        """Test starting a new Jules session with a title."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock the POST response
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"sessionId": "test-session-title"}
        mock_post.return_value = mock_response

        client = JulesClient()
        session_id = client.start_session("Test prompt", repo_name="owner/repo", base_branch="main", title="Test Session Title")

        assert session_id == "test-session-title"

        # Verify correct API call was made
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://jules.googleapis.com/v1alpha/sessions"
        assert call_args[1]["json"]["prompt"] == "Test prompt"
        assert call_args[1]["json"]["title"] == "Test Session Title"

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_start_session_with_api_key(self, mock_post, mock_get_config):
        """Test that start_session uses API key from config."""
        # Mock config to provide API key
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = ["--verbose"]
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = "my-api-key"
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock the POST response
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"sessionId": "session-456"}
        mock_post.return_value = mock_response

        client = JulesClient()
        _ = client.start_session("Test prompt", "owner/repo", "main")

        # Verify API key is in headers
        assert "X-Goog-Api-Key" in client.session.headers
        assert client.session.headers["X-Goog-Api-Key"] == "my-api-key"

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_start_session_handles_http_error(self, mock_post, mock_get_config):
        """Test that start_session raises error on HTTP failure."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock error response
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_post.return_value = mock_response

        client = JulesClient()

        with pytest.raises(RuntimeError, match="Failed to start Jules session"):
            client.start_session("Test prompt", "owner/repo", "main")

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_start_session_handles_network_error(self, mock_post, mock_get_config):
        """Test that start_session raises error on network failure."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock network error
        mock_post.side_effect = requests.exceptions.ConnectionError("Network error")

        client = JulesClient()

        with pytest.raises(RuntimeError, match="Failed to start Jules session"):
            client.start_session("Test prompt", "owner/repo", "main")

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_send_message(self, mock_post, mock_get_config):
        """Test sending a message to an existing session."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock the POST response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"response": "Response from Jules"}
        mock_post.return_value = mock_response

        client = JulesClient()
        # Manually add a session
        client.active_sessions["test-session"] = "Previous prompt"

        response = client.send_message("test-session", "New message")

        assert response == "Response from Jules"

        # Verify correct API call was made
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://jules.googleapis.com/v1alpha/sessions/test-session:sendMessage"
        assert call_args[1]["json"]["prompt"] == "New message"

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_send_message_handles_http_error(self, mock_post, mock_get_config):
        """Test that send_message raises error on HTTP failure."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock error response
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response

        client = JulesClient()
        client.active_sessions["test-session"] = "Prompt"

        with pytest.raises(RuntimeError, match="Failed to send message to Jules session"):
            client.send_message("test-session", "Message")

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_send_message_handles_network_error(self, mock_post, mock_get_config):
        """Test that send_message raises error on network failure."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock network error
        mock_post.side_effect = requests.exceptions.Timeout("Timeout")

        client = JulesClient()
        client.active_sessions["test-session"] = "Prompt"

        with pytest.raises(RuntimeError, match="Failed to send message to Jules session"):
            client.send_message("test-session", "Message")

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.delete")
    def test_end_session_success(self, mock_delete, mock_get_config):
        """Test ending a session successfully."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock the DELETE response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_delete.return_value = mock_response

        client = JulesClient()
        client.active_sessions["test-session"] = "Prompt"

        result = client.end_session("test-session")

        assert result is True
        assert "test-session" not in client.active_sessions

        # Verify correct API call was made
        mock_delete.assert_called_once()
        call_args = mock_delete.call_args
        assert call_args[0][0] == "https://jules.googleapis.com/v1alpha/sessions/test-session"

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.delete")
    def test_end_session_failure(self, mock_delete, mock_get_config):
        """Test that end_session returns False on failure."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock error response
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_delete.return_value = mock_response

        client = JulesClient()
        client.active_sessions["test-session"] = "Prompt"

        result = client.end_session("test-session")

        assert result is False
        # Session should still be tracked on failure
        assert "test-session" in client.active_sessions

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.delete")
    def test_end_session_removes_from_active_sessions_on_success(self, mock_delete, mock_get_config):
        """Test that session is removed from active_sessions only on success."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_delete.return_value = mock_response

        client = JulesClient()
        client.active_sessions["session1"] = "Prompt1"
        client.active_sessions["session2"] = "Prompt2"

        # End session1 successfully
        client.end_session("session1")

        # session1 should be removed, session2 should remain
        assert "session1" not in client.active_sessions
        assert "session2" in client.active_sessions

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.delete")
    def test_end_session_handles_network_error(self, mock_delete, mock_get_config):
        """Test that end_session handles network errors gracefully."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock network error
        mock_delete.side_effect = requests.exceptions.ConnectionError("Network error")

        client = JulesClient()
        client.active_sessions["test-session"] = "Prompt"

        result = client.end_session("test-session")

        assert result is False

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    @patch("requests.Session.delete")
    def test_run_llm_cli_single_run(self, mock_delete, mock_post, mock_get_config):
        """Test _run_llm_cli starts session, sends message, and ends session."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # Mock responses for start_session, send_message, end_session
        mock_start_response = Mock()
        mock_start_response.status_code = 201
        mock_start_response.json.return_value = {"sessionId": "abc123"}

        mock_send_response = Mock()
        mock_send_response.status_code = 200
        mock_send_response.json.return_value = {"response": "Response from Jules"}

        mock_end_response = Mock()
        mock_end_response.status_code = 200

        mock_post.side_effect = [
            mock_start_response,  # start_session
            mock_send_response,  # send_message
        ]
        mock_delete.return_value = mock_end_response  # end_session

        client = JulesClient()
        response = client._run_llm_cli("Test prompt")

        assert response == "Response from Jules"
        assert "abc123" not in client.active_sessions  # Session should be cleaned up

        # Should have called post 2 times: start, send, and delete 1 time
        assert mock_post.call_count == 2
        assert mock_delete.call_count == 1

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    @patch("requests.Session.delete")
    def test_run_llm_cli_ensures_session_cleanup_on_error(self, mock_delete, mock_post, mock_get_config):
        """Test that _run_llm_cli cleans up session even when send_message fails."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        # start_session succeeds, send_message fails, end_session should still be called
        mock_start_response = Mock()
        mock_start_response.status_code = 201
        mock_start_response.json.return_value = {"sessionId": "abc123"}

        mock_send_response = Mock()
        mock_send_response.status_code = 500
        mock_send_response.text = "Internal Server Error"

        mock_end_response = Mock()
        mock_end_response.status_code = 200

        mock_post.side_effect = [
            mock_start_response,  # start_session
            mock_send_response,  # send_message (fails)
        ]
        mock_delete.return_value = mock_end_response  # end_session

        client = JulesClient()

        with pytest.raises(RuntimeError):
            client._run_llm_cli("Test prompt")

        # Session should be cleaned up despite the error
        assert "abc123" not in client.active_sessions

        # Should have called post 2 times (start and send), and delete 1 time
        assert mock_post.call_count == 2
        assert mock_delete.call_count == 1

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_close(self, mock_get_config):
        """Test that close() properly closes the HTTP session."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.session.close = Mock()  # Mock the close method

        client.close()

        client.session.close.assert_called_once()

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_check_mcp_server_configured(self, mock_get_config):
        """Test that Jules does not support MCP servers."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()

        # Jules does not support MCP servers
        assert client.check_mcp_server_configured("graphrag") is False
        assert client.check_mcp_server_configured("mcp-pdb") is False

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_add_mcp_server_config(self, mock_get_config):
        """Test that Jules does not support adding MCP server configuration."""
        # Mock config
        mock_config = Mock()
        mock_backend_config = Mock()
        mock_backend_config.options = []
        mock_backend_config.options_for_noedit = []
        mock_backend_config.api_key = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()

        # Jules does not support MCP server configuration
        assert client.add_mcp_server_config("graphrag", "uv", []) is False
