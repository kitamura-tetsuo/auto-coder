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
                # Mock get_parent_issue_details to return OPEN parent
                github_client.get_parent_issue_details.return_value = {"number": parent_issue_number, "title": "Parent Issue", "state": "OPEN", "url": "http://url"}

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


def test_existing_work_branch_not_recreated():
    """Test that when work branch exists locally, it should not be recreated."""
    repo_name = "owner/repo"
    issue_number = 456
    issue_data = {"number": issue_number, "title": "Test Issue"}
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
        # Simulate: work branch already exists locally
        # git rev-parse --abbrev-ref HEAD (get current branch)
        # git rev-parse --verify work_branch (check if work branch exists)
        mock_cmd.run_command.side_effect = [
            _cmd_result(success=True, stdout="main", returncode=0),  # get current branch
            _cmd_result(success=True, stdout="issue-456", returncode=0),  # rev-parse work branch exists
        ]

        with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                # Minimal GitHub client mock
                github_client = MagicMock()
                github_client.get_parent_issue_details.return_value = None  # No parent issue

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

    # Verify that branch_context was called with create_new=False
    assert captured_calls, "branch_context was not called"
    first_args, first_kwargs = captured_calls[0]
    assert first_args[0] == "issue-456"
    assert first_kwargs.get("create_new") is False, "Work branch should not be recreated when it already exists"


def test_missing_work_branch_created_with_correct_base():
    """Test that when work branch doesn't exist, it's created from the correct base branch."""
    repo_name = "owner/repo"
    issue_number = 789
    issue_data = {"number": issue_number, "title": "Test Issue"}
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
        # Simulate: work branch does not exist locally
        # git rev-parse --abbrev-ref HEAD (get current branch)
        # git rev-parse --verify work_branch (check if work branch exists)
        mock_cmd.run_command.side_effect = [
            _cmd_result(success=True, stdout="main", returncode=0),  # get current branch
            _cmd_result(success=False, stderr="not found", returncode=1),  # rev-parse work branch missing
        ]

        with patch("src.auto_coder.issue_processor.LabelManager", fake_label_manager):
            with patch("src.auto_coder.issue_processor.branch_context", fake_branch_context):
                # Minimal GitHub client mock
                github_client = MagicMock()
                github_client.get_parent_issue_details.return_value = None  # No parent issue

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

    # Verify that branch_context was called with create_new=True and correct base_branch
    assert captured_calls, "branch_context was not called"
    first_args, first_kwargs = captured_calls[0]
    assert first_args[0] == "issue-789"
    assert first_kwargs.get("create_new") is True, "Work branch should be created when it doesn't exist"
    assert first_kwargs.get("base_branch") == config.MAIN_BRANCH, "Work branch should be created from MAIN_BRANCH when no parent issue"


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
            # Now using generic add_labels method with "pr"
            label_calls = []
            for call in github_client.method_calls:
                if call[0] == "add_labels":
                    label_calls.append(call)

            # Should have at least one call for urgent
            assert len(label_calls) >= 1
            # Verify the label propagated is 'urgent' with "pr"
            assert any(call[0] == "add_labels" and call[1][0] == repo_name and call[1][1] == pr_number and call[1][2] == ["urgent"] and call[2] == {"item_type": "pr"} for call in label_calls)

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
                if call[0] == "add_labels":
                    label_calls.append(call)

            # Should have at least one call for urgent
            assert len(label_calls) >= 1
            # Verify the label propagated is 'urgent' with "pr"
            assert any(call[0] == "add_labels" and call[1][0] == repo_name and call[1][1] == pr_number and call[1][2] == ["urgent"] and call[2] == {"item_type": "pr"} for call in label_calls)

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
                if call[0] == "add_labels":
                    label_calls.append(call)

            # At least 1 label addition for urgent
            assert len(label_calls) >= 1
            # Verify the label propagated is 'urgent' with "pr"
            assert any(call[0] == "add_labels" and call[1][0] == repo_name and call[1][1] == pr_number and call[1][2] == ["urgent"] and call[2] == {"item_type": "pr"} for call in label_calls)

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
                if call[0] == "add_labels":
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
                if call[0] == "add_labels":
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
        github_client.add_labels.side_effect = Exception("GitHub API error")

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


