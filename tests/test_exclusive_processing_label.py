"""Tests for exclusive processing using @auto-coder label."""

from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.github_client import GitHubClient
from src.auto_coder.issue_processor import (_process_issues_jules_mode,
                                            _process_issues_normal)
from src.auto_coder.pr_processor import process_pull_requests


class TestGitHubClientExclusiveLabels:
    """Test GitHubClient label management methods."""

    def test_try_add_work_in_progress_label_success(self):
        """Test successfully adding @auto-coder label
        when it doesn't exist.
        """
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()
        mock_label1 = Mock()
        mock_label1.name = "bug"
        mock_label2 = Mock()
        mock_label2.name = "enhancement"
        mock_issue.labels = [mock_label1, mock_label2]

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token")
        client.github = mock_github

        result = client.try_add_work_in_progress_label("owner/repo", 123)

        assert result is True
        mock_issue.edit.assert_called_once()
        # Check that @auto-coder was added to labels
        call_args = mock_issue.edit.call_args
        assert "@auto-coder" in call_args.kwargs["labels"]

    def test_try_add_work_in_progress_label_already_exists(self):
        """Test that adding @auto-coder label fails when it already exists."""
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()
        mock_label1 = Mock()
        mock_label1.name = "@auto-coder"
        mock_label2 = Mock()
        mock_label2.name = "bug"
        mock_issue.labels = [mock_label1, mock_label2]

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token")
        client.github = mock_github

        result = client.try_add_work_in_progress_label("owner/repo", 123)

        assert result is False
        mock_issue.edit.assert_not_called()

    def test_remove_labels_from_issue(self):
        """Test removing labels from an issue."""
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()
        mock_label1 = Mock()
        mock_label1.name = "@auto-coder"
        mock_label2 = Mock()
        mock_label2.name = "bug"
        mock_issue.labels = [mock_label1, mock_label2]

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token")
        client.github = mock_github

        client.remove_labels_from_issue("owner/repo", 123, ["@auto-coder"])

        mock_issue.edit.assert_called_once()
        call_args = mock_issue.edit.call_args
        assert "@auto-coder" not in call_args.kwargs["labels"]
        assert "bug" in call_args.kwargs["labels"]

    def test_has_label_true(self):
        """Test checking if an issue has a specific label (exists)."""
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()
        mock_label1 = Mock()
        mock_label1.name = "@auto-coder"
        mock_label2 = Mock()
        mock_label2.name = "bug"
        mock_issue.labels = [mock_label1, mock_label2]

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token")
        client.github = mock_github

        result = client.has_label("owner/repo", 123, "@auto-coder")

        assert result is True

    def test_has_label_false(self):
        """Test checking if an issue has a specific label (doesn't exist)."""
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()
        mock_label1 = Mock()
        mock_label1.name = "bug"
        mock_issue.labels = [mock_label1]

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token")
        client.github = mock_github

        result = client.has_label("owner/repo", 123, "@auto-coder")

        assert result is False

    def test_disable_labels_add_labels(self):
        """Test that add_labels_to_issue is a no-op when disable_labels=True."""
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token", disable_labels=True)
        client.github = mock_github

        client.add_labels_to_issue("owner/repo", 123, ["test-label"])

        # Should not call get_issue or edit when labels are disabled
        mock_repo.get_issue.assert_not_called()
        mock_issue.edit.assert_not_called()

    def test_disable_labels_remove_labels(self):
        """Test that remove_labels_from_issue is a no-op when disable_labels=True."""
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token", disable_labels=True)
        client.github = mock_github

        client.remove_labels_from_issue("owner/repo", 123, ["test-label"])

        # Should not call get_issue or edit when labels are disabled
        mock_repo.get_issue.assert_not_called()
        mock_issue.edit.assert_not_called()

    def test_disable_labels_has_label(self):
        """Test that has_label returns False when disable_labels=True."""
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token", disable_labels=True)
        client.github = mock_github

        result = client.has_label("owner/repo", 123, "@auto-coder")

        # Should return False without calling get_issue
        assert result is False
        mock_repo.get_issue.assert_not_called()

    def test_disable_labels_try_add_work_in_progress_label(self):
        """Test that try_add_work_in_progress_label returns True when disable_labels=True."""
        mock_github = Mock()
        mock_repo = Mock()
        mock_issue = Mock()

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo

        client = GitHubClient("fake-token", disable_labels=True)
        client.github = mock_github

        result = client.try_add_work_in_progress_label("owner/repo", 123)

        # Should return True to allow processing to continue
        assert result is True
        # Should not call get_issue or edit when labels are disabled
        mock_repo.get_issue.assert_not_called()
        mock_issue.edit.assert_not_called()


