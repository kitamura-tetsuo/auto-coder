"""
Tests for Jules client functionality.
"""

import json
import time
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
        mock_backend_config.base_url = None
        mock_config.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config

        client = JulesClient()
        assert client.backend_name == "jules"
        assert client.timeout is None
        assert len(client.active_sessions) == 0
        assert client.session is not None
        assert client.base_url == "https://jules.googleapis.com/v1alpha"

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_get_session_success(self, mock_get_config):
        """Test getting a session successfully."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"name": "session1", "state": "RUNNING"}
        client.session.get.return_value = mock_response

        session = client.get_session("session1")
        assert session == {"name": "session1", "state": "RUNNING"}
        client.session.get.assert_called_with("https://jules.googleapis.com/v1alpha/sessions/session1", timeout=None)

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_get_session_not_found(self, mock_get_config):
        """Test getting a session that doesn't exist."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.session = Mock()
        mock_response = Mock()
        mock_response.status_code = 404
        client.session.get.return_value = mock_response

        session = client.get_session("session1")
        assert session is None

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_process_session_status_resume_failed(self, mock_get_config):
        """Test resuming a failed session."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.send_message = Mock()
        
        session = {"name": "projects/p/locations/l/sessions/s1", "state": "FAILED"}
        retry_state = {"s1": 1}

        changed = client.process_session_status(session, retry_state)
        
        assert changed is True
        assert "s1" not in retry_state
        client.send_message.assert_called_with("s1", "ok")

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_process_session_status_approve_plan(self, mock_get_config):
        """Test approving a plan."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.approve_plan = Mock(return_value=True)

        session = {"name": "projects/p/locations/l/sessions/s1", "state": "AWAITING_PLAN_APPROVAL"}
        retry_state = {}

        changed = client.process_session_status(session, retry_state)

        assert changed is True
        client.approve_plan.assert_called_with("s1")

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_process_session_status_resume_no_pr(self, mock_get_config):
        """Test resuming a completed session without PR."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.send_message = Mock()

        session = {"name": "projects/p/locations/l/sessions/s1", "state": "COMPLETED", "outputs": {}}
        retry_state = {}

        changed = client.process_session_status(session, retry_state)

        assert changed is True
        assert retry_state["s1"] == 1
        client.send_message.assert_called_with("s1", "ok")

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_process_session_status_force_pr(self, mock_get_config):
        """Test forcing PR creation after retries."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.send_message = Mock()

        session = {"name": "projects/p/locations/l/sessions/s1", "state": "COMPLETED", "outputs": {}}
        retry_state = {"s1": 2}

        changed = client.process_session_status(session, retry_state)

        assert changed is True
        assert retry_state["s1"] == 0
        client.send_message.assert_called_with("s1", "Please create a PR with the current code")

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_process_session_status_archive_pr(self, mock_get_config):
        """Test archiving session when PR is closed/merged."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.archive_session = Mock(return_value=True)
        
        mock_github_client = Mock()
        mock_repo = Mock()
        mock_pr = Mock()
        mock_pr.state = "closed"
        mock_pr.merged = True
        mock_repo.get_pull.return_value = mock_pr
        mock_github_client.get_repository.return_value = mock_repo

        session = {
            "name": "projects/p/locations/l/sessions/s1",
            "state": "COMPLETED",
            "outputs": {"pullRequest": {"number": 123, "repository": {"name": "owner/repo"}}}
        }
        retry_state = {"s1": 1}

        changed = client.process_session_status(session, retry_state, mock_github_client)

        assert changed is True
        assert "s1" not in retry_state
        client.archive_session.assert_called_with("s1")

    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_process_session_status_auto_merge(self, mock_get_config):
        """Test auto-merging PR when merge_pr is True."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.archive_session = Mock(return_value=True)
        
        mock_github_client = Mock()
        mock_repo = Mock()
        mock_pr = Mock()
        mock_pr.state = "open"
        mock_pr.merged = False
        # auto-merge success
        mock_pr.merge.return_value = Mock(merged=True)
        
        mock_repo.get_pull.return_value = mock_pr
        mock_github_client.get_repository.return_value = mock_repo

        session = {
            "name": "projects/p/locations/l/sessions/s1",
            "state": "COMPLETED",
            "outputs": {"pullRequest": {"number": 123, "repository": {"name": "owner/repo"}}}
        }
        retry_state = {}

        # Call with merge_pr=True
        changed = client.process_session_status(session, retry_state, mock_github_client, merge_pr=True)

        assert changed is True
        mock_pr.merge.assert_called_once()
        # Should proceed to archive since it's now merged (in our mocked world, we simulate post-merge state)
        # However, the logic re-fetches the PR. So we need to ensure the second fetch returns merged=True
        # but mock_repo.get_pull returns the same mock object, so we just need to ensure the mock object
        # has state="closed" or merged=True for the archive check.
        # But wait, the code calls pr.merge(), then if success, sets state_changed=True.
        # Then it continues to check if pr.state == "closed".
        # If the mock pr.state is still "open", it won't archive yet (until next poll).
        # Let's verify merge was called.
        
    @patch("src.auto_coder.jules_client.get_llm_config")
    def test_process_session_status_no_auto_merge(self, mock_get_config):
        """Test that PR is NOT merged when merge_pr is False."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        
        mock_github_client = Mock()
        mock_repo = Mock()
        mock_pr = Mock()
        mock_repo.get_pull.return_value = mock_pr
        mock_github_client.get_repository.return_value = mock_repo
        
        mock_pr.state = "open"
        mock_pr.merged = False

        session = {
            "name": "projects/p/locations/l/sessions/s1",
            "state": "COMPLETED",
            "outputs": {"pullRequest": {"number": 123, "repository": {"name": "owner/repo"}}}
        }
        retry_state = {}

        # Call with merge_pr=False (default)
        client.process_session_status(session, retry_state, mock_github_client, merge_pr=False)

        mock_pr.merge.assert_not_called()

    @patch("src.auto_coder.jules_client.get_llm_config")
    @patch("src.auto_coder.jules_client.time.sleep")
    def test_run_llm_cli_polling(self, mock_sleep, mock_get_config):

        """Test _run_llm_cli with polling loop."""
        mock_config = Mock()
        mock_config.get_backend_config.return_value = Mock(options=[], options_for_noedit=[], api_key=None, base_url=None)
        mock_get_config.return_value = mock_config

        client = JulesClient()
        client.start_session = Mock(return_value="session1")
        client.send_message = Mock(return_value="Initial response")
        client.get_session = Mock()
        client.process_session_status = Mock()
        
        # Sequence of polling: RUNNING -> COMPLETED (archive called) -> ARCHIVED
        client.get_session.side_effect = [
            {"name": "session1", "state": "RUNNING"},
            {"name": "session1", "state": "COMPLETED"}, # process_session_status might archive it here
            {"name": "session1", "state": "ARCHIVED"},
        ]
        
        # We need to mock process_session_status to not actually do anything complex, 
        # but the loop checks 'state' of session from get_session. 
        # If state is ARCHIVED, it returns.
        
        with patch("src.auto_coder.github_client.GitHubClient") as mock_gh_cls:
             response = client._run_llm_cli("prompt")

        assert response == "Session session1 completed and archived."
        assert client.get_session.call_count == 3
