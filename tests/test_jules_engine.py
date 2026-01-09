import unittest
from unittest.mock import MagicMock, patch

from auto_coder.jules_engine import check_and_resume_or_archive_sessions


class TestJulesEngine(unittest.TestCase):
    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_resume_failed_session(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s1", "state": "FAILED"}]
        mock_load_state.return_value = {"s1": 1}  # Should be cleared

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.send_message.assert_called_once_with("s1", "ok")
        mock_save_state.assert_called_once_with({})  # s1 removed

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_resume_completed_session_no_pr_first_attempt(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s2", "state": "COMPLETED", "outputs": {}}]
        mock_load_state.return_value = {}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.send_message.assert_called_once_with("s2", "ok")
        mock_save_state.assert_called_once_with({"s2": 1})

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_resume_completed_session_no_pr_second_attempt(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s2", "state": "COMPLETED", "outputs": {}}]
        mock_load_state.return_value = {"s2": 1}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.send_message.assert_called_once_with("s2", "ok")
        mock_save_state.assert_called_once_with({"s2": 2})

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_resume_completed_session_no_pr_force_pr(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s2", "state": "COMPLETED", "outputs": {}}]
        mock_load_state.return_value = {"s2": 2}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.send_message.assert_called_once_with("s2", "Please create a PR with the current code")
        mock_save_state.assert_called_once_with({"s2": 0})

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_archive_completed_session_pr_closed(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s3", "state": "COMPLETED", "outputs": {"pullRequest": {"number": 123, "repository": {"name": "owner/repo"}}}}]
        mock_load_state.return_value = {"s3": 2}  # Should be cleared

        mock_github_client = mock_github_client_cls.get_instance.return_value
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "closed"
        mock_pr.merged = False
        mock_repo.get_pull.return_value = mock_pr
        mock_github_client.get_repository.return_value = mock_repo

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_github_client.get_repository.assert_called_with("owner/repo")
        mock_repo.get_pull.assert_called_with(123)
        mock_jules_client.archive_session.assert_called_once_with("s3")
        mock_save_state.assert_called_once_with({})  # s3 removed

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_archive_completed_session_pr_merged(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s4", "state": "COMPLETED", "outputs": {"pullRequest": "https://github.com/owner/repo/pull/456"}}]
        mock_load_state.return_value = {}

        mock_github_client = mock_github_client_cls.get_instance.return_value
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "closed"
        mock_pr.merged = True
        mock_repo.get_pull.return_value = mock_pr
        mock_github_client.get_repository.return_value = mock_repo

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.archive_session.assert_called_once_with("s4")

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_do_nothing_completed_session_pr_open(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s5", "state": "COMPLETED", "outputs": {"pullRequest": {"number": 789, "repository": {"full_name": "owner/repo"}}}}]
        mock_load_state.return_value = {}

        mock_github_client = mock_github_client_cls.get_instance.return_value
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.state = "open"
        mock_repo.get_pull.return_value = mock_pr
        mock_github_client.get_repository.return_value = mock_repo

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_github_client.get_repository.assert_called_with("owner/repo")
        mock_repo.get_pull.assert_called_with(789)
        mock_jules_client.archive_session.assert_not_called()
        mock_jules_client.send_message.assert_not_called()

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_github_client_not_initialized(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s6", "state": "COMPLETED", "outputs": {"pullRequest": "https://github.com/owner/repo/pull/101"}}]
        mock_load_state.return_value = {}

        mock_github_client_cls.get_instance.side_effect = ValueError("Not initialized")

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.archive_session.assert_not_called()
        mock_jules_client.send_message.assert_not_called()
