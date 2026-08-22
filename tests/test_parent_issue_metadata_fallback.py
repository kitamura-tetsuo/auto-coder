"""Unit and integration tests for Parent-Issue metadata fallback for sub-issues."""

from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig, Candidate
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.util.gh_cache import GitHubClient, parse_parent_issue_number


class TestParseParentIssueNumber:
    """Tests for parse_parent_issue_number."""

    def test_parse_standard_parent_issue(self):
        body = "Here is the issue description.\n\nParent-Issue: #1234\n\nSome more details."
        assert parse_parent_issue_number(body) == 1234

    def test_parse_case_insensitive(self):
        assert parse_parent_issue_number("parent-issue: #5678") == 5678
        assert parse_parent_issue_number("PARENT-ISSUE: #999") == 999
        assert parse_parent_issue_number("Parent-issue: #42") == 42

    def test_parse_without_hash(self):
        assert parse_parent_issue_number("Parent-Issue: 1234") == 1234
        assert parse_parent_issue_number("parent_issue: 5678") == 5678
        assert parse_parent_issue_number("Parent Issue: 999") == 999

    def test_parse_none_or_empty(self):
        assert parse_parent_issue_number(None) is None
        assert parse_parent_issue_number("") is None
        assert parse_parent_issue_number("Just a normal issue description with no metadata.") is None

    def test_parse_invalid_number(self):
        assert parse_parent_issue_number("Parent-Issue: #abc") is None
        assert parse_parent_issue_number("Parent-Issue:") is None


class TestAddSubIssue:
    """Tests for GitHubClient.add_sub_issue."""

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    @patch.object(GitHubClient, "get_all_sub_issues")
    @patch.object(GitHubClient, "get_issue")
    def test_add_sub_issue_success(self, mock_get_issue, mock_get_all_sub_issues, mock_get_caching_client, mock_github_token):
        mock_get_all_sub_issues.return_value = []
        mock_sub_issue = {"id": 98765, "number": 200}
        mock_get_issue.return_value = mock_sub_issue

        mock_http_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_http_client.post.return_value = mock_response
        mock_get_caching_client.return_value = mock_http_client

        client = GitHubClient.get_instance("test-token")
        success = client.add_sub_issue("owner/repo", parent_issue_number=100, sub_issue_number=200)

        assert success is True
        mock_http_client.post.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/issues/100/sub_issues",
            headers={"Authorization": "bearer test-token", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            json={"sub_issue_id": 98765},
        )

    @patch.object(GitHubClient, "get_all_sub_issues")
    def test_add_sub_issue_already_sub_issue(self, mock_get_all_sub_issues, mock_github_token):
        mock_get_all_sub_issues.return_value = [200, 201]

        client = GitHubClient.get_instance("test-token")
        # Issue 200 is already in parent's sub-issues
        success = client.add_sub_issue("owner/repo", parent_issue_number=100, sub_issue_number=200)

        assert success is True

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    @patch.object(GitHubClient, "get_all_sub_issues")
    @patch.object(GitHubClient, "get_issue")
    def test_add_sub_issue_api_failure(self, mock_get_issue, mock_get_all_sub_issues, mock_get_caching_client, mock_github_token):
        mock_get_all_sub_issues.return_value = []
        mock_get_issue.return_value = {"id": 98765, "number": 200}

        mock_http_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_http_client.post.return_value = mock_response
        mock_get_caching_client.return_value = mock_http_client

        client = GitHubClient.get_instance("test-token")
        success = client.add_sub_issue("owner/repo", parent_issue_number=100, sub_issue_number=200)

        assert success is False


