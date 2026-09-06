"""FTR-1792 regression: cloud Issue dispatch (Jules, Claude Routine, Codex Cloud)
must send byte-identical provider prompts for two otherwise-identical Issues
that differ only by the presence of the exact retired "@auto-coder" label
(REQ-005), exercising the real prompt renderer rather than a mocked one.

This closes the material test-oracle gap raised on PR #1804 (TOG-dc8c3f6458b3):
the local `_apply_issue_actions_directly` path already had this invariance
test, but the three cloud dispatch call sites in issue_processor.py --
_process_issue_jules_mode, _process_issue_claude_routine_mode, and
_process_issue_codex_cloud_mode -- did not have a regression proving the
*real* renderer (not a mock) produces identical prompts for their raw,
GitHub-API-shaped label dicts.
"""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.issue_processor import _process_issue_claude_routine_mode, _process_issue_codex_cloud_mode, _process_issue_jules_mode


def _issue_data(issue_number: int, raw_labels: list) -> dict:
    return {
        "number": issue_number,
        "title": "Cloud dispatch legacy label regression",
        "body": "Verifying the retired legacy processing-lock label stays inert in cloud dispatch prompts.",
        "state": "open",
        "user": {"login": "reporter"},
        "labels": raw_labels,
    }


class TestJulesDispatchIgnoresLegacyAutoCoderLabel:
    @patch("auto_coder.issue_processor.get_commit_log", return_value="(No commit history)")
    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.issue_processor.JulesClient")
    def _dispatch(self, issue_number, raw_labels, mock_jules_client_cls, mock_cloud_manager_cls, mock_get_commit_log):
        mock_jules_client = MagicMock()
        mock_jules_client.start_session.return_value = f"session-{issue_number}"
        mock_jules_client_cls.return_value = mock_jules_client
        mock_cloud_manager_cls.return_value.add_session.return_value = True

        github_client = MagicMock()
        github_client.get_parent_issue_details.return_value = None

        _process_issue_jules_mode(
            repo_name="owner/repo",
            issue_data=_issue_data(issue_number, raw_labels),
            config=AutomationConfig(),
            github_client=github_client,
        )

        mock_jules_client.start_session.assert_called_once()
        return mock_jules_client.start_session.call_args[0][0]

    def test_prompt_identical_with_and_without_legacy_label(self):
        without_legacy = self._dispatch(201, [{"name": "bug"}, {"name": "urgent"}])
        with_legacy = self._dispatch(201, [{"name": "bug"}, {"name": "urgent"}, {"name": "@auto-coder"}])
        assert with_legacy == without_legacy
        assert "@auto-coder" not in with_legacy
        assert "bug" in with_legacy and "urgent" in with_legacy


class TestClaudeRoutineDispatchIgnoresLegacyAutoCoderLabel:
    @patch("auto_coder.issue_processor.get_commit_log", return_value="(No commit history)")
    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.claude_routine_client.ClaudeRoutineClient")
    def _dispatch(self, issue_number, raw_labels, mock_routine_client_cls, mock_cloud_manager_cls, mock_get_commit_log):
        mock_routine_client = MagicMock()
        mock_routine_client.fire_routine.return_value = (f"session-{issue_number}", "https://claude.ai/code/session")
        mock_routine_client_cls.return_value = mock_routine_client
        mock_cloud_manager_cls.return_value.add_session.return_value = True

        github_client = MagicMock()
        github_client.get_parent_issue_details.return_value = None

        _process_issue_claude_routine_mode(
            "owner/repo",
            _issue_data(issue_number, raw_labels),
            AutomationConfig(),
            github_client,
            backend_name="claude-opus-routine",
        )

        mock_routine_client.fire_routine.assert_called_once()
        return mock_routine_client.fire_routine.call_args[0][0]

    def test_prompt_identical_with_and_without_legacy_label(self):
        without_legacy = self._dispatch(301, [{"name": "bug"}, {"name": "urgent"}])
        with_legacy = self._dispatch(301, [{"name": "bug"}, {"name": "urgent"}, {"name": "@auto-coder"}])
        assert with_legacy == without_legacy
        assert "@auto-coder" not in with_legacy
        assert "bug" in with_legacy and "urgent" in with_legacy


class TestCodexCloudDispatchIgnoresLegacyAutoCoderLabel:
    @patch("auto_coder.issue_processor.get_commit_log", return_value="(No commit history)")
    @patch("auto_coder.issue_processor.get_current_attempt", return_value=0)
    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.codex_cloud_client.CodexCloudClient")
    def _dispatch(self, issue_number, raw_labels, mock_client_cls, mock_cloud_manager_cls, mock_get_current_attempt, mock_get_commit_log, home_dir, monkeypatch):
        monkeypatch.setenv("HOME", str(home_dir))
        mock_client = MagicMock()
        mock_client.start_task.return_value = f"task-{issue_number}"
        mock_client.task_urls = {}
        mock_client_cls.return_value = mock_client
        mock_cloud_manager_cls.return_value.add_session.return_value = True

        github_client = MagicMock()

        _process_issue_codex_cloud_mode(
            "owner/repo",
            _issue_data(issue_number, raw_labels),
            AutomationConfig(),
            github_client,
            backend_name="codex-cloud-luna",
        )

        mock_client.start_task.assert_called_once()
        return mock_client.start_task.call_args[0][0]

    def test_prompt_identical_with_and_without_legacy_label(self, tmp_path_factory, monkeypatch):
        # Each call gets its own isolated $HOME so the durable CloudRunRepository
        # (keyed by issue_number + attempt) never treats the second call as a
        # duplicate dispatch of the first, letting both use the same issue number.
        without_legacy = self._dispatch(401, [{"name": "bug"}, {"name": "urgent"}], home_dir=tmp_path_factory.mktemp("without-legacy"), monkeypatch=monkeypatch)
        with_legacy = self._dispatch(401, [{"name": "bug"}, {"name": "urgent"}, {"name": "@auto-coder"}], home_dir=tmp_path_factory.mktemp("with-legacy"), monkeypatch=monkeypatch)
        assert with_legacy == without_legacy
        assert "@auto-coder" not in with_legacy
        assert "bug" in with_legacy and "urgent" in with_legacy
