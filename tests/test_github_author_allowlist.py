"""
Tests for GitHub author allowlists for Issues and Pull Requests (Issue #1554).
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, get_author_id, is_author_allowlisted
from auto_coder.automation_engine import AutomationEngine
from auto_coder.llm_backend_config import get_issue_allowlist_from_config, get_pr_allowlist_from_config
from auto_coder.util.gh_cache import GitHubClient


class TestGetAuthorId:
    """Test get_author_id extraction helper."""

    def test_direct_author_id_int(self):
        data = {"author_id": 12345}
        assert get_author_id(data) == 12345

    def test_direct_author_id_str(self):
        data = {"author_id": "12345"}
        assert get_author_id(data) == 12345

    def test_direct_user_id(self):
        data = {"user_id": 67890}
        assert get_author_id(data) == 67890

    def test_nested_user_dict(self):
        data = {"user": {"id": 112233, "login": "testuser"}}
        assert get_author_id(data) == 112233

    def test_nested_user_object(self):
        user_obj = MagicMock()
        user_obj.id = 445566
        data = {"user": user_obj}
        assert get_author_id(data) == 445566

    def test_none_and_invalid(self):
        assert get_author_id(None) is None
        assert get_author_id({}) is None
        assert get_author_id({"author_id": "not_an_int"}) is None
        assert get_author_id({"user": {"id": "invalid"}}) is None


class TestIsAuthorAllowlisted:
    """Test is_author_allowlisted helper."""

    def test_none_allowlist_allows_all(self):
        # When allowlist is None, unrestricted access
        assert is_author_allowlisted(12345, None) is True
        assert is_author_allowlisted(None, None) is True

    def test_empty_allowlist_denies_all(self):
        # When allowlist is empty list, all authors denied
        assert is_author_allowlisted(12345, []) is False
        assert is_author_allowlisted(None, []) is False

    def test_matching_allowlist(self):
        allowlist = [100, 200, 300]
        assert is_author_allowlisted(100, allowlist) is True
        assert is_author_allowlisted(200, allowlist) is True
        assert is_author_allowlisted(300, allowlist) is True
        assert is_author_allowlisted(400, allowlist) is False
        assert is_author_allowlisted(None, allowlist) is False

    def test_string_allowlist_items(self):
        allowlist = ["100", 200]
        assert is_author_allowlisted(100, allowlist) is True
        assert is_author_allowlisted("200", allowlist) is True


class TestAllowlistConfig:
    """Test allowlist loading from config.toml and environment variables."""

    def test_load_from_toml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = os.path.join(tmpdir, "config.toml")
            with open(config_file, "w") as f:
                f.write(
                    """
[github]
issue_allowlist = [12345678, 87654321]
pr_allowlist = [99887766]
"""
                )

            assert get_issue_allowlist_from_config(config_file) == [12345678, 87654321]
            assert get_pr_allowlist_from_config(config_file) == [99887766]

    def test_load_from_toml_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = os.path.join(tmpdir, "config.toml")
            with open(config_file, "w") as f:
                f.write(
                    """
