"""Tests for automatic issue closing when PR is merged."""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _close_linked_issues, _extract_linked_issues_from_pr_body, _merge_pr


class TestExtractLinkedIssues:
    """Test extraction of linked issues from PR body."""

    def test_extract_closes_keyword(self):
        """Test extraction with 'closes' keyword."""
        pr_body = "This PR closes #123"
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == [123]

    def test_extract_fixes_keyword(self):
        """Test extraction with 'fixes' keyword."""
        pr_body = "This PR fixes #456"
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == [456]

    def test_extract_resolves_keyword(self):
        """Test extraction with 'resolves' keyword."""
        pr_body = "This PR resolves #789"
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == [789]

    def test_extract_multiple_keywords(self):
        """Test extraction with multiple keywords."""
        pr_body = "This PR closes #123 and fixes #456"
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == [123, 456]

    def test_extract_case_insensitive(self):
        """Test extraction is case insensitive."""
        pr_body = "This PR Closes #123 and FIXES #456"
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == [123, 456]

    def test_extract_all_keyword_variants(self):
        """Test all supported keyword variants."""
        pr_body = """
        close #1
        closes #2
        closed #3
        fix #4
        fixes #5
        fixed #6
        resolve #7
        resolves #8
        resolved #9
        """
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    def test_extract_with_cross_repo_reference(self):
        """Test extraction with cross-repo reference (owner/repo#123)."""
        pr_body = "This PR closes owner/repo#123"
        result = _extract_linked_issues_from_pr_body(pr_body)
        # We extract the issue number even for cross-repo references
        assert result == [123]

    def test_extract_removes_duplicates(self):
        """Test that duplicate issue numbers are removed."""
        pr_body = "This PR closes #123 and fixes #123"
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == [123]

    def test_extract_empty_body(self):
        """Test extraction with empty body."""
        result = _extract_linked_issues_from_pr_body("")
        assert result == []

    def test_extract_none_body(self):
        """Test extraction with None body."""
        result = _extract_linked_issues_from_pr_body(None)
        assert result == []

    def test_extract_no_keywords(self):
        """Test extraction with no keywords."""
        pr_body = "This is a regular PR description without any issue links"
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == []

    def test_extract_preserves_order(self):
        """Test that issue order is preserved."""
        pr_body = "Closes #789, fixes #123, resolves #456"
        result = _extract_linked_issues_from_pr_body(pr_body)
        assert result == [789, 123, 456]


