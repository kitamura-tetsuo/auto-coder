import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from auto_coder.jules_engine import (
    check_and_restart_recurrent_jules_task_for_pr,
    check_and_resume_or_archive_sessions,
    check_and_start_recurrent_jules_tasks,
)


class TestJulesEngine(unittest.TestCase):
    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_resume_failed_session(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s1", "state": "FAILED", "automationMode": "AUTO_CREATE_PR"}]
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
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s2", "state": "COMPLETED", "outputs": {}, "automationMode": "AUTO_CREATE_PR"}]
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
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s2", "state": "COMPLETED", "outputs": {}, "automationMode": "AUTO_CREATE_PR"}]
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
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s2", "state": "COMPLETED", "outputs": {}, "automationMode": "AUTO_CREATE_PR"}]
        mock_load_state.return_value = {"s2": 5}

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
        # mock_repo = MagicMock()
        # mock_pr = MagicMock()
        # mock_pr.state = "closed"
        # mock_pr.merged = False
        # mock_repo.get_pull.return_value = mock_pr
        # mock_github_client.get_repository.return_value = mock_repo

        # New usage: client.get_pull_request(repo, number) -> dict
        mock_github_client.get_pull_request.return_value = {"state": "closed", "merged": False}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        # mock_github_client.get_repository.assert_called_with("owner/repo")
        # mock_repo.get_pull.assert_called_with(123)
        mock_github_client.get_pull_request.assert_called_with("owner/repo", 123)
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
        mock_github_client.get_pull_request.return_value = {"state": "closed", "merged": True}

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
        mock_github_client.get_pull_request.return_value = {"state": "open"}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        # mock_github_client.get_repository.assert_called_with("owner/repo")
        # mock_repo.get_pull.assert_called_with(789)
        mock_github_client.get_pull_request.assert_called_with("owner/repo", 789)
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

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    @patch("auto_coder.jules_engine.datetime")
    def test_resume_in_progress_session_timed_out(self, mock_datetime, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value

        # 6 minutes ago
        now = datetime(2024, 1, 1, 12, 10, 0, tzinfo=timezone.utc)
        six_mins_ago = (now - timedelta(minutes=6)).isoformat()

        mock_datetime.now.return_value = now

        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s7", "state": "IN_PROGRESS", "updateTime": six_mins_ago, "automationMode": "AUTO_CREATE_PR"}]
        mock_load_state.return_value = {}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.send_message.assert_called_once_with("s7", "ok")

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_do_not_resume_non_auto_create_pr_session(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {"name": "projects/p/locations/l/sessions/s8", "state": "FAILED", "automationMode": "NONE"},
            {"name": "projects/p/locations/l/sessions/s9", "state": "AWAITING_USER_FEEDBACK", "automationMode": "NONE"},
            {"name": "projects/p/locations/l/sessions/s_comment_none", "state": "AWAITING_COMMENT", "automationMode": "NONE"},
            {"name": "projects/p/locations/l/sessions/s_comments_none", "state": "AWAITING_COMMENTS", "automationMode": "NONE"},
            {"name": "projects/p/locations/l/sessions/s10", "state": "COMPLETED", "outputs": {}, "automationMode": "NONE"},
        ]
        mock_load_state.return_value = {}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.send_message.assert_not_called()
        mock_save_state.assert_not_called()

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_resume_awaiting_comments_session(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {"name": "projects/p/locations/l/sessions/s_comment", "state": "AWAITING_COMMENT", "automationMode": "AUTO_CREATE_PR"},
            {"name": "projects/p/locations/l/sessions/s_comments", "state": "AWAITING_COMMENTS", "automationMode": "AUTO_CREATE_PR"},
        ]
        mock_load_state.return_value = {}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        self.assertEqual(mock_jules_client.send_message.call_count, 2)
        mock_jules_client.send_message.assert_any_call("s_comment", "ok")
        mock_jules_client.send_message.assert_any_call("s_comments", "ok")
        mock_save_state.assert_called_once_with({"s_comment": 1, "s_comments": 1})

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_resume_custom_awaiting_states(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {"name": "projects/p/locations/l/sessions/s_custom_input", "state": "AWAITING_USER_INPUT", "automationMode": "AUTO_CREATE_PR"},
            {"name": "projects/p/locations/l/sessions/s_custom_feedback", "state": "AWAITING_USER_FEEDBACK", "automationMode": "AUTO_CREATE_PR"},
        ]
        mock_load_state.return_value = {}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        self.assertEqual(mock_jules_client.send_message.call_count, 2)
        mock_jules_client.send_message.assert_any_call("s_custom_input", "ok")
        mock_jules_client.send_message.assert_any_call("s_custom_feedback", "ok")
        mock_save_state.assert_called_once_with({"s_custom_input": 1, "s_custom_feedback": 1})

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_resume_session_missing_automation_mode(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {"name": "projects/p/locations/l/sessions/s_missing_mode", "state": "AWAITING_USER_FEEDBACK"},
        ]
        mock_load_state.return_value = {}

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        mock_jules_client.send_message.assert_called_once_with("s_missing_mode", "ok")
        mock_save_state.assert_called_once_with({"s_missing_mode": 1})

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    def test_check_and_start_recurrent_jules_tasks_starts_new_session(self, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        # Mock file contents with valid jules, recurrent tags and name
        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = []  # No active sessions

        # Execute
        check_and_start_recurrent_jules_tasks("owner/repo")

        # Verify
        mock_jules_client.start_session.assert_called_once()
        args, kwargs = mock_jules_client.start_session.call_args
        self.assertIn("This is a recurrent task prompt.", kwargs["prompt"])
        self.assertEqual(kwargs["repo_name"], "owner/repo")
        self.assertEqual(kwargs["title"], "auto improvement with demo site")

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    def test_check_and_start_recurrent_jules_tasks_already_running(self, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        # Mock file contents with valid jules, recurrent tags and name
        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        # Session already contains the same prompt and name
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {
                "name": "projects/p/locations/l/sessions/s_active",
                "prompt": """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt.""",
            }
        ]

        # Execute
        check_and_start_recurrent_jules_tasks("owner/repo")

        # Verify
        mock_jules_client.start_session.assert_not_called()

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    def test_check_and_start_recurrent_jules_tasks_completed_and_merged(self, mock_github_client_cls, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        # Mock file contents with valid jules, recurrent tags and name
        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        # Session is completed and has a PR url
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {
                "name": "projects/p/locations/l/sessions/s_completed",
                "state": "COMPLETED",
                "outputs": {"pullRequest": "https://github.com/owner/repo/pull/123"},
                "prompt": '---\ntags: [jules, recurrent, auto-improvement]\nname: ["auto improvement with demo site"]\n---\nThis is a recurrent task prompt.',
            }
        ]

        mock_github_client = mock_github_client_cls.get_instance.return_value
        mock_github_client.get_pull_request.return_value = {"state": "closed", "merged": True}

        # Execute
        check_and_start_recurrent_jules_tasks("owner/repo")

        # Verify
        mock_github_client.get_pull_request.assert_called_once_with("owner/repo", 123)
        mock_jules_client.start_session.assert_called_once()

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    def test_check_and_start_recurrent_jules_tasks_completed_but_not_merged(self, mock_github_client_cls, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        # Mock file contents
        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        # Session is completed and has a PR dict
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {
                "name": "projects/p/locations/l/sessions/s_completed",
                "state": "COMPLETED",
                "outputs": {"pullRequest": {"number": 123, "repository": {"name": "owner/repo"}}},
                "prompt": '---\ntags: [jules, recurrent, auto-improvement]\nname: ["auto improvement with demo site"]\n---\nThis is a recurrent task prompt.',
            }
        ]

        mock_github_client = mock_github_client_cls.get_instance.return_value
        mock_github_client.get_pull_request.return_value = {"state": "open", "merged": False}

        # Execute
        check_and_start_recurrent_jules_tasks("owner/repo")

        # Verify
        mock_github_client.get_pull_request.assert_called_once_with("owner/repo", 123)
        mock_jules_client.start_session.assert_not_called()

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    def test_check_and_restart_recurrent_jules_task_for_pr(self, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        # Mock file contents with valid jules, recurrent tags and name
        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.get_session.return_value = {
            "name": "projects/p/locations/l/sessions/s_merged",
            "prompt": """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt.""",
        }

        # Execute
        check_and_restart_recurrent_jules_task_for_pr("owner/repo", 123, "s_merged")

        # Verify
        mock_jules_client.get_session.assert_called_once_with("s_merged")
        mock_jules_client.start_session.assert_called_once()
        args, kwargs = mock_jules_client.start_session.call_args
        self.assertEqual(kwargs["title"], "auto improvement with demo site")

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_session_error_resilience(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup: Two sessions. s1 will raise an error on send_message, s2 should still be processed.
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {"name": "projects/p/locations/l/sessions/s1", "state": "FAILED", "automationMode": "AUTO_CREATE_PR"},
            {"name": "projects/p/locations/l/sessions/s2", "state": "FAILED", "automationMode": "AUTO_CREATE_PR"},
        ]
        mock_load_state.return_value = {}

        # s1 send_message will raise RuntimeError
        mock_jules_client.send_message.side_effect = lambda session_id, msg: exec("raise RuntimeError('unexpected failure')") if session_id == "s1" else None

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify: both sessions were attempted
        self.assertEqual(mock_jules_client.send_message.call_count, 2)
        mock_jules_client.send_message.assert_any_call("s1", "ok")
        mock_jules_client.send_message.assert_any_call("s2", "ok")

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_session_404_error_handling(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup: Session s1 gets 404 error during resume.
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {"name": "projects/p/locations/l/sessions/s1", "state": "FAILED", "automationMode": "AUTO_CREATE_PR"},
        ]
        mock_load_state.return_value = {}

        # Mock send_message to raise a 404 RuntimeError
        mock_jules_client.send_message.side_effect = RuntimeError("Failed to send message: HTTP 404: Requested entity was not found.")

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify state is set to -1 (NOT_FOUND) and saved
        mock_save_state.assert_called_with({"s1": -1})

        # Test that session is skipped when retry_state contains -1
        mock_jules_client.send_message.reset_mock()
        mock_load_state.return_value = {"s1": -1}

        check_and_resume_or_archive_sessions()
        mock_jules_client.send_message.assert_not_called()

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    def test_check_and_start_recurrent_jules_tasks_comma_separated_tags(self, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        # Mock file contents with comma-separated tags in frontmatter
        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: jules, recurrent, auto-improvement
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = []  # No active sessions

        # Execute
        check_and_start_recurrent_jules_tasks("owner/repo")

        # Verify
        mock_jules_client.start_session.assert_called_once()
        args, kwargs = mock_jules_client.start_session.call_args
        self.assertEqual(kwargs["title"], "auto improvement with demo site")

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    def test_check_and_start_recurrent_jules_tasks_space_separated_tags(self, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        # Mock file contents with space-separated tags in frontmatter
        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: jules recurrent auto-improvement
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = []  # No active sessions

        # Execute
        check_and_start_recurrent_jules_tasks("owner/repo")

        # Verify
        mock_jules_client.start_session.assert_called_once()
        args, kwargs = mock_jules_client.start_session.call_args
        self.assertEqual(kwargs["title"], "auto improvement with demo site")
