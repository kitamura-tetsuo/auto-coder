import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.jules_client import JulesClient, JulesSessionRejectedError
from auto_coder.jules_engine import (
    SESSION_STATE_STOPPED,
    _recurrent_implementation_owner,
    check_and_restart_recurrent_jules_task_for_pr,
    check_and_resume_or_archive_sessions,
    check_and_start_recurrent_jules_tasks,
    get_session_pull_request,
    is_session_stopped,
    mark_session_stopped,
    normalize_session_outputs,
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

    @patch("auto_coder.jules_engine.os.path.isdir", return_value=True)
    @patch("auto_coder.jules_engine.glob.glob", return_value=["/path/to/prompts/recurrent_prompt.md"])
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    def test_recurrent_jules_task_respects_and_durably_owns_implementation_slot(
        self,
        mock_jules_client_cls,
        mock_open,
        _mock_glob,
        _mock_isdir,
    ):
        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent]
name: [maintenance]
---
Maintain the repository."""
        mock_open.return_value.__enter__.return_value = mock_file
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = []

        with TemporaryDirectory() as temporary_directory:
            slots = ImplementationSlotRepository(
                "owner/repo",
                1,
                Path(temporary_directory) / "slots.json",
            )
            issue_owner = ImplementationOwner("issue", 100)
            self.assertTrue(slots.reserve(issue_owner))

            check_and_start_recurrent_jules_tasks("owner/repo", slots)

            mock_jules_client.start_session.assert_not_called()
            self.assertEqual(slots.active_owners(), (issue_owner,))

            slots.release(issue_owner)
            check_and_start_recurrent_jules_tasks("owner/repo", slots)

            mock_jules_client.start_session.assert_called_once()
            active_owners = slots.active_owners()
            self.assertEqual(len(active_owners), 1)
            self.assertEqual(active_owners[0].kind, "recurrent")

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("auto_coder.jules_engine.JulesClient")
    def test_recurrent_task_respects_and_durably_claims_implementation_limit(self, mock_jules_client_cls, mock_glob, mock_isdir):
        mock_isdir.return_value = True
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = []

        with TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "recurrent_prompt.md"
            prompt_path.write_text(
                """---
tags: [jules, recurrent]
name: [scheduled maintenance]
---
Maintain the application.""",
                encoding="utf-8",
            )
            mock_glob.return_value = [str(prompt_path)]
            slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")
            occupied_owner = ImplementationOwner("issue", 100)
            self.assertTrue(slots.reserve(occupied_owner))

            check_and_start_recurrent_jules_tasks("owner/repo", slots)

            mock_jules_client.start_session.assert_not_called()
            self.assertEqual(slots.active_owners(), (occupied_owner,))

            slots.release(occupied_owner)

            def assert_reserved_before_start(**_kwargs):
                owners = slots.active_owners()
                self.assertEqual(len(owners), 1)
                self.assertEqual(owners[0].kind, "recurrent")
                return "session-1"

            mock_jules_client.start_session.side_effect = assert_reserved_before_start
            check_and_start_recurrent_jules_tasks("owner/repo", slots)

            mock_jules_client.start_session.assert_called_once()
            self.assertEqual(slots.active_owners()[0].kind, "recurrent")

    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine.JulesClient")
    def test_terminal_recurrent_session_releases_and_reuses_owner(self, mock_jules_client_cls, mock_github_client_cls):
        with TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "recurrent_prompt.md"
            prompt = """---