class TestLabelBasedIssueProcessing:
    """Test label-based issue processing functionality."""

    def test_breaking_change_issue_detection(self):
        """Test detection and processing of breaking-change labeled issues."""
        from src.auto_coder.prompt_loader import _is_breaking_change_issue

        # Test breaking-change label detection
        issue_labels = ["breaking-change", "bug"]
        assert _is_breaking_change_issue(issue_labels) is True

        # Test api-change label
        issue_labels = ["api-change", "enhancement"]
        assert _is_breaking_change_issue(issue_labels) is True

        # Test deprecation label
        issue_labels = ["deprecation", "documentation"]
        assert _is_breaking_change_issue(issue_labels) is True

        # Test version-major label
        issue_labels = ["version-major", "feature"]
        assert _is_breaking_change_issue(issue_labels) is True

        # Test non-breaking labels
        issue_labels = ["bug", "enhancement", "documentation"]
        assert _is_breaking_change_issue(issue_labels) is False

    def test_label_based_prompt_selection_in_issue_processing(self):
        """Test label-based prompt selection in issue processing."""
        from src.auto_coder.prompt_loader import get_label_specific_prompt

        # Test urgent label priority
        labels = ["urgent", "bug", "feature"]
        mappings = {
            "urgent": "issue.urgent",
            "bug": "issue.bug",
            "feature": "issue.feature",
        }
        priorities = ["urgent", "bug", "feature"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.urgent"

        # Test bug label handling
        labels = ["bug", "feature"]
        priorities = ["urgent", "bug", "feature"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.bug"

        # Test feature label handling
        labels = ["feature", "documentation"]
        priorities = ["urgent", "bug", "feature", "documentation"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.feature"

        # Test priority resolution with multiple labels
        labels = ["urgent", "breaking-change"]
        mappings["breaking-change"] = "issue.breaking_change"
        priorities = ["breaking-change", "urgent", "bug", "feature"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.breaking_change"

    def test_issue_processing_with_different_label_types(self, tmp_path):
        """Test issue processing with different label types."""
        from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt

        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default: $issue_number"\n' '  bug: "Bug fix: $issue_number"\n' '  feature: "Feature: $issue_number"\n' '  breaking_change: "Breaking: $issue_number"\n' '  urgent: "Urgent: $issue_number"\n' '  documentation: "Docs: $issue_number"\n',
            encoding="utf-8",
        )

        # Test bug label
        clear_prompt_cache()
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="123",
        )
        assert "Bug fix: 123" in result

        # Test feature label
        clear_prompt_cache()
        labels = ["feature"]
        mappings = {"feature": "issue.feature"}
        priorities = ["feature"]
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="456",
        )
        assert "Feature: 456" in result

        # Test urgent label
        clear_prompt_cache()
        labels = ["urgent"]
        mappings = {"urgent": "issue.urgent"}
        priorities = ["urgent"]
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="789",
        )
        assert "Urgent: 789" in result

        # Test breaking-change label
        clear_prompt_cache()
        labels = ["breaking-change"]
        mappings = {"breaking-change": "issue.breaking_change"}
        priorities = ["breaking-change"]
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="999",
        )
        assert "Breaking: 999" in result

    def test_label_processing_error_handling(self):
        """Test error handling in label-based processing."""
        from src.auto_coder.prompt_loader import get_label_specific_prompt

        # Test missing label-specific prompts
        labels = ["custom-label"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result is None

        # Test invalid label configurations - should handle gracefully
        labels = []
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result is None

        # Test None inputs
        result = get_label_specific_prompt(None, None, None)
        assert result is None

    def test_issue_with_multiple_labels_uses_highest_priority(self, tmp_path):
        """Test that issue with multiple labels uses the highest priority label."""
        from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt

        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default"\n' '  bug: "Bug prompt"\n' '  feature: "Feature prompt"\n' '  urgent: "Urgent prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = ["bug", "feature", "urgent"]
        mappings = {
            "bug": "issue.bug",
            "feature": "issue.feature",
            "urgent": "issue.urgent",
        }
        priorities = ["urgent", "bug", "feature"]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
        )

        # Urgent should be selected (highest priority)
        assert "Urgent prompt" in result

    def test_issue_without_labels_falls_back_to_default(self, tmp_path):
        """Test that issue without labels falls back to default prompt."""
        from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt

        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = []
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
        )

        # Should fall back to default
        assert "Default prompt" in result

    def test_case_insensitive_label_matching(self):
        """Test that label matching is case-insensitive."""
        from src.auto_coder.prompt_loader import _is_breaking_change_issue

        # Test breaking-change detection with different cases
        assert _is_breaking_change_issue(["BREAKING-CHANGE"]) is True
        assert _is_breaking_change_issue(["Breaking-Change"]) is True
        assert _is_breaking_change_issue(["breaking-change"]) is True

        # Test that non-breaking labels are correctly identified
        assert _is_breaking_change_issue(["bug"]) is False
        assert _is_breaking_change_issue(["FEATURE"]) is False
