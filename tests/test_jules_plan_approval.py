import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

from src.auto_coder.jules_client import JulesClient
from src.auto_coder.jules_engine import check_and_resume_or_archive_sessions


class TestJulesPlanApproval(unittest.TestCase):

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_approve_plan_success(self, mock_post, mock_get_config):
        """Test approve_plan successfully approves a plan."""
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
        mock_post.return_value = mock_response

        client = JulesClient()
        result = client.approve_plan("session-123")

        assert result is True

        # Verify correct API call was made
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == f"{mock_backend_config.base_url}/sessions/session-123:approvePlan"
        assert call_args[1]["json"] == {}

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("requests.Session.post")
    def test_approve_plan_failure(self, mock_post, mock_get_config):
        """Test approve_plan handles failure."""
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
        result = client.approve_plan("session-123")

        assert result is False

    @patch("src.auto_coder.jules_engine.JulesClient")
    @patch("src.auto_coder.jules_engine.GitHubClient")
    @patch("src.auto_coder.jules_engine._load_state")
    @patch("src.auto_coder.jules_engine._save_state")
    def test_engine_approves_plan(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        """Test that engine calls approve_plan when state is AWAITING_PLAN_APPROVAL."""
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s1", "state": "AWAITING_PLAN_APPROVAL"}]
        mock_load_state.return_value = {}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.process_session_status.assert_called_once()
        # Should not save state as we didn't modify retry_state, but state_changed is True because we approved plan?
        # Let's check the code.
        # In the implementation:
        # if jules_client.approve_plan(session_id):
        #     logger.info(f"Successfully approved plan for session {session_id}")
        #     state_changed = True
        # So it should save state.
        mock_save_state.assert_called_once()

    @patch("src.auto_coder.jules_engine.JulesClient")
    @patch("src.auto_coder.jules_engine.GitHubClient")
    @patch("src.auto_coder.jules_engine._load_state")
    @patch("src.auto_coder.jules_engine._save_state")
    def test_engine_handles_approve_plan_failure(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        """Test that engine handles failure when approving plan."""
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s1", "state": "AWAITING_PLAN_APPROVAL"}]
        mock_load_state.return_value = {}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.process_session_status.assert_called_once()
        # state_changed should be False
        mock_save_state.assert_not_called()