tags: [jules, recurrent]
name: [scheduled maintenance]
---
Maintain the application."""
            prompt_path.write_text(prompt, encoding="utf-8")
            slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")
            owner = _recurrent_implementation_owner("owner/repo", str(prompt_path))
            self.assertTrue(slots.reserve_new(owner))
            self.assertTrue(slots.record_provider_session(owner, "old-session"))

            jules = mock_jules_client_cls.return_value
            jules.list_sessions.return_value = [
                {
                    "name": "sessions/old-session",
                    "state": "COMPLETED",
                    "prompt": prompt,
                    "outputs": {"pullRequest": {"number": 123, "repository": {"name": "owner/repo"}}},
                }
            ]
            jules.start_session.return_value = "new-session"
            mock_github_client_cls.get_instance.return_value.get_pull_request.return_value = {
                "number": 123,
                "state": "closed",
                "merged": True,
            }

            with (
                patch("auto_coder.jules_engine.os.path.isdir", return_value=True),
                patch("auto_coder.jules_engine.glob.glob", return_value=[str(prompt_path)]),
            ):
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

            jules.start_session.assert_called_once()
            restarted_slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")
            self.assertEqual(restarted_slots.active_owners(), (owner,))
            source_less_pr_owner = restarted_slots.resolve_owner(
                "pr",
                {"number": 123, "body": "No Issue reference"},
                MagicMock(issues=[]),
            )
            self.assertEqual(source_less_pr_owner, owner)

    @patch("auto_coder.jules_engine.JulesClient")
    def test_accepted_recurrent_session_retains_slot_when_membership_write_fails(self, mock_jules_client_cls):
        with TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "recurrent_prompt.md"
            prompt = """---
tags: [jules, recurrent]
name: [scheduled maintenance]
---
Maintain the application."""
            prompt_path.write_text(prompt, encoding="utf-8")
            slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")
            owner = _recurrent_implementation_owner("owner/repo", str(prompt_path))
            record_provider_session = slots.record_provider_session
            slots.record_provider_session = MagicMock(return_value=False)

            jules = mock_jules_client_cls.return_value
            jules.list_sessions.return_value = []
            jules.start_session.return_value = "submitted-session"
            with (
                patch("auto_coder.jules_engine.os.path.isdir", return_value=True),
                patch("auto_coder.jules_engine.glob.glob", return_value=[str(prompt_path)]),
            ):
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

                self.assertEqual(slots.active_owners(), (owner,))
                self.assertFalse(slots.reserve(ImplementationOwner("issue", 200)))

                slots.record_provider_session = record_provider_session
                jules.list_sessions.return_value = [
                    {
                        "name": "sessions/submitted-session",
                        "state": "ACTIVE",
                        "prompt": prompt,
                    }
                ]
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

            jules.start_session.assert_called_once()
            state = json.loads((Path(directory) / "slots.json").read_text(encoding="utf-8"))
            self.assertEqual(state[owner.key]["provider_sessions"], ["submitted-session"])

    @patch("auto_coder.jules_engine.JulesClient")
    def test_ambiguous_recurrent_submission_retains_slot_until_provider_recovery(self, mock_jules_client_cls):
        with TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "recurrent_prompt.md"
            prompt = """---
tags: [jules, recurrent]
name: [scheduled maintenance]
---
Maintain the application."""
            prompt_path.write_text(prompt, encoding="utf-8")
            slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")
            owner = _recurrent_implementation_owner("owner/repo", str(prompt_path))

            jules = mock_jules_client_cls.return_value
            jules.list_sessions.return_value = []
            jules.start_session.side_effect = RuntimeError("read timed out after provider acceptance")
            with (
                patch("auto_coder.jules_engine.os.path.isdir", return_value=True),
                patch("auto_coder.jules_engine.glob.glob", return_value=[str(prompt_path)]),
            ):
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

                self.assertEqual(slots.active_owners(), (owner,))
                self.assertFalse(slots.reserve(ImplementationOwner("issue", 200)))

                jules.list_sessions.return_value = [
                    {
                        "name": "sessions/accepted-despite-timeout",
                        "state": "ACTIVE",
                        "prompt": prompt,
                    }
                ]
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

            jules.start_session.assert_called_once()
            state = json.loads((Path(directory) / "slots.json").read_text(encoding="utf-8"))
            self.assertEqual(state[owner.key]["provider_sessions"], ["accepted-despite-timeout"])

    @patch("auto_coder.jules_engine.JulesClient")
    def test_server_error_after_recurrent_submission_retains_slot_until_recovery(self, mock_jules_client_cls):
        with TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "recurrent_prompt.md"
            prompt = """---
