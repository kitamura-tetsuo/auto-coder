"""Tests for GitHub API."""

import json
from unittest.mock import MagicMock, patch

import pytest

from github_sub_issue.github_api import GitHubSubIssueAPI


class TestGitHubSubIssueAPI:
    """Test class for GitHubSubIssueAPI."""

    @patch("subprocess.run")
    def test_get_current_repo(self, mock_run: MagicMock) -> None:
        """Verify that current repository name can be obtained."""
        mock_run.return_value = MagicMock(
            stdout="owner/repo\n",
            returncode=0,
        )

        api = GitHubSubIssueAPI()
        assert api.repo == "owner/repo"

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "gh"
        assert args[1] == "repo"
        assert args[2] == "view"

    def test_parse_issue_reference_number(self) -> None:
        """Verify that issue number can be parsed."""
        api = GitHubSubIssueAPI(repo="owner/repo")
        repo, number = api._parse_issue_reference("123")
        assert repo == "owner/repo"
        assert number == 123

    def test_parse_issue_reference_url(self) -> None:
        """Verify that issue URL can be parsed."""
        api = GitHubSubIssueAPI(repo="owner/repo")
        repo, number = api._parse_issue_reference("https://github.com/other/repo/issues/456")
        assert repo == "other/repo"
        assert number == 456

    def test_parse_issue_reference_invalid(self) -> None:
        """Verify that error occurs with invalid issue reference."""
        api = GitHubSubIssueAPI(repo="owner/repo")
        with pytest.raises(ValueError, match="Invalid issue reference"):
            api._parse_issue_reference("invalid")

    @patch("subprocess.run")
    def test_get_issue_id(self, mock_run: MagicMock) -> None:
        """Verify that issue ID can be obtained."""
        mock_run.return_value = MagicMock(
            stdout="I_kwDOOakzpM6yyU6H\n",
            returncode=0,
        )

        api = GitHubSubIssueAPI(repo="owner/repo")
        issue_id = api._get_issue_id("owner/repo", 123)
        assert issue_id == "I_kwDOOakzpM6yyU6H"

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "gh"
        assert args[1] == "issue"
        assert args[2] == "view"
        assert args[3] == "123"

    @patch("subprocess.run")
    def test_add_sub_issue(self, mock_run: MagicMock) -> None:
        """Verify that sub-issue can be added."""
        # _get_issue_id call (2 times)
        mock_run.side_effect = [
            MagicMock(stdout="I_parent\n", returncode=0),  # parent ID
            MagicMock(stdout="I_child\n", returncode=0),   # child ID
            MagicMock(  # GraphQL mutation
                stdout=json.dumps({
                    "data": {
                        "addSubIssue": {
                            "issue": {"number": 123, "title": "Parent"},
                            "subIssue": {"number": 456, "title": "Child"},
                        }
                    }
                }),
                returncode=0,
            ),
        ]

        api = GitHubSubIssueAPI(repo="owner/repo")
        result = api.add_sub_issue("123", "456")

        assert result["data"]["addSubIssue"]["issue"]["number"] == 123
        assert result["data"]["addSubIssue"]["subIssue"]["number"] == 456

        # Verify GraphQL mutation call
        graphql_call = mock_run.call_args_list[2]
        args = graphql_call[0][0]
        assert args[0] == "gh"
        assert args[1] == "api"
        assert args[2] == "graphql"
        assert args[3] == "-H"
        assert args[4] == "GraphQL-Features: sub_issues"

    @patch("subprocess.run")
    def test_remove_sub_issue(self, mock_run: MagicMock) -> None:
        """Verify that sub-issue can be removed."""
        mock_run.side_effect = [
            MagicMock(stdout="I_parent\n", returncode=0),
            MagicMock(stdout="I_child\n", returncode=0),
            MagicMock(
                stdout=json.dumps({
                    "data": {
                        "removeSubIssue": {
                            "issue": {"number": 123, "title": "Parent"},
                            "subIssue": {"number": 456, "title": "Child"},
                        }
                    }
                }),
                returncode=0,
            ),
        ]

        api = GitHubSubIssueAPI(repo="owner/repo")
        result = api.remove_sub_issue("123", "456")

        assert result["data"]["removeSubIssue"]["issue"]["number"] == 123
        assert result["data"]["removeSubIssue"]["subIssue"]["number"] == 456

    @patch("subprocess.run")
    def test_list_sub_issues(self, mock_run: MagicMock) -> None:
        """Verify that list of sub-issues can be obtained."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "data": {
                    "repository": {
                        "issue": {
                            "number": 123,
                            "title": "Parent",
                            "subIssues": {
                                "nodes": [
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
                                        "state": "CLOSED",
                                        "url": "https://github.com/owner/repo/issues/457",
                                        "assignees": {"nodes": []},
                                    },
                                ]
                            },
                            "subIssuesSummary": {
                                "total": 2,
                                "completed": 1,
                                "percentCompleted": 50,
                            },
                        }
                    }
                }
            }),
            returncode=0,
        )

        api = GitHubSubIssueAPI(repo="owner/repo")
        sub_issues = api.list_sub_issues("123", "OPEN")

        assert len(sub_issues) == 1
        assert sub_issues[0]["number"] == 456
        assert sub_issues[0]["state"] == "OPEN"

    @patch("subprocess.run")
    def test_create_sub_issue(self, mock_run: MagicMock) -> None:
        """Verify that a new sub-issue can be created."""
        mock_run.side_effect = [
            # gh issue create
            MagicMock(stdout="https://github.com/owner/repo/issues/789\n", returncode=0),
            # _get_issue_id (parent)
            MagicMock(stdout="I_parent\n", returncode=0),
            # _get_issue_id (new issue)
            MagicMock(stdout="I_new\n", returncode=0),
            # addSubIssue mutation
            MagicMock(
                stdout=json.dumps({
                    "data": {
                        "addSubIssue": {
                            "issue": {"number": 123, "title": "Parent"},
                            "subIssue": {"number": 789, "title": "New Issue"},
                        }
                    }
                }),
                returncode=0,
            ),
        ]

        api = GitHubSubIssueAPI(repo="owner/repo")
        result = api.create_sub_issue(
            "123",
            "New Issue",
            body="Description",
            labels=["bug"],
            assignees=["user1"],
        )

        assert result["number"] == 789
        assert result["title"] == "New Issue"
        assert "https://github.com/owner/repo/issues/789" in result["url"]

        # Verify gh issue create call
        create_call = mock_run.call_args_list[0]
        args = create_call[0][0]
        assert args[0] == "gh"
        assert args[1] == "issue"
        assert args[2] == "create"
        assert "--title" in args
        assert "New Issue" in args
        assert "--body" in args
        assert "Description" in args
        assert "--label" in args
        assert "bug" in args
        assert "--assignee" in args
        assert "user1" in args

    @patch("subprocess.run")
    def test_create_sub_issue_with_body_file(self, mock_run: MagicMock) -> None:
        """Verify that a new sub-issue can be created with body from file."""
        mock_run.side_effect = [
            # gh issue create
            MagicMock(stdout="https://github.com/owner/repo/issues/789\n", returncode=0),
            # _get_issue_id (parent)
            MagicMock(stdout="I_parent\n", returncode=0),
            # _get_issue_id (new issue)
            MagicMock(stdout="I_new\n", returncode=0),
            # addSubIssue mutation
            MagicMock(
                stdout=json.dumps({
                    "data": {
                        "addSubIssue": {
                            "issue": {"number": 123, "title": "Parent"},
                            "subIssue": {"number": 789, "title": "New Issue"},
                        }
                    }
                }),
                returncode=0,
            ),
        ]

        api = GitHubSubIssueAPI(repo="owner/repo")
        result = api.create_sub_issue(
            "123",
            "New Issue",
            body_file="/path/to/body.txt",
            labels=["bug"],
        )

        assert result["number"] == 789
        assert result["title"] == "New Issue"
        assert "https://github.com/owner/repo/issues/789" in result["url"]

        # Verify gh issue create call
        create_call = mock_run.call_args_list[0]
        args = create_call[0][0]
        assert args[0] == "gh"
        assert args[1] == "issue"
        assert args[2] == "create"
        assert "--title" in args
        assert "New Issue" in args
        assert "--body-file" in args
        assert "/path/to/body.txt" in args
        assert "--label" in args
        assert "bug" in args
        # Should not have --body when body_file is used
        body_indices = [i for i, x in enumerate(args) if x == "--body"]
        assert len(body_indices) == 0

    @patch("subprocess.run")
    def test_create_sub_issue_body_precedence(self, mock_run: MagicMock) -> None:
        """Verify that body takes precedence over body_file when both are provided."""
        mock_run.side_effect = [
            # gh issue create
            MagicMock(stdout="https://github.com/owner/repo/issues/789\n", returncode=0),
            # _get_issue_id (parent)
            MagicMock(stdout="I_parent\n", returncode=0),
            # _get_issue_id (new issue)
            MagicMock(stdout="I_new\n", returncode=0),
            # addSubIssue mutation
            MagicMock(
                stdout=json.dumps({
                    "data": {
                        "addSubIssue": {
                            "issue": {"number": 123, "title": "Parent"},
                            "subIssue": {"number": 789, "title": "New Issue"},
                        }
                    }
                }),
                returncode=0,
            ),
        ]

        api = GitHubSubIssueAPI(repo="owner/repo")
        result = api.create_sub_issue(
            "123",
            "New Issue",
            body="Direct body text",
            body_file="/path/to/body.txt",
        )

        assert result["number"] == 789
        assert result["title"] == "New Issue"

        # Verify gh issue create call - body should take precedence over body-file
        create_call = mock_run.call_args_list[0]
        args = create_call[0][0]
        assert args[0] == "gh"
        assert args[1] == "issue"
        assert args[2] == "create"
        assert "--body" in args
        assert "Direct body text" in args
        # Should not have --body-file when body is also provided
        body_file_indices = [i for i, x in enumerate(args) if x == "--body-file"]
        assert len(body_file_indices) == 0