[jules]
enabled = true
"""
                )

            assert get_issue_allowlist_from_config(config_file) is None
            assert get_pr_allowlist_from_config(config_file) is None

    def test_automation_config_init_overrides(self):
        config = AutomationConfig(
            env_override=False,
            issue_allowlist=[111, 222],
            pr_allowlist=[333, 444],
        )
        assert config.ISSUE_ALLOWLIST == [111, 222]
        assert config.PR_ALLOWLIST == [333, 444]

    def test_automation_config_env_overrides(self, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_ISSUE_ALLOWLIST", "[1001, 1002]")
        monkeypatch.setenv("AUTO_CODER_PR_ALLOWLIST", "2001, 2002")

        config = AutomationConfig(env_override=True)
        assert config.ISSUE_ALLOWLIST == [1001, 1002]
        assert config.PR_ALLOWLIST == [2001, 2002]


class TestGitHubClientAuthorIdExtraction:
    """Test author_id field extraction in GitHubClient methods."""

    def test_get_issue_details(self):
        client = GitHubClient(token="mock")
        raw_issue = {
            "number": 1,
            "title": "Test Issue",
            "body": "Body",
            "state": "open",
            "labels": [],
            "assignees": [],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/test/repo/issues/1",
            "user": {"login": "alice", "id": 12345},
            "comments": 0,
        }
        details = client.get_issue_details(raw_issue)
        assert details["author"] == "alice"
        assert details["author_id"] == 12345

    def test_get_pr_details(self):
        client = GitHubClient(token="mock")
        raw_pr = {
            "number": 2,
            "title": "Test PR",
            "body": "PR Body",
            "state": "open",
            "labels": [],
            "assignees": [],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/test/repo/pull/2",
            "user": {"login": "bob", "id": 67890},
            "head": {"ref": "feature", "sha": "abc"},
            "base": {"ref": "main"},
            "mergeable": True,
            "draft": False,
            "comments": 0,
            "review_comments": 0,
            "commits": 1,
            "additions": 10,
            "deletions": 2,
        }
        details = client.get_pr_details(raw_pr)
        assert details["author"] == "bob"
        assert details["author_id"] == 67890


class TestAutomationEngineAllowlistFiltering:
    """Test candidate collection and candidate processing filtering in AutomationEngine."""

    @pytest.fixture
    def mock_github(self):
        gh = MagicMock()
        gh.clear_sub_issue_cache = MagicMock()
        return gh

    def test_get_candidates_filters_untrusted_issues_and_prs(self, mock_github):
        config = AutomationConfig(
            env_override=False,
            issue_allowlist=[111],  # Only author 111 allowed for issues
            pr_allowlist=[222],  # Only author 222 allowed for PRs
        )
        config.CHECK_LABELS = False

        engine = AutomationEngine(mock_github, config)

        # Mock PRs: PR #1 (author 222 - trusted), PR #2 (author 999 - untrusted)
        mock_github.get_open_prs_json.return_value = [
            {
                "number": 1,
                "title": "Trusted PR",
                "author": "trusted_pr_user",
                "author_id": 222,
                "created_at": "2026-01-01T00:00:00Z",
                "labels": [],
                "mergeable": True,
                "head": {"ref": "branch1"},
            },
            {
                "number": 2,
                "title": "Untrusted PR",
                "author": "untrusted_user",
                "author_id": 999,
                "created_at": "2026-01-01T00:00:00Z",
                "labels": [],
                "mergeable": True,
                "head": {"ref": "branch2"},
            },
        ]

        # Mock Issues: Issue #10 (author 111 - trusted), Issue #20 (author 999 - untrusted)
        mock_github.get_open_issues_json.return_value = [
            {
                "number": 10,
                "title": "Trusted Issue",
                "author": "trusted_issue_user",
                "author_id": 111,
                "created_at": "2020-01-01T00:00:00Z",
                "labels": [],
            },
            {
                "number": 20,
                "title": "Untrusted Issue",
                "author": "untrusted_user",
                "author_id": 999,
                "created_at": "2020-01-01T00:00:00Z",
                "labels": [],
            },
        ]

        with patch("auto_coder.util.github_action.preload_github_actions_status"), patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress", return_value=True), patch("auto_coder.util.github_action._check_github_actions_status") as mock_checks:
            mock_checks.return_value = MagicMock(success=True)

            candidates = engine._get_candidates("test/repo", max_items=10)

        # Candidate collection should only contain PR #1 and Issue #10
        candidate_prs = [c for c in candidates if c.type == "pr"]
        candidate_issues = [c for c in candidates if c.type == "issue"]

        assert len(candidate_prs) == 1
        assert candidate_prs[0].data["number"] == 1

        assert len(candidate_issues) == 1
        assert candidate_issues[0].data["number"] == 10

    def test_process_single_candidate_unified_denies_untrusted_issue(self, mock_github):
        config = AutomationConfig(
            env_override=False,
            issue_allowlist=[111],
            pr_allowlist=[222],
        )
        engine = AutomationEngine(mock_github, config)

        untrusted_candidate = Candidate(
            type="issue",
            data={"number": 42, "title": "Malicious Issue", "author_id": 999},
            priority=0,
        )

        with patch("auto_coder.automation_engine.LabelManager") as mock_lm, patch.object(engine, "_take_issue_actions") as mock_actions:
            res = engine._process_single_candidate_unified("test/repo", untrusted_candidate, config)

            # Should return immediately with success=False, actions=[]
            assert res.success is False
            assert res.actions == []
            assert res.error is None
            # Must NOT touch LabelManager or issue actions
            mock_lm.assert_not_called()
            mock_actions.assert_not_called()

    def test_process_single_candidate_unified_denies_untrusted_pr(self, mock_github):
        config = AutomationConfig(
            env_override=False,
            issue_allowlist=[111],
            pr_allowlist=[222],
        )
        engine = AutomationEngine(mock_github, config)

        untrusted_candidate = Candidate(
            type="pr",
            data={"number": 84, "title": "Malicious PR", "author_id": 999},
            priority=0,
        )

        with patch("auto_coder.automation_engine.LabelManager") as mock_lm, patch("auto_coder.automation_engine.process_pull_request") as mock_pr_proc:
            res = engine._process_single_candidate_unified("test/repo", untrusted_candidate, config)

            # Should return immediately with success=False, actions=[]
            assert res.success is False
            assert res.actions == []
            assert res.error is None
            # Must NOT touch LabelManager or process_pull_request
            mock_lm.assert_not_called()
            mock_pr_proc.assert_not_called()

    def test_independent_allowlists_issue_allowed_pr_denied(self, mock_github):
        # Author 500 is in issue_allowlist but NOT in pr_allowlist
        config = AutomationConfig(
            env_override=False,
            issue_allowlist=[500],
            pr_allowlist=[600],
        )
        engine = AutomationEngine(mock_github, config)

        issue_data = {"number": 1, "title": "Issue", "author_id": 500}
        pr_data = {"number": 2, "title": "PR", "author_id": 500}

        assert engine._is_issue_author_allowed(issue_data) is True
        assert engine._is_pr_author_allowed(pr_data) is False

    def test_independent_allowlists_pr_allowed_issue_denied(self, mock_github):
        # Author 600 is in pr_allowlist but NOT in issue_allowlist
        config = AutomationConfig(
            env_override=False,
            issue_allowlist=[500],
            pr_allowlist=[600],
        )
        engine = AutomationEngine(mock_github, config)

        issue_data = {"number": 1, "title": "Issue", "author_id": 600}
        pr_data = {"number": 2, "title": "PR", "author_id": 600}

        assert engine._is_issue_author_allowed(issue_data) is False
        assert engine._is_pr_author_allowed(pr_data) is True

    def test_process_single_denies_untrusted_issue(self, mock_github):
        config = AutomationConfig(
            env_override=False,
            issue_allowlist=[111],
        )
        engine = AutomationEngine(mock_github, config)

        mock_github.get_issue.return_value = {"number": 42}
        mock_github.get_issue_details.return_value = {
            "number": 42,
            "title": "Untrusted Issue",
            "author_id": 999,
            "user": {"id": 999},
        }

        with patch.object(engine, "_check_and_handle_closed_branch", return_value=True), patch("auto_coder.automation_engine.LabelManager") as mock_lm, patch.object(engine, "_take_issue_actions") as mock_actions:
            result = engine.process_single("test/repo", "issue", 42)

            assert result["issues_processed"] == []
            assert result["prs_processed"] == []
            mock_lm.assert_not_called()
            mock_actions.assert_not_called()

    def test_process_single_denies_untrusted_pr(self, mock_github):
        config = AutomationConfig(
            env_override=False,
            pr_allowlist=[222],
        )
        engine = AutomationEngine(mock_github, config)

        mock_github.get_pull_request.return_value = {"number": 84}
        mock_github.get_pr_details.return_value = {
            "number": 84,
            "title": "Untrusted PR",
            "author_id": 999,
            "user": {"id": 999},
            "head_branch": "patch-1",
        }

        with patch.object(engine, "_check_and_handle_closed_branch", return_value=True), patch("auto_coder.automation_engine.LabelManager") as mock_lm, patch("auto_coder.automation_engine.process_pull_request") as mock_pr:
            result = engine.process_single("test/repo", "pr", 84)

            assert result["issues_processed"] == []
            assert result["prs_processed"] == []
            mock_lm.assert_not_called()
            mock_pr.assert_not_called()

    def test_allowlist_none_allows_all(self, mock_github):
        config = AutomationConfig(
            env_override=False,
            issue_allowlist=None,
            pr_allowlist=None,
        )
        engine = AutomationEngine(mock_github, config)

        assert engine._is_issue_author_allowed({"author_id": 999}) is True
        assert engine._is_issue_author_allowed({}) is True
        assert engine._is_pr_author_allowed({"author_id": 999}) is True
        assert engine._is_pr_author_allowed({}) is True