tags: [jules, recurrent]
name: [scheduled maintenance]
---
Maintain the application."""
            prompt_path.write_text(prompt, encoding="utf-8")
            slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")
            owner = _recurrent_implementation_owner("owner/repo", str(prompt_path))

            # Exercise the production HTTP status classification while keeping
            # provider discovery deterministic for this lifecycle regression.
            with patch("auto_coder.jules_client.get_llm_config") as get_config:
                backend = MagicMock(options=[], options_for_noedit=[], api_key=None)
                get_config.return_value.get_backend_config.return_value = backend
                jules = JulesClient()
            response = MagicMock(status_code=500, text="committed before response failed")
            jules.session.post = MagicMock(return_value=response)
            jules.list_sessions = MagicMock(return_value=[])
            mock_jules_client_cls.return_value = jules

            with (
                patch("auto_coder.jules_engine.os.path.isdir", return_value=True),
                patch("auto_coder.jules_engine.glob.glob", return_value=[str(prompt_path)]),
            ):
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

                self.assertEqual(slots.active_owners(), (owner,))
                self.assertFalse(slots.reserve(ImplementationOwner("issue", 200)))

                jules.list_sessions.return_value = [
                    {
                        "name": "sessions/accepted-despite-server-error",
                        "state": "ACTIVE",
                        "prompt": prompt,
                    }
                ]
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

            jules.session.post.assert_called_once()
            state = json.loads((Path(directory) / "slots.json").read_text(encoding="utf-8"))
            self.assertEqual(state[owner.key]["provider_sessions"], ["accepted-despite-server-error"])

    @patch("auto_coder.jules_engine.JulesClient")
    def test_authoritative_recurrent_rejection_releases_slot(self, mock_jules_client_cls):
        with TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "recurrent_prompt.md"
            prompt_path.write_text(
                """---
tags: [jules, recurrent]
name: [scheduled maintenance]
---
Maintain the application.""",
                encoding="utf-8",
            )
            slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")
            jules = mock_jules_client_cls.return_value
            jules.list_sessions.return_value = []
            jules.start_session.side_effect = JulesSessionRejectedError("Failed to start Jules session: HTTP 400: invalid prompt")

            with (
                patch("auto_coder.jules_engine.os.path.isdir", return_value=True),
                patch("auto_coder.jules_engine.glob.glob", return_value=[str(prompt_path)]),
            ):
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

            self.assertEqual(slots.active_owners(), ())
            self.assertTrue(slots.reserve(ImplementationOwner("issue", 200)))

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

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    def test_check_and_start_recurrent_jules_tasks_completed_and_merged_snake_case_pr(self, mock_github_client_cls, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        # Session contains snake_case pull_request
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {
                "name": "projects/p/locations/l/sessions/s_completed",
                "state": "COMPLETED",
                "outputs": {"pull_request": "https://github.com/owner/repo/pull/123"},
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
    @patch("auto_coder.auth_utils.get_github_token")
    def test_check_and_start_recurrent_jules_tasks_completed_and_merged_token_fallback(self, mock_get_token, mock_github_client_cls, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {
                "name": "projects/p/locations/l/sessions/s_completed",
                "state": "COMPLETED",
                "outputs": {"pullRequest": "https://github.com/owner/repo/pull/123"},
                "prompt": '---\ntags: [jules, recurrent, auto-improvement]\nname: ["auto improvement with demo site"]\n---\nThis is a recurrent task prompt.',
            }
        ]

        mock_get_token.return_value = "dummy_token"
        mock_github_client = MagicMock()
        mock_github_client.get_pull_request.return_value = {"state": "closed", "merged": True}

        # Make get_instance raise ValueError when called with no token, but return client with token
        def get_instance_side_effect(token=None, **kwargs):
            if token is None:
                raise ValueError("GitHub token is required on first call")
            return mock_github_client

        mock_github_client_cls.get_instance.side_effect = get_instance_side_effect

        # Execute
        check_and_start_recurrent_jules_tasks("owner/repo")

        # Verify
        mock_get_token.assert_called_once()
        mock_github_client.get_pull_request.assert_called_once_with("owner/repo", 123)
        mock_jules_client.start_session.assert_called_once()

    @patch("auto_coder.jules_engine.os.path.isdir")
    @patch("auto_coder.jules_engine.glob.glob")
    @patch("builtins.open", new_callable=MagicMock)
    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    def test_check_and_start_recurrent_jules_tasks_completed_and_merged_pr_dict_url_only(self, mock_github_client_cls, mock_jules_client_cls, mock_open, mock_glob, mock_isdir):
        # Setup
        mock_isdir.return_value = True
        mock_glob.return_value = ["/path/to/prompts/recurrent_prompt.md"]

        mock_file = MagicMock()
        mock_file.read.return_value = """---
