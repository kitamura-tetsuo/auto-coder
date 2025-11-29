"""Tests for jules_client module."""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.jules_client import JulesClient
from src.auto_coder.llm_client_base import LLMClientBase


class TestJulesClient:
    """Test cases for JulesClient class."""

    def test_jules_client_inherits_from_llm_client_base(self):
        """Test that JulesClient inherits from LLMClientBase."""
        assert issubclass(JulesClient, LLMClientBase)

    def test_jules_client_init_default(self):
        """Test JulesClient initialization with default parameters."""
        client = JulesClient()
        # Client should initialize without error
        assert isinstance(client, JulesClient)

    def test_jules_client_init_with_backend_name(self):
        """Test JulesClient initialization with backend_name parameter."""
        client = JulesClient(backend_name="test_backend")
        # Client should initialize with backend_name
        assert isinstance(client, JulesClient)
        assert hasattr(client, "backend_name")

    def test_jules_client_run_llm_cli_raises_not_implemented(self):
        """Test that _run_llm_cli raises NotImplementedError."""
        client = JulesClient()

        with pytest.raises(NotImplementedError) as excinfo:
            client._run_llm_cli("test prompt")

        assert "JulesClient does not run LLM directly" in str(excinfo.value)

    def test_jules_client_check_mcp_server_configured_returns_false(self):
        """Test that check_mcp_server_configured returns False."""
        client = JulesClient()

        result = client.check_mcp_server_configured("graphrag")

        assert result is False

    def test_jules_client_check_mcp_server_configured_various_servers(self):
        """Test check_mcp_server_configured with various server names."""
        client = JulesClient()

        # Should return False for any server name
        assert client.check_mcp_server_configured("graphrag") is False
        assert client.check_mcp_server_configured("mcp-pdb") is False
        assert client.check_mcp_server_configured("unknown") is False
        assert client.check_mcp_server_configured("") is False

    def test_jules_client_add_mcp_server_config_returns_false(self):
        """Test that add_mcp_server_config returns False."""
        client = JulesClient()

        result = client.add_mcp_server_config("graphrag", "uv", ["run", "main.py"])

        assert result is False

    def test_jules_client_add_mcp_server_config_various_params(self):
        """Test add_mcp_server_config with various parameters."""
        client = JulesClient()

        # Should always return False regardless of parameters
        assert client.add_mcp_server_config("graphrag", "uv", ["run"]) is False
        assert client.add_mcp_server_config("mcp-pdb", "/path/script.sh", []) is False
        assert client.add_mcp_server_config("", "", []) is False
        assert client.add_mcp_server_config("test", "command", ["arg1", "arg2"]) is False

    def test_jules_client_is_abstract_base_subclass(self):
        """Test that JulesClient properly implements the ABC interface."""
        client = JulesClient()

        # Should have abstract methods from LLMClientBase
        assert hasattr(client, "_run_llm_cli")
        assert hasattr(client, "check_mcp_server_configured")
        assert hasattr(client, "add_mcp_server_config")
        assert hasattr(client, "ensure_mcp_server_configured")
        assert hasattr(client, "switch_to_default_model")
        assert hasattr(client, "close")

    @patch("requests.post")
    def test_start_session_success(self, mock_post):
        """Test start_session returns session ID on successful API call."""
        client = JulesClient()

        # Mock successful API response
        mock_response = Mock()
        mock_response.json.return_value = {"session_id": "test-session-123"}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        session_id = client.start_session("Test prompt")

        assert session_id == "test-session-123"
        mock_post.assert_called_once()

    @patch("requests.post")
    def test_start_session_with_id_field(self, mock_post):
        """Test start_session handles 'id' field in response."""
        client = JulesClient()

        # Mock successful API response with 'id' instead of 'session_id'
        mock_response = Mock()
        mock_response.json.return_value = {"id": "test-session-456"}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        session_id = client.start_session("Test prompt")

        assert session_id == "test-session-456"

    @patch("requests.post")
    def test_start_session_api_error(self, mock_post):
        """Test start_session raises exception on API error."""
        client = JulesClient()

        # Mock API error response
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = Exception("API Error")
        mock_post.return_value = mock_response

        with pytest.raises(Exception):
            client.start_session("Test prompt")

    @patch("requests.post")
    def test_start_session_no_session_id(self, mock_post):
        """Test start_session raises exception when session_id is missing."""
        client = JulesClient()

        # Mock response without session_id
        mock_response = Mock()
        mock_response.json.return_value = {"status": "ok"}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        with pytest.raises(ValueError):
            client.start_session("Test prompt")

    @patch("requests.post")
    def test_send_message_success(self, mock_post):
        """Test send_message returns response on successful API call."""
        client = JulesClient()

        # Mock successful API response
        mock_response = Mock()
        mock_response.json.return_value = {"response": "Test response"}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        response = client.send_message("test-session-123", "Test message")

        assert response == {"response": "Test response"}

    @patch("requests.post")
    def test_send_message_api_error(self, mock_post):
        """Test send_message raises exception on API error."""
        client = JulesClient()

        # Mock API error response
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = Exception("API Error")
        mock_post.return_value = mock_response

        with pytest.raises(Exception):
            client.send_message("test-session-123", "Test message")

    def test_init_with_custom_base_url(self, monkeypatch):
        """Test JulesClient uses custom base URL from environment variable."""
        monkeypatch.setenv("JULES_API_URL", "http://custom-jules:9000")

        client = JulesClient()

        assert client.base_url == "http://custom-jules:9000"

    def test_init_with_default_base_url(self):
        """Test JulesClient uses default base URL when env var not set."""
        client = JulesClient()

        assert client.base_url == "http://localhost:8000"