class TestCloseLinkedIssues:
    """Test closing of linked issues."""

    @patch("src.auto_coder.pr_processor.cmd")
    def test_close_single_issue(self, mock_cmd):
        """Test closing a single linked issue."""
        # Mock PR body retrieval
        mock_cmd.run_command.side_effect = [
            Mock(
                success=True,
                stdout='{"body": "Closes #123"}',
                stderr="",
                returncode=0,
            ),
            Mock(success=True, stdout="", stderr="", returncode=0),  # issue close
        ]

        _close_linked_issues("test/repo", 456)

        # Verify gh pr view was called
        assert mock_cmd.run_command.call_count == 2
        pr_view_call = mock_cmd.run_command.call_args_list[0][0][0]
        assert pr_view_call == [
            "gh",
            "pr",
            "view",
            "456",
            "--repo",
            "test/repo",
            "--json",
            "body",
        ]

        # Verify gh issue close was called
        issue_close_call = mock_cmd.run_command.call_args_list[1][0][0]
        assert issue_close_call == [
            "gh",
            "issue",
            "close",
            "123",
            "--repo",
            "test/repo",
            "--comment",
            "Closed by PR #456",
        ]

    @patch("src.auto_coder.pr_processor.cmd")
    def test_close_multiple_issues(self, mock_cmd):
        """Test closing multiple linked issues."""
        # Mock PR body retrieval
        mock_cmd.run_command.side_effect = [
            Mock(
                success=True,
                stdout='{"body": "Closes #123 and fixes #456"}',
                stderr="",
                returncode=0,
            ),
            Mock(success=True, stdout="", stderr="", returncode=0),  # close #123
            Mock(success=True, stdout="", stderr="", returncode=0),  # close #456
        ]

        _close_linked_issues("test/repo", 789)

        # Verify both issues were closed
        assert mock_cmd.run_command.call_count == 3
        issue_close_calls = [
            mock_cmd.run_command.call_args_list[1][0][0],
            mock_cmd.run_command.call_args_list[2][0][0],
        ]
        assert [
            "gh",
            "issue",
            "close",
            "123",
            "--repo",
            "test/repo",
            "--comment",
            "Closed by PR #789",
        ] in issue_close_calls
        assert [
            "gh",
            "issue",
            "close",
            "456",
            "--repo",
            "test/repo",
            "--comment",
            "Closed by PR #789",
        ] in issue_close_calls

    @patch("src.auto_coder.pr_processor.cmd")
    def test_no_linked_issues(self, mock_cmd):
        """Test when PR has no linked issues."""
        # Mock PR body retrieval
        mock_cmd.run_command.return_value = Mock(
            success=True,
            stdout='{"body": "Regular PR description"}',
            stderr="",
            returncode=0,
        )

        _close_linked_issues("test/repo", 456)

        # Verify only PR view was called, no issue close
        assert mock_cmd.run_command.call_count == 1

    @patch("src.auto_coder.pr_processor.cmd")
    def test_pr_view_failure(self, mock_cmd):
        """Test when PR view fails."""
        # Mock PR body retrieval failure
        mock_cmd.run_command.return_value = Mock(
            success=False,
            stdout="",
            stderr="PR not found",
            returncode=1,
        )

        # Should not raise exception
        _close_linked_issues("test/repo", 456)

        # Verify only PR view was called
        assert mock_cmd.run_command.call_count == 1

    @patch("src.auto_coder.pr_processor.cmd")
    def test_issue_close_failure(self, mock_cmd):
        """Test when issue close fails."""
        # Mock PR body retrieval success, issue close failure
        mock_cmd.run_command.side_effect = [
            Mock(
                success=True,
                stdout='{"body": "Closes #123"}',
                stderr="",
                returncode=0,
            ),
            Mock(
                success=False,
                stdout="",
                stderr="Issue not found",
                returncode=1,
            ),
        ]

        # Should not raise exception
        _close_linked_issues("test/repo", 456)

        # Verify both calls were made
        assert mock_cmd.run_command.call_count == 2


class TestMergePRWithIssueClosing:
    """Test that _merge_pr closes linked issues after successful merge."""

    @patch("src.auto_coder.pr_processor._close_linked_issues")
    @patch("src.auto_coder.pr_processor.cmd")
    def test_merge_pr_closes_issues_on_success(self, mock_cmd, mock_close_issues):
        """Test that successful merge triggers issue closing."""
        config = AutomationConfig()
        config.MERGE_AUTO = False
        config.MERGE_METHOD = "--squash"

        # Mock successful merge
        mock_cmd.run_command.return_value = Mock(success=True, stdout="", stderr="", returncode=0)

        result = _merge_pr("test/repo", 123, {}, config)

        assert result is True
        mock_close_issues.assert_called_once_with("test/repo", 123)

    @patch("src.auto_coder.pr_processor._close_linked_issues")
    @patch("src.auto_coder.pr_processor.cmd")
    def test_merge_pr_does_not_close_issues_on_failure(self, mock_cmd, mock_close_issues):
        """Test that failed merge does not trigger issue closing."""
        config = AutomationConfig()
        config.MERGE_AUTO = False
        config.MERGE_METHOD = "--squash"

        # Mock failed merge
        mock_cmd.run_command.return_value = Mock(success=False, stdout="", stderr="Merge failed", returncode=1)

        result = _merge_pr("test/repo", 123, {}, config)

        assert result is False
        mock_close_issues.assert_not_called()

    @patch("src.auto_coder.pr_processor._close_linked_issues")
    @patch("src.auto_coder.pr_processor.cmd")
    def test_merge_pr_auto_merge_closes_issues(self, mock_cmd, mock_close_issues):
        """Test that auto-merge success triggers issue closing."""
        config = AutomationConfig()
        config.MERGE_AUTO = True
        config.MERGE_METHOD = "--squash"

        # Mock successful auto-merge
        mock_cmd.run_command.return_value = Mock(success=True, stdout="", stderr="", returncode=0)

        result = _merge_pr("test/repo", 123, {}, config)

        assert result is True
        mock_close_issues.assert_called_once_with("test/repo", 123)