class TestIssueProcessorExclusiveProcessing:
    """Test issue processor exclusive processing with @auto-coder label."""

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_normal_skips_when_label_exists(self, mock_take_actions):
        """Test that issues with @auto-coder label
        are skipped.
        """
        mock_github_client = Mock()
        mock_github_client.disable_labels = False  # Labels enabled
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Test Issue",
            "labels": ["@auto-coder"],  # Label already exists
        }

        config = AutomationConfig()
        result = _process_issues_normal(mock_github_client, config, False, "owner/repo")

        assert len(result) == 1
        actions_taken = result[0]["actions_taken"][0]
        assert "Skipped - already being processed" in actions_taken
        mock_take_actions.assert_not_called()
        # Verify try_add_work_in_progress_label was NOT called (skipped before that check)
        mock_github_client.try_add_work_in_progress_label.assert_not_called()

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_normal_processes_when_label_added(self, mock_take_actions):
        """Test that issues are processed when
        @auto-coder label is successfully added.
        """
        mock_github_client = Mock()
        mock_github_client.disable_labels = False  # Labels enabled
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Test Issue",
            "labels": [],
        }
        # Mock get_open_sub_issues to return empty list (no sub-issues)
        mock_github_client.get_open_sub_issues.return_value = []
        # Mock has_linked_pr to return False (no linked PR)
        mock_github_client.has_linked_pr.return_value = False
        # Simulate label successfully added
        mock_github_client.try_add_work_in_progress_label.return_value = True
        mock_take_actions.return_value = ["Action taken"]

        config = AutomationConfig()
        result = _process_issues_normal(mock_github_client, config, False, "owner/repo")

        assert len(result) == 1
        assert result[0]["actions_taken"] == ["Action taken"]
        mock_take_actions.assert_called_once()
        # Verify label was removed after processing
        mock_github_client.remove_labels_from_issue.assert_called_once_with(
            "owner/repo", 123, ["@auto-coder"]
        )

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_normal_removes_label_on_error(self, mock_take_actions):
        """Test that @auto-coder label is
        removed even when processing fails.
        """
        mock_github_client = Mock()
        mock_github_client.disable_labels = False  # Labels enabled
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Test Issue",
            "labels": [],
        }
        mock_github_client.try_add_work_in_progress_label.return_value = True
        mock_github_client.has_linked_pr.return_value = False
        # Simulate error during processing
        mock_take_actions.side_effect = Exception("Processing failed")

        config = AutomationConfig()
        _ = _process_issues_normal(mock_github_client, config, False, "owner/repo")

        # Verify label removal was attempted
        mock_github_client.remove_labels_from_issue.assert_called_with(
            "owner/repo", 123, ["@auto-coder"]
        )

    def test_process_issues_jules_mode_skips_when_label_exists(self):
        """Test that Jules mode skips issues
        with @auto-coder label.
        """
        mock_github_client = Mock()
        mock_github_client.disable_labels = False  # Labels enabled
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Test Issue",
            "labels": ["@auto-coder"],  # Label already exists
        }

        config = AutomationConfig()
        result = _process_issues_jules_mode(
            mock_github_client, config, False, "owner/repo"
        )

        assert len(result) == 1
        actions_taken = result[0]["actions_taken"][0]
        assert "Skipped - already being processed" in actions_taken
        # Verify try_add_work_in_progress_label was NOT called (skipped before that check)
        mock_github_client.try_add_work_in_progress_label.assert_not_called()
        # Verify jules label was not added
        mock_github_client.add_labels_to_issue.assert_not_called()


class TestPRProcessorExclusiveProcessing:
    """Test PR processor exclusive processing with @auto-coder label."""

    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.check_for_updates_and_restart")
    def test_process_pull_requests_skips_when_label_exists(
        self, mock_check_updates, mock_check_actions
    ):
        """Test that PRs with @auto-coder label are skipped."""
        mock_github_client = Mock()
        mock_github_client.disable_labels = False  # Labels enabled
        mock_pr = Mock()
        mock_pr.number = 456

        mock_github_client.get_open_pull_requests.return_value = [mock_pr]
        mock_github_client.get_pr_details.return_value = {
            "number": 456,
            "title": "Test PR",
            "mergeable": True,
        }
        # Simulate label already exists
        mock_github_client.try_add_work_in_progress_label.return_value = False

        config = AutomationConfig()
        result = process_pull_requests(mock_github_client, config, False, "owner/repo")

        # Should have skipped entries in both passes
        skipped_count = sum(
            1
            for pr in result
            if any(
                "Skipped - already being processed" in action
                for action in pr.get("actions_taken", [])
            )
        )
        assert skipped_count >= 1

    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.check_for_updates_and_restart")
    def test_process_pull_requests_skips_when_label_present_even_if_labels_disabled(
        self, mock_check_updates, mock_check_actions
    ):
        """Ensure PRs are skipped when @auto-coder label is present
        even if label operations are disabled."""
        mock_github_client = Mock()
        mock_github_client.disable_labels = True  # Labels disabled
        mock_pr = Mock()
        mock_pr.number = 789

        mock_github_client.get_open_pull_requests.return_value = [mock_pr]
        mock_github_client.get_pr_details.return_value = {
            "number": 789,
            "title": "Test PR",
            "labels": ["@auto-coder"],  # Label already present on PR
            "mergeable": False,
        }

        config = AutomationConfig()
        result = process_pull_requests(mock_github_client, config, False, "owner/repo")

        assert any(
            any(
                "Skipped - already being processed" in action
                for action in pr.get("actions_taken", [])
            )
            for pr in result
        )


class TestIssueProcessorWithDisabledLabels:
    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_skips_on_label_even_if_labels_disabled(
        self, mock_take_actions
    ):
        """Ensure issues are skipped when @auto-coder label is present
        even if label operations are disabled."""
        mock_github_client = Mock()
        mock_github_client.disable_labels = True  # Labels disabled
        mock_issue = Mock()
        mock_issue.number = 321

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 321,
            "title": "Test Issue",
            "labels": ["@auto-coder"],  # Label already exists
        }

        config = AutomationConfig()
        result = _process_issues_normal(mock_github_client, config, False, "owner/repo")

        assert len(result) == 1
        actions_taken = result[0]["actions_taken"][0]
        assert "Skipped - already being processed" in actions_taken
        mock_take_actions.assert_not_called()
        # verify we never attempted to add/remove labels
        mock_github_client.try_add_work_in_progress_label.assert_not_called()
        mock_github_client.remove_labels_from_issue.assert_not_called()