class TestParentIssueDetailsFallback:
    """Tests for fallback handling in GitHubClient.get_parent_issue_details."""

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    @patch.object(GitHubClient, "add_sub_issue")
    @patch.object(GitHubClient, "get_issue")
    def test_fallback_when_native_404(self, mock_get_issue, mock_add_sub_issue, mock_get_ghapi, mock_github_token):
        mock_api = MagicMock()
        mock_api.side_effect = Exception("404 Not Found")
        mock_get_ghapi.return_value = mock_api

        # Child issue #200 has Parent-Issue: #100 in body
        mock_child = {
            "id": 12345,
            "number": 200,
            "title": "Child task",
            "body": "Fix some stuff\n\nParent-Issue: #100",
        }
        mock_parent = {
            "id": 11111,
            "number": 100,
            "title": "Parent epic",
            "body": "Epic description",
            "state": "open",
        }

        def get_issue_side_effect(repo_name, issue_num):
            if issue_num == 200:
                return mock_child
            elif issue_num == 100:
                return mock_parent
            return None

        mock_get_issue.side_effect = get_issue_side_effect
        mock_add_sub_issue.return_value = True

        client = GitHubClient.get_instance("test-token")
        result = client.get_parent_issue_details("owner/repo", 200)

        assert result is not None
        assert result["number"] == 100
        assert result["title"] == "Parent epic"
        mock_add_sub_issue.assert_called_once_with("owner/repo", 100, 200, sub_issue_id=12345)

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    @patch.object(GitHubClient, "add_sub_issue")
    @patch.object(GitHubClient, "get_issue")
    def test_fallback_retained_when_conversion_fails(self, mock_get_issue, mock_add_sub_issue, mock_get_ghapi, mock_github_token):
        mock_api = MagicMock()
        mock_api.side_effect = Exception("404 Not Found")
        mock_get_ghapi.return_value = mock_api

        # Child issue #200 has Parent-Issue: #100
        mock_child = {
            "id": 12345,
            "number": 200,
            "title": "Child task",
            "body": "Fix some stuff\n\nParent-Issue: #100",
        }
        mock_parent = {
            "id": 11111,
            "number": 100,
            "title": "Parent epic",
            "body": "Epic description",
            "state": "open",
        }

        def get_issue_side_effect(repo_name, issue_num):
            if issue_num == 200:
                return mock_child
            elif issue_num == 100:
                return mock_parent
            return None

        mock_get_issue.side_effect = get_issue_side_effect
        # Conversion fails (e.g. 403 or unsupported)
        mock_add_sub_issue.return_value = False

        client = GitHubClient.get_instance("test-token")
        result = client.get_parent_issue_details("owner/repo", 200)

        # Parent relationship is still retained!
        assert result is not None
        assert result["number"] == 100
        assert result["title"] == "Parent epic"