tags: [jules, recurrent, auto-improvement]
name: ["auto improvement with demo site"]
---
This is a recurrent task prompt."""
        mock_open.return_value.__enter__.return_value = mock_file

        # Session contains pullRequest dict with url but no repository
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [
            {
                "name": "projects/p/locations/l/sessions/s_completed",
                "state": "COMPLETED",
                "outputs": {"pullRequest": {"number": 123, "url": "https://github.com/owner/repo/pull/123"}},
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


class TestJulesStoppedSessions(unittest.TestCase):
    """Tests for sessions stopped after failing to create a PR in time."""

    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_stopped_session_is_not_resumed(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        mock_jules_client = mock_jules_client_cls.return_value
        mock_jules_client.list_sessions.return_value = [{"name": "projects/p/locations/l/sessions/s9", "state": "FAILED", "automationMode": "AUTO_CREATE_PR"}]
        mock_load_state.return_value = {"s9": SESSION_STATE_STOPPED}

        check_and_resume_or_archive_sessions()

        mock_jules_client.send_message.assert_not_called()
        mock_save_state.assert_not_called()

    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_mark_and_check_stopped_state(self, mock_save_state, mock_load_state):
        mock_load_state.return_value = {"other": 2}

        mark_session_stopped("s9")
        mock_save_state.assert_called_once_with({"other": 2, "s9": SESSION_STATE_STOPPED})

        mock_load_state.return_value = {"s9": SESSION_STATE_STOPPED}
        self.assertTrue(is_session_stopped("s9"))

        mock_load_state.return_value = {"s9": 1}
        self.assertFalse(is_session_stopped("s9"))


class TestNormalizeSessionOutputs(unittest.TestCase):
    """Tests for Jules session output normalization."""

    def test_dict_outputs_pass_through(self):
        self.assertEqual(normalize_session_outputs({"pullRequest": {"number": 1}}), {"pullRequest": {"number": 1}})

    def test_list_of_dicts_is_merged(self):
        self.assertEqual(normalize_session_outputs([{"a": 1}, {"b": 2}]), {"a": 1, "b": 2})

    def test_list_of_pairs_is_merged(self):
        self.assertEqual(normalize_session_outputs([("a", 1), ["b", 2]]), {"a": 1, "b": 2})

    def test_unsupported_type_returns_empty_dict(self):
        self.assertEqual(normalize_session_outputs("nonsense"), {})

    def test_get_session_pull_request_supports_both_keys(self):
        self.assertEqual(get_session_pull_request({"outputs": {"pullRequest": "url-1"}}), "url-1")
        self.assertEqual(get_session_pull_request({"outputs": [{"pull_request": "url-2"}]}), "url-2")
        self.assertIsNone(get_session_pull_request({"outputs": {}}))
