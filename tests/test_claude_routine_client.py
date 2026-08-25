"""
Unit tests for ClaudeRoutineClient.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from auto_coder.claude_routine_client import ClaudeRoutineClient
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestClaudeRoutineClient:
    """Test suite for ClaudeRoutineClient."""

    @pytest.fixture
    def mock_backend_config(self):
        """Create mock configuration for claude-routine."""
        config = LLMBackendConfiguration()
        routine_config = BackendConfig(
            name="claude-opus-routine",
            backend_type="claude-routine",
            url="https://api.anthropic.com/v1/claude_code/routines/trig_123/fire",
            claude_code_routine_token="test-routine-token",
            options=["--dangerously-skip-permissions"],
        )
        config.backends["claude-opus-routine"] = routine_config
        return config

    def test_init_loads_config(self, mock_backend_config):
        """Test that initialization loads URL and token from config."""
        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            assert client.backend_name == "claude-opus-routine"
            assert client.url == "https://api.anthropic.com/v1/claude_code/routines/trig_123/fire"
            assert client.token == "test-routine-token"
            assert client.session.headers["Authorization"] == "Bearer test-routine-token"
            assert client.session.headers["anthropic-beta"] == "experimental-cc-routine-2026-04-01"

    def test_fire_routine_success(self, mock_backend_config):
        """Test successfully firing a Claude routine."""
        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "type": "routine_fire",
                "claude_code_session_id": "session_01HJKLMNOPQRSTUVWXYZ",
                "claude_code_session_url": "https://claude.ai/code/session_01HJKLMNOPQRSTUVWXYZ",
            }

            with (
                patch("auto_coder.claude_routine_client.check_claude_usage_or_raise"),
                patch.object(client.session, "post", return_value=mock_response) as mock_post,
            ):
                session_id, session_url = client.fire_routine(
                    "Test prompt",
                    repo_name="owner/repo",
                    base_branch="main",
                    title="Test title",
                )

                assert session_id == "session_01HJKLMNOPQRSTUVWXYZ"
                assert session_url == "https://claude.ai/code/session_01HJKLMNOPQRSTUVWXYZ"
                assert session_id in client.active_sessions

                mock_post.assert_called_once()
                args, kwargs = mock_post.call_args
                assert args[0] == "https://api.anthropic.com/v1/claude_code/routines/trig_123/fire"
                assert kwargs["json"] == {"text": "Test prompt"}

    def test_fire_routine_http_error(self, mock_backend_config):
        """Test fire_routine raising RuntimeError on HTTP error."""
        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_response.text = "Unauthorized"

            with (
                patch("auto_coder.claude_routine_client.check_claude_usage_or_raise"),
                patch.object(client.session, "post", return_value=mock_response),
            ):
                with pytest.raises(RuntimeError) as exc_info:
                    client.fire_routine("Test prompt")

                assert "HTTP 401: Unauthorized" in str(exc_info.value)

    def test_fire_routine_missing_url(self):
        """Test fire_routine with no URL configured."""
        config = LLMBackendConfiguration()
        routine_config = BackendConfig(
            name="empty-routine",
            backend_type="claude-routine",
        )
        config.backends["empty-routine"] = routine_config

        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=config):
            client = ClaudeRoutineClient("empty-routine")
            with (
                patch("auto_coder.claude_routine_client.check_claude_usage_or_raise"),
                pytest.raises(ValueError) as exc_info,
            ):
                client.fire_routine("Test prompt")
            assert "No URL configured" in str(exc_info.value)

    def test_start_session_and_run_llm_cli(self, mock_backend_config):
        """Test start_session and _run_llm_cli helpers."""
        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "type": "routine_fire",
                "claude_code_session_id": "session_456",
                "claude_code_session_url": "https://claude.ai/code/session_456",
            }

            with (
                patch("auto_coder.claude_routine_client.check_claude_usage_or_raise"),
                patch.object(client.session, "post", return_value=mock_response),
            ):
                sess_id = client.start_session("Fix bug", "owner/repo", "main")
                assert sess_id == "session_456"

                cli_out = client._run_llm_cli("Analyze code")
                assert "session_456" in cli_out

    def test_close_and_mcp_checks(self, mock_backend_config):
        """Test close and MCP check methods."""
        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            assert client.check_mcp_server_configured("test") is False
            assert client.add_mcp_server_config("test", "cmd", []) is False

            client.close()

    def test_fire_routine_raises_when_usage_cannot_be_retrieved(self, mock_backend_config):
        """Test fire_routine raises AutoCoderUsageLimitError when usage data cannot be retrieved."""
        from auto_coder.exceptions import AutoCoderUsageLimitError

        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            with (
                patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=None),
                patch.object(client.session, "post") as mock_post,
            ):
                with pytest.raises(AutoCoderUsageLimitError) as exc_info:
                    client.fire_routine("Test prompt")

                assert "Claude usage data could not be retrieved" in str(exc_info.value)
                mock_post.assert_not_called()

    def test_fire_routine_raises_usage_limit_error(self, mock_backend_config):
        """Test fire_routine raises AutoCoderUsageLimitError when usage limit is low."""
        from auto_coder.exceptions import AutoCoderUsageLimitError

        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            with patch(
                "auto_coder.claude_routine_client.check_claude_usage_or_raise",
                side_effect=AutoCoderUsageLimitError("5-hour limit remaining 15.0% <= threshold 20.0%"),
            ) as mock_check:
                with pytest.raises(AutoCoderUsageLimitError) as exc_info:
                    client.fire_routine("Test prompt")

                assert "5-hour limit remaining 15.0%" in str(exc_info.value)
                mock_check.assert_called_once_with(token="test-routine-token", backend_name="claude-opus-routine")

    def test_fire_routine_raises_usage_limit_on_http_429(self, mock_backend_config):
        """Test fire_routine raises AutoCoderUsageLimitError when HTTP response is 429."""
        from auto_coder.exceptions import AutoCoderUsageLimitError

        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.text = '{"error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}}'

            with (
                patch("auto_coder.claude_routine_client.check_claude_usage_or_raise"),
                patch.object(client.session, "post", return_value=mock_response),
            ):
                with pytest.raises(AutoCoderUsageLimitError) as exc_info:
                    client.fire_routine("Test prompt")

                assert "rate limit exceeded" in str(exc_info.value).lower()

    def test_continue_if_paused_skips_when_usage_insufficient(self, mock_backend_config):
        """Test continue_if_paused returns False when quota is insufficient."""
        from auto_coder.claude_usage_checker import ClaudeUsageQuota

        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            insufficient_quota = ClaudeUsageQuota(is_quota_insufficient=True, reason="5-hour limit remaining 10.0%")
            with patch("auto_coder.claude_routine_client.check_claude_usage", return_value=insufficient_quota):
                res = client.continue_if_paused("session_123")
                assert res is False

    def test_continue_if_paused_skips_when_usage_cannot_be_retrieved(self, mock_backend_config):
        """Test continue_if_paused returns False when usage cannot be retrieved."""
        with patch("auto_coder.claude_routine_client.get_llm_config", return_value=mock_backend_config):
            client = ClaudeRoutineClient("claude-opus-routine")

            with (
                patch("auto_coder.claude_usage_checker.fetch_claude_usage_data", return_value=None),
                patch("auto_coder.claude_routine_client.CommandExecutor.run_command") as mock_run,
            ):
                res = client.continue_if_paused("session_123")
                assert res is False
                mock_run.assert_not_called()
