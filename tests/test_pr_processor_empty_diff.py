"""Tests for detecting and closing PRs with zero effective diff and requeuing source issues."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig, EmptyPRResult
from src.auto_coder.automation_engine import AutomationEngine, Candidate
from src.auto_coder.pr_processor import (
    _close_empty_pr,
    _is_empty_pr,
    _resolve_pr_issue_numbers,
    process_pull_request,
)


def _empty_pr_data_with_commits() -> dict:
    """Build PR data with commits but 0 changed files (e.g. kitamura-tetsuo/outliner#4971)."""
    return {
        "number": 4971,
        "title": "Fix outline bug",
        "body": "Fixes bug.\n\nclose #4800",
        "state": "open",
        "user": {"login": "google-labs-jules[bot]"},
        "head": {"ref": "jules-fix-4800", "sha": "abcdef123456"},
        "base": {"ref": "main"},
        "commits_count": 3,
        "changed_files": 0,
        "additions": 0,
        "deletions": 0,
    }


def _empty_pr_data_zero_commits() -> dict:
    """Build PR data with 0 commits and 0 changed files."""
    return {
        "number": 4972,
        "title": "Fix something else",
        "body": "Fixes something.\n\nclose #4801",
        "state": "open",
        "user": {"login": "some-developer"},
        "head": {"ref": "feature-branch", "sha": "123456abcdef"},
        "base": {"ref": "main"},
        "commits_count": 0,
        "changed_files": 0,
        "additions": 0,
        "deletions": 0,
    }


def _non_empty_pr_data() -> dict:
    """Build PR data with non-zero diff."""
    return {
        "number": 4973,
        "title": "Legitimate code fix",
        "body": "Fixes real issue.\n\nclose #4802",
        "state": "open",
        "user": {"login": "some-developer"},
        "head": {"ref": "fix-4802", "sha": "feedbeef1234"},
        "base": {"ref": "main"},
        "commits_count": 1,
        "changed_files": 2,
        "additions": 15,
        "deletions": 3,
    }


class TestIsEmptyPR:
    """Tests for _is_empty_pr detection logic."""

    def test_empty_pr_with_commits_is_empty(self):
        """A PR with commits but changed_files == 0 is empty."""
        pr_data = _empty_pr_data_with_commits()
        assert _is_empty_pr(pr_data) is True

    def test_empty_pr_with_zero_commits_is_empty(self):
        """A PR with 0 commits and 0 changed files is empty."""
        pr_data = _empty_pr_data_zero_commits()
        assert _is_empty_pr(pr_data) is True

    def test_non_empty_pr_is_not_empty(self):
        """A PR with changed_files > 0 is not empty."""
        pr_data = _non_empty_pr_data()
        assert _is_empty_pr(pr_data) is False

    def test_empty_pr_fallback_to_client_diff(self):
        """When changed_files is None, check diff via github_client."""
        pr_data = {
            "number": 5000,
            "title": "No diff PR",
            "body": "Fixes #123",
            "state": "open",
        }
        github_client = Mock()
        github_client.get_pr_diff.return_value = ""

        assert _is_empty_pr(pr_data, repo_name="owner/repo", github_client=github_client) is True
        github_client.get_pr_diff.assert_called_once_with("owner/repo", 5000)

    def test_non_empty_pr_fallback_to_client_diff(self):
        """When changed_files is None and diff is non-empty, return False."""
        pr_data = {
            "number": 5001,
            "title": "Diff PR",
            "body": "Fixes #123",
            "state": "open",
        }
        github_client = Mock()
        github_client.get_pr_diff.return_value = "diff --git a/foo.py b/foo.py\n+print('hello')"

        assert _is_empty_pr(pr_data, repo_name="owner/repo", github_client=github_client) is False


class TestResolvePRIssueNumbers:
    """Tests for _resolve_pr_issue_numbers."""

    def test_resolves_from_pr_body(self):
        pr_data = {"body": "This fixes #4800 and closes #4801"}
        github_client = Mock()
        issues = _resolve_pr_issue_numbers("owner/repo", pr_data, github_client)
        assert 4800 in issues
        assert 4801 in issues

    def test_resolves_from_branch_name(self):
        pr_data = {"body": "", "head": {"ref": "issue-4805"}}
        github_client = Mock()
        with patch("src.auto_coder.pr_processor._resolve_jules_pr_issue_number", return_value=None):
            issues = _resolve_pr_issue_numbers("owner/repo", pr_data, github_client)
            assert issues == [4805]

    def test_resolves_from_title(self):
        pr_data = {"body": "", "head": {"ref": "some-random-branch"}, "title": "Fix #4810 issue"}
        github_client = Mock()
        with patch("src.auto_coder.pr_processor._resolve_jules_pr_issue_number", return_value=None):
            issues = _resolve_pr_issue_numbers("owner/repo", pr_data, github_client)
            assert issues == [4810]


class TestCloseEmptyPR:
    """Tests for _close_empty_pr."""

    @patch("src.auto_coder.pr_processor._remove_reviewer_sessions_for_closed_pr")
    @patch("src.auto_coder.pr_processor._release_issue_processing_label")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_closes_empty_pr_and_increments_attempt(self, mock_increment, mock_release_label, mock_remove_sessions):
        github_client = Mock()
        github_client.get_issue.return_value = {"state": "open", "number": 4800}
        mock_increment.return_value = 2
        mock_release_label.return_value = True
        config = AutomationConfig()

        pr_data = _empty_pr_data_with_commits()
        result = _close_empty_pr(github_client, "owner/repo", pr_data, config)

        assert result.closed is True
        assert result.issue_numbers == [4800]
        github_client.close_pr.assert_called_once()
        mock_remove_sessions.assert_called_once_with("owner/repo", 4971)
        close_args = github_client.close_pr.call_args[0]
        assert close_args[0] == "owner/repo"
        assert close_args[1] == 4971
        assert "no effective diff" in close_args[2]

        mock_increment.assert_called_once_with("owner/repo", 4800)
        mock_release_label.assert_called_once_with(github_client, "owner/repo", 4800, config)
        github_client.reopen_issue.assert_not_called()

    @patch("src.auto_coder.pr_processor._release_issue_processing_label")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_reopens_closed_source_issue(self, mock_increment, mock_release_label):
        github_client = Mock()
        github_client.get_issue.return_value = {"state": "closed", "number": 4800}
        mock_increment.return_value = 2
        mock_release_label.return_value = True
        config = AutomationConfig()

        pr_data = _empty_pr_data_with_commits()
        result = _close_empty_pr(github_client, "owner/repo", pr_data, config)

        assert result.closed is True
        github_client.reopen_issue.assert_called_once()
        reopen_args = github_client.reopen_issue.call_args[0]
        assert reopen_args[0] == "owner/repo"
        assert reopen_args[1] == 4800
        assert any("Reopened closed issue #4800" in a for a in result.actions)

    def test_ignores_non_empty_pr(self):
        github_client = Mock()
        config = AutomationConfig()
        pr_data = _non_empty_pr_data()

        result = _close_empty_pr(github_client, "owner/repo", pr_data, config)
        assert result.closed is False
        assert result.actions == []
        assert result.issue_numbers == []
        github_client.close_pr.assert_not_called()

    def test_ignores_already_closed_pr(self):
        github_client = Mock()
        config = AutomationConfig()
        pr_data = _empty_pr_data_with_commits()
        pr_data["state"] = "closed"

        result = _close_empty_pr(github_client, "owner/repo", pr_data, config)
        assert result.closed is False
        github_client.close_pr.assert_not_called()


class TestProcessPullRequestEmptyDiff:
    """Tests for process_pull_request with empty PRs."""

    @patch("src.auto_coder.pr_processor._release_issue_processing_label")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_process_pull_request_closes_empty_pr(self, mock_increment, mock_release_label):
        github_client = Mock()
        github_client.get_issue.return_value = {"state": "open", "number": 4800}
        mock_increment.return_value = 2
        mock_release_label.return_value = True
        config = AutomationConfig()

        pr_data = _empty_pr_data_with_commits()
        result = process_pull_request(github_client, config, "owner/repo", pr_data)

        assert result.priority == "close"
        assert any("Closed empty PR #4971" in action for action in result.actions_taken)
        github_client.close_pr.assert_called_once()


class TestAutomationEngineEmptyPRRequeue:
    """Tests for AutomationEngine candidate collection and single candidate empty PR handling."""

    @patch("src.auto_coder.util.github_action.preload_github_actions_status")
    @patch("src.auto_coder.pr_processor._release_issue_processing_label")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_get_candidates_closes_empty_pr_and_queues_issue(self, mock_increment, mock_release_label, mock_preload):
        mock_increment.return_value = 2
        mock_release_label.return_value = True

        mock_github = Mock()
        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config=config)

        empty_pr = _empty_pr_data_with_commits()
        engine.github.get_open_prs_json.return_value = [empty_pr]
        engine.github.get_issue.return_value = {"state": "open", "number": 4800}
        engine.github.get_open_issues.return_value = []
        engine.github.get_open_sub_issues.return_value = []

        with patch.object(engine, "_create_candidate_from_single") as mock_create_cand:
            mock_cand = Candidate(type="issue", data={"number": 4800, "labels": []}, priority=0)
            mock_create_cand.return_value = mock_cand

            candidates = engine._get_candidates("owner/repo")

            engine.github.close_pr.assert_called_once()
            mock_increment.assert_called_once_with("owner/repo", 4800)
            mock_create_cand.assert_called_once_with("owner/repo", "issue", 4800)
            assert mock_cand.priority == 3
            assert mock_cand in candidates

    @patch("src.auto_coder.pr_processor._release_issue_processing_label")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_process_single_candidate_closes_empty_pr_and_runs_unlocked_issue(self, mock_increment, mock_release_label):
        mock_increment.return_value = 2
        mock_release_label.return_value = True

        mock_github = Mock()
        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config=config)

        empty_pr = _empty_pr_data_with_commits()
        candidate = Candidate(type="pr", data=empty_pr, priority=1)
        engine.github.get_issue.return_value = {"state": "open", "number": 4800}

        with patch.object(engine, "_process_unlocked_issue", return_value=["Processed new attempt"]) as mock_unlocked:
            result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config, jules_mode=True)

            assert result.success is True
            assert any("Closed empty PR #4971" in a for a in result.actions)
            assert "Processed new attempt" in result.actions
            mock_unlocked.assert_called_once_with("owner/repo", 4800, engine.config, True)
