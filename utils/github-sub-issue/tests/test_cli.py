"""Tests for CLI."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from github_sub_issue.cli import main


class TestCLI:
    """Test class for CLI."""

    def setup_method(self) -> None:
        """Run before each test."""
        self.runner = CliRunner()

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_add_command(self, mock_api_class: MagicMock) -> None:
        """Verify that add command works correctly."""
        mock_api = MagicMock()
        mock_api.add_sub_issue.return_value = {
            "data": {
                "addSubIssue": {
                    "issue": {"number": 123, "title": "Parent Issue"},
                    "subIssue": {"number": 456, "title": "Child Issue"},
                }
            }
        }
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["add", "123", "456"])

        assert result.exit_code == 0
        assert "Added sub-issue #456 to parent #123" in result.output
        mock_api.add_sub_issue.assert_called_once_with("123", "456", False)

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_add_command_multiple_sub_issues(self, mock_api_class: MagicMock) -> None:
        """Verify that multiple sub-issues can be added in a single command."""
        mock_api = MagicMock()
        mock_api.add_sub_issue.side_effect = [
            {
                "data": {
                    "addSubIssue": {
                        "issue": {"number": 123, "title": "Parent Issue"},
                        "subIssue": {"number": 456, "title": "Child 1"},
                    }
                }
            },
            {
                "data": {
                    "addSubIssue": {
                        "issue": {"number": 123, "title": "Parent Issue"},
                        "subIssue": {"number": 457, "title": "Child 2"},
                    }
                }
            },
            {
                "data": {
                    "addSubIssue": {
                        "issue": {"number": 123, "title": "Parent Issue"},
                        "subIssue": {"number": 458, "title": "Child 3"},
                    }
                }
            },
        ]
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["add", "123", "456", "457", "458"])

        assert result.exit_code == 0
        assert "Added sub-issue #456" in result.output
        assert "Added sub-issue #457" in result.output
        assert "Added sub-issue #458" in result.output
        assert "Successfully added 3 sub-issue(s)" in result.output
        assert mock_api.add_sub_issue.call_count == 3

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_add_command_with_replace_parent(self, mock_api_class: MagicMock) -> None:
        """Verify that --replace-parent option works in add command."""
        mock_api = MagicMock()
        mock_api.add_sub_issue.return_value = {
            "data": {
                "addSubIssue": {
                    "issue": {"number": 123, "title": "Parent Issue"},
                    "subIssue": {"number": 456, "title": "Child Issue"},
                }
            }
        }
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["add", "123", "456", "--replace-parent"])

        assert result.exit_code == 0
        mock_api.add_sub_issue.assert_called_once_with("123", "456", True)

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_add_command_multiple_with_failures(self, mock_api_class: MagicMock) -> None:
        """Verify error handling when some sub-issues fail to add."""
        mock_api = MagicMock()
        # First succeeds, second fails, third succeeds
        mock_api.add_sub_issue.side_effect = [
            {
                "data": {
                    "addSubIssue": {
                        "issue": {"number": 123, "title": "Parent Issue"},
                        "subIssue": {"number": 456, "title": "Child 1"},
                    }
                }
            },
            Exception("Issue #999 does not exist"),  # Second one fails
            {
                "data": {
                    "addSubIssue": {
                        "issue": {"number": 123, "title": "Parent Issue"},
                        "subIssue": {"number": 458, "title": "Child 3"},
                    }
                }
            },
        ]
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["add", "123", "456", "999", "458"])

        assert result.exit_code == 1
        assert "Added sub-issue #456" in result.output
        assert "Failed to add sub-issue 999" in result.output
        assert "Added sub-issue #458" in result.output
        assert "Successfully added 2 sub-issue(s)" in result.output
        assert "Failed to add 1 sub-issue(s)" in result.output
        assert mock_api.add_sub_issue.call_count == 3

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_add_command_all_failures(self, mock_api_class: MagicMock) -> None:
        """Verify error handling when all sub-issues fail to add."""
        mock_api = MagicMock()
        # All fail
        mock_api.add_sub_issue.side_effect = [
            Exception("Issue #999 does not exist"),
            Exception("Issue #998 does not exist"),
        ]
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["add", "123", "999", "998"])

        assert result.exit_code == 1
        assert "Failed to add sub-issue 999" in result.output
        assert "Failed to add sub-issue 998" in result.output
        assert "Successfully added 0 sub-issue(s)" in result.output
        assert "Failed to add 2 sub-issue(s)" in result.output
        assert mock_api.add_sub_issue.call_count == 2

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_create_command(self, mock_api_class: MagicMock) -> None:
        """Verify that create command works correctly."""
        mock_api = MagicMock()
        mock_api.create_sub_issue.return_value = {
            "number": 789,
            "title": "New Issue",
            "url": "https://github.com/owner/repo/issues/789",
        }
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(
            main,
            [
                "create",
                "--parent", "123",
                "--title", "New Issue",
                "--body", "Description",
                "--label", "bug",
                "--assignee", "user1",
            ],
        )

        assert result.exit_code == 0
        assert "Created sub-issue #789: New Issue" in result.output
        mock_api.create_sub_issue.assert_called_once_with(
            "123",
            "New Issue",
            body="Description",
            labels=["bug"],
            assignees=["user1"],
            body_file=None,
        )

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_create_command_with_body_file(self, mock_api_class: MagicMock) -> None:
        """Verify that create command works correctly with --body-file option."""
        mock_api = MagicMock()
        mock_api.create_sub_issue.return_value = {
            "number": 789,
            "title": "New Issue",
            "url": "https://github.com/owner/repo/issues/789",
        }
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(
            main,
            [
                "create",
                "--parent", "123",
                "--title", "New Issue",
                "--body-file", "/path/to/body.txt",
                "--label", "bug",
            ],
        )

        assert result.exit_code == 0
        assert "Created sub-issue #789: New Issue" in result.output
        mock_api.create_sub_issue.assert_called_once_with(
            "123",
            "New Issue",
            body=None,
            labels=["bug"],
            assignees=None,
            body_file="/path/to/body.txt",
        )

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_list_command(self, mock_api_class: MagicMock) -> None:
        """Verify that list command works correctly."""
        mock_api = MagicMock()
        mock_api.list_sub_issues.return_value = [
            {
                "number": 456,
                "title": "Child 1",
                "state": "OPEN",
                "url": "https://github.com/owner/repo/issues/456",
                "assignees": {"nodes": []},
            },
            {
                "number": 457,
                "title": "Child 2",
                "state": "OPEN",
                "url": "https://github.com/owner/repo/issues/457",
                "assignees": {"nodes": [{"login": "user1"}]},
            },
        ]
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["list", "123"])

        assert result.exit_code == 0
        assert "Sub-issues (2 open)" in result.output
        assert "#456" in result.output
        assert "Child 1" in result.output
        assert "#457" in result.output
        assert "Child 2" in result.output
        assert "@user1" in result.output
        mock_api.list_sub_issues.assert_called_once_with("123", "OPEN")

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_list_command_json_output(self, mock_api_class: MagicMock) -> None:
        """Verify that JSON output works in list command."""
        mock_api = MagicMock()
        sub_issues = [
            {
                "number": 456,
                "title": "Child 1",
                "state": "OPEN",
                "url": "https://github.com/owner/repo/issues/456",
                "assignees": {"nodes": []},
            },
        ]
        mock_api.list_sub_issues.return_value = sub_issues
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["list", "123", "--json"])

        assert result.exit_code == 0
        output_data = json.loads(result.output)
        assert len(output_data) == 1
        assert output_data[0]["number"] == 456

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_list_command_empty(self, mock_api_class: MagicMock) -> None:
        """Verify behavior when list command has no sub-issues."""
        mock_api = MagicMock()
        mock_api.list_sub_issues.return_value = []
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["list", "123"])

        assert result.exit_code == 0
        assert "No open sub-issues found" in result.output

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_remove_command_with_force(self, mock_api_class: MagicMock) -> None:
        """Verify that --force option works in remove command."""
        mock_api = MagicMock()
        mock_api.remove_sub_issue.return_value = {
            "data": {
                "removeSubIssue": {
                    "issue": {"number": 123, "title": "Parent Issue"},
                    "subIssue": {"number": 456, "title": "Child Issue"},
                }
            }
        }
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["remove", "123", "456", "--force"])

        assert result.exit_code == 0
        assert "Removed sub-issue #456" in result.output
        mock_api.remove_sub_issue.assert_called_once_with("123", "456")

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_remove_command_multiple(self, mock_api_class: MagicMock) -> None:
        """Verify that multiple sub-issues can be removed in remove command."""
        mock_api = MagicMock()
        mock_api.remove_sub_issue.side_effect = [
            {
                "data": {
                    "removeSubIssue": {
                        "issue": {"number": 123, "title": "Parent Issue"},
                        "subIssue": {"number": 456, "title": "Child 1"},
                    }
                }
            },
            {
                "data": {
                    "removeSubIssue": {
                        "issue": {"number": 123, "title": "Parent Issue"},
                        "subIssue": {"number": 457, "title": "Child 2"},
                    }
                }
            },
        ]
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["remove", "123", "456", "457", "--force"])

        assert result.exit_code == 0
        assert "Removed sub-issue #456" in result.output
        assert "Removed sub-issue #457" in result.output
        assert mock_api.remove_sub_issue.call_count == 2

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_remove_command_with_confirmation(self, mock_api_class: MagicMock) -> None:
        """Verify that confirmation prompt appears in remove command."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # User inputs 'n' to cancel
        result = self.runner.invoke(main, ["remove", "123", "456"], input="n\n")

        assert result.exit_code == 0
        assert "Continue?" in result.output
        assert "Cancelled." in result.output
        mock_api.remove_sub_issue.assert_not_called()

    @patch("github_sub_issue.cli.GitHubSubIssueAPI")
    def test_repo_option(self, mock_api_class: MagicMock) -> None:
        """Verify that --repo option is passed correctly."""
        mock_api = MagicMock()
        mock_api.list_sub_issues.return_value = []
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["--repo", "owner/repo", "list", "123"])

        assert result.exit_code == 0
        mock_api_class.assert_called_once_with(repo="owner/repo")