class TestSiblingExclusionWithMetadataFallback:
    """Tests that sibling exclusion works with fallback Parent-Issue metadata."""

    @patch("src.auto_coder.automation_engine.LabelManager")
    @patch.object(AutomationEngine, "_is_issue_author_allowed", return_value=True)
    def test_elder_sibling_blocks_younger_sibling(self, mock_author_allowed, mock_label_manager, mock_github_token):
        """When issue #101 and issue #102 both reference Parent #100, #101 is queued and #102 is skipped."""
        client = GitHubClient.get_instance("test-token")

        config = AutomationConfig()
        config.CHECK_LABELS = False
        engine = AutomationEngine(client, config=config)

        # Mock LabelManager context manager
        mock_lm = MagicMock()
        mock_lm.__enter__.return_value = True
        mock_label_manager.return_value = mock_lm

        # Open issues:
        # Issue 100: Parent
        # Issue 101: Elder child (Parent-Issue: #100)
        # Issue 102: Younger child (Parent-Issue: #100)
        issues = [
            {
                "number": 100,
                "title": "Parent Issue",
                "body": "Parent body",
                "state": "open",
                "labels": [],
                "created_at": "2020-01-01T00:00:00Z",
                "open_sub_issue_numbers": [101, 102],
                "has_open_sub_issues": True,
                "parent_issue_number": None,
                "linked_pr_numbers": [],
            },
            {
                "number": 101,
                "title": "Sub Issue 1",
                "body": "Child 1\n\nParent-Issue: #100",
                "state": "open",
                "labels": [],
                "created_at": "2020-01-01T00:00:00Z",
                "open_sub_issue_numbers": [],
                "has_open_sub_issues": False,
                "parent_issue_number": 100,
                "linked_pr_numbers": [],
            },
            {
                "number": 102,
                "title": "Sub Issue 2",
                "body": "Child 2\n\nParent-Issue: #100",
                "state": "open",
                "labels": [],
                "created_at": "2020-01-01T00:00:00Z",
                "open_sub_issue_numbers": [],
                "has_open_sub_issues": False,
                "parent_issue_number": 100,
                "linked_pr_numbers": [],
            },
        ]

        with patch.object(client, "get_open_issues_json", return_value=issues), patch.object(client, "get_open_prs_json", return_value=[]), patch.object(client, "get_open_sub_issues", return_value=[101, 102]):
            candidates = engine._get_candidates("owner/repo")

        # Parent #100 is skipped because it has open sub-issues.
        # Issue #102 is skipped because elder sibling #101 is open.
        # Issue #101 should be the candidate!
        issue_candidates = [c for c in candidates if c.type == "issue"]
        candidate_numbers = [c.issue_number for c in issue_candidates]

        assert 100 not in candidate_numbers
        assert 102 not in candidate_numbers
        assert 101 in candidate_numbers

    @patch("src.auto_coder.automation_engine.LabelManager")
    @patch.object(AutomationEngine, "_is_issue_author_allowed", return_value=True)
    def test_closed_parent_fallback_siblings(self, mock_author_allowed, mock_label_manager, mock_github_token):
        """When parent is closed (not in issue list), elder sibling still blocks younger sibling."""
        client = GitHubClient.get_instance("test-token")

        config = AutomationConfig()
        config.CHECK_LABELS = False
        engine = AutomationEngine(client, config=config)

        mock_lm = MagicMock()
        mock_lm.__enter__.return_value = True
        mock_label_manager.return_value = mock_lm

        # Open issues (parent #100 is closed, not in list):
        # Issue 101: Elder child (Parent-Issue: #100)
        # Issue 102: Younger child (Parent-Issue: #100)
        issues = [
            {
                "number": 101,
                "title": "Sub Issue 1",
                "body": "Child 1\n\nParent-Issue: #100",
                "state": "open",
                "labels": [],
                "created_at": "2020-01-01T00:00:00Z",
                "open_sub_issue_numbers": [],
                "has_open_sub_issues": False,
                "parent_issue_number": 100,
                "linked_pr_numbers": [],
            },
            {
                "number": 102,
                "title": "Sub Issue 2",
                "body": "Child 2\n\nParent-Issue: #100",
                "state": "open",
                "labels": [],
                "created_at": "2020-01-01T00:00:00Z",
                "open_sub_issue_numbers": [],
                "has_open_sub_issues": False,
                "parent_issue_number": 100,
                "linked_pr_numbers": [],
            },
        ]

        with patch.object(client, "get_open_issues_json", return_value=issues), patch.object(client, "get_open_prs_json", return_value=[]), patch.object(client, "get_open_sub_issues", return_value=[101, 102]):
            candidates = engine._get_candidates("owner/repo")

        issue_candidates = [c for c in candidates if c.type == "issue"]
        candidate_numbers = [c.issue_number for c in issue_candidates]

        assert 101 in candidate_numbers
        assert 102 not in candidate_numbers


class TestGetOpenIssuesJsonFallback:
    """Tests for get_open_issues_json handling Parent-Issue metadata."""

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    @patch.object(GitHubClient, "add_sub_issue")
    @patch.object(GitHubClient, "get_linked_prs", return_value=[])
    def test_get_open_issues_json_synchronizes_fallback_sub_issues(self, mock_linked_prs, mock_add_sub_issue, mock_get_ghapi, mock_github_token):
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        # Raw issue list returned by GitHub API
        raw_issues = [
            {
                "number": 100,
                "title": "Parent Issue",
                "body": "Parent body without sub_issues_summary",
                "state": "open",
                "labels": [],
                "assignees": [],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "http://github.com/owner/repo/issues/100",
                "user": {"login": "dev", "id": 1},
                "comments": 0,
                "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
            },
            {
                "number": 101,
                "title": "Child Issue 1",
                "body": "Description\n\nParent-Issue: #100",
                "state": "open",
                "labels": [],
                "assignees": [],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "http://github.com/owner/repo/issues/101",
                "user": {"login": "dev", "id": 1},
                "comments": 0,
                "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
            },
        ]
        mock_api.issues.list_for_repo.return_value = raw_issues

        client = GitHubClient.get_instance("test-token")
        client.clear_open_issues_cache()

        results = client.get_open_issues_json("owner/repo")

        # Check Child #101 has parent_issue_number 100
        child_item = next(r for r in results if r["number"] == 101)
        assert child_item["parent_issue_number"] == 100

        # Check Parent #100 has open_sub_issue_numbers containing [101]
        parent_item = next(r for r in results if r["number"] == 100)
        assert 101 in parent_item["open_sub_issue_numbers"]
        assert parent_item["has_open_sub_issues"] is True

        # Check that add_sub_issue was attempted
        mock_add_sub_issue.assert_called_once()
