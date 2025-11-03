"""CLI のテスト."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from gh_sub_issue.cli import main


class TestCLI:
    """CLI のテストクラス."""

    def setup_method(self) -> None:
        """各テストの前に実行."""
        self.runner = CliRunner()

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_add_command(self, mock_api_class: MagicMock) -> None:
        """add コマンドが正しく動作することを確認."""
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

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_add_command_with_replace_parent(self, mock_api_class: MagicMock) -> None:
        """add コマンドで --replace-parent オプションが動作することを確認."""
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

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_create_command(self, mock_api_class: MagicMock) -> None:
        """create コマンドが正しく動作することを確認."""
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
                "--parent",
                "123",
                "--title",
                "New Issue",
                "--body",
                "Description",
                "--label",
                "bug",
                "--assignee",
                "user1",
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
        )

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_list_command(self, mock_api_class: MagicMock) -> None:
        """list コマンドが正しく動作することを確認."""
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

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_list_command_json_output(self, mock_api_class: MagicMock) -> None:
        """list コマンドで JSON 出力が動作することを確認."""
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

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_list_command_empty(self, mock_api_class: MagicMock) -> None:
        """list コマンドで sub-issue がない場合の動作を確認."""
        mock_api = MagicMock()
        mock_api.list_sub_issues.return_value = []
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["list", "123"])

        assert result.exit_code == 0
        assert "No open sub-issues found" in result.output

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_remove_command_with_force(self, mock_api_class: MagicMock) -> None:
        """remove コマンドで --force オプションが動作することを確認."""
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

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_remove_command_multiple(self, mock_api_class: MagicMock) -> None:
        """remove コマンドで複数の sub-issue を削除できることを確認."""
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

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_remove_command_with_confirmation(self, mock_api_class: MagicMock) -> None:
        """remove コマンドで確認プロンプトが表示されることを確認."""
        mock_api = MagicMock()
        mock_api_class.return_value = mock_api

        # ユーザーが 'n' を入力してキャンセル
        result = self.runner.invoke(main, ["remove", "123", "456"], input="n\n")

        assert result.exit_code == 0
        assert "続行しますか?" in result.output
        assert "キャンセルしました" in result.output
        mock_api.remove_sub_issue.assert_not_called()

    @patch("gh_sub_issue.cli.GitHubSubIssueAPI")
    def test_repo_option(self, mock_api_class: MagicMock) -> None:
        """--repo オプションが正しく渡されることを確認."""
        mock_api = MagicMock()
        mock_api.list_sub_issues.return_value = []
        mock_api_class.return_value = mock_api

        result = self.runner.invoke(main, ["--repo", "owner/repo", "list", "123"])

        assert result.exit_code == 0
        mock_api_class.assert_called_once_with(repo="owner/repo")
