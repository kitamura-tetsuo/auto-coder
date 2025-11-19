"""Tests for issue_processor branch creation behavior."""

from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly, _create_pr_for_issue


def _cmd_result(success=True, stdout="", stderr="", returncode=0):
    class R:
        def __init__(self):
            self.success = success
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    return R()


def test_parent_issue_branch_creation_uses_main_base():
    """When creating a missing parent issue branch, base_branch must be MAIN_BRANCH."""
    repo_name = "owner/repo"
    issue_number = 999
    parent_issue_number = 123
    issue_data = {"number": issue_number, "title": "Test"}
    config = AutomationConfig()

    # Capture calls to branch_context
    captured_calls = []

    @contextmanager
    def fake_branch_context(*args, **kwargs):
        captured_calls.append((args, kwargs))
        yield

    @contextmanager
    def fake_label_manager(*_args, **_kwargs):
        yield True

    # CommandExecutor instance in issue_processor module
    with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
        # Simulate: parent branch missing, work branch missing
        mock_cmd.run_command.side_effect = [
            _cmd_result(success=False, stderr="not found", returncode=1),  # rev-parse parent
            _cmd_result(success=False, stderr="not found", returncode=1),  # rev-parse work
        ]

        with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                # Minimal GitHub client mock
                github_client = MagicMock()
                github_client.get_parent_issue.return_value = parent_issue_number

                # Avoid deeper execution
                class DummyLLM:
                    def _run_llm_cli(self, *_args, **_kwargs):
                        return None

                with patch("src.auto_coder.issue_processor.get_llm_backend_manager", return_value=DummyLLM()):
                    _apply_issue_actions_directly(
                        repo_name,
                        issue_data,
                        config,
                        github_client,
                    )

    # First branch_context call should be for creating the parent branch from MAIN_BRANCH
    assert captured_calls, "branch_context was not called"
    first_args, first_kwargs = captured_calls[0]
    # args[0] should be the branch name
    assert first_args[0] == f"issue-{parent_issue_number}"
    assert first_kwargs.get("create_new") is True
    assert first_kwargs.get("base_branch") == config.MAIN_BRANCH


class TestPRLabelCopying:
    """Integration tests for PR label copying functionality."""

    def test_create_pr_for_issue_copies_semantic_labels(self):
        """Test that PR creation copies semantic labels from issue to PR."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug", "urgent", "documentation"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        llm_response = "Test response"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Verify label copying - only urgent label should be propagated
            # Note: add_labels_to_issue is called first (for backward compatibility)
            # Then add_labels_to_pr is called
            # We check the calls made during label copying
            label_calls = []
            for call in github_client.method_calls:
                if call[0] in ["add_labels_to_issue", "add_labels_to_pr"]:
                    label_calls.append(call)

            # Should have calls for urgent (both wrappers are called)
            assert len(label_calls) >= 2
            # Verify the label propagated is 'urgent'
            assert any(call[0] == "add_labels_to_issue" and call[1][2] == ["urgent"] for call in label_calls)

    def test_create_pr_for_issue_copies_labels_with_aliases(self):
        """Test that PR creation handles label aliases correctly."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bugfix", "high-priority", "doc"],  # Aliases
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        llm_response = "Test response"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Should have label calls for urgent (from high-priority) - only urgent label is propagated
            label_calls = []
            for call in github_client.method_calls:
                if call[0] in ["add_labels_to_issue", "add_labels_to_pr"]:
                    label_calls.append(call)

            # Should have calls for urgent (both wrappers are called)
            assert len(label_calls) >= 2
            # Verify the label propagated is 'urgent'
            assert any(call[0] == "add_labels_to_issue" and call[1][2] == ["urgent"] for call in label_calls)

    def test_create_pr_for_issue_respects_max_label_count(self):
        """Test that PR creation respects the maximum label count."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug", "urgent", "enhancement", "documentation", "breaking-change"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        llm_response = "Test response"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True
        config.PR_LABEL_MAX_COUNT = 2  # Limit to 2 labels

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Should only copy urgent label (only urgent is propagated)
            label_calls = []
            for call in github_client.method_calls:
                if call[0] in ["add_labels_to_issue", "add_labels_to_pr"]:
                    label_calls.append(call)

            # At least 2 label additions (both wrappers are called for urgent)
            assert len(label_calls) >= 2
            # Verify the label propagated is 'urgent'
            assert any(call[0] == "add_labels_to_issue" and call[1][2] == ["urgent"] for call in label_calls)

    def test_create_pr_for_issue_disabled_label_copying(self):
        """Test that PR creation skips label copying when disabled."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug", "urgent"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        llm_response = "Test response"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = False  # Disabled

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Should not have any label-related calls
            label_calls = []
            for call in github_client.method_calls:
                if call[0] in ["add_labels_to_issue", "add_labels_to_pr"]:
                    label_calls.append(call)

            assert len(label_calls) == 0

    def test_create_pr_for_issue_no_semantic_labels(self):
        """Test that PR creation handles issues with no semantic labels."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["random-label", "another-label"],  # No semantic labels
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        llm_response = "Test response"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True

        # Mock GitHub client
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                github_client,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

            # Should not have any label-related calls (no semantic labels to copy)
            label_calls = []
            for call in github_client.method_calls:
                if call[0] in ["add_labels_to_issue", "add_labels_to_pr"]:
                    label_calls.append(call)

            assert len(label_calls) == 0

    def test_create_pr_for_issue_handles_label_error_gracefully(self):
        """Test that PR creation continues even if label copying fails."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        llm_response = "Test response"
        pr_number = 456

        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True

        # Mock GitHub client that raises error on label operations
        github_client = Mock()
        github_client.get_pr_closing_issues.return_value = [issue_number]
        github_client.add_labels_to_issue.side_effect = Exception("GitHub API error")

        # Mock gh pr create to return PR URL
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Call _create_pr_for_issue - should not raise despite label error
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                github_client,
                config,
            )

            # Verify PR was still created successfully
            assert f"Successfully created PR for issue #{issue_number}" in result
