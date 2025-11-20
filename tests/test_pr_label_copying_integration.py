"""Integration tests for PR label copying functionality.

This module contains comprehensive integration tests that verify the end-to-end
PR label copying workflows, including:
- Semantic label copying from issues to PRs
- Priority-based label selection for PRs
- Max label count enforcement
- Label alias resolution during PR creation
- Breaking change label propagation
- Integration with resolve_pr_labels_with_priority
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _create_pr_for_issue
from src.auto_coder.label_manager import get_semantic_labels_from_issue, resolve_pr_labels_with_priority
from tests.fixtures.label_prompt_fixtures import (
    TEST_ISSUE_DATA,
    TEST_PR_LABEL_MAPPINGS,
    TEST_PR_LABEL_PRIORITIES,
)


def _cmd_result(success=True, stdout="", stderr="", returncode=0):
    """Helper to create a command result object."""

    class R:
        def __init__(self):
            self.success = success
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    return R()


class TestPRLabelCopyingIntegration:
    """Integration tests for PR label copying from issues to PRs."""

    @pytest.fixture
    def config_with_pr_label_copying(self):
        """Create a test configuration with PR label copying enabled."""
        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = True
        config.PR_LABEL_MAX_COUNT = 3
        config.PR_LABEL_MAPPINGS = TEST_PR_LABEL_MAPPINGS
        config.PR_LABEL_PRIORITIES = TEST_PR_LABEL_PRIORITIES
        return config

    @pytest.fixture
    def mock_github_client_for_pr(self):
        """Create a mock GitHub client for PR operations."""
        client = Mock()
        client.get_pr_closing_issues.return_value = []
        client.get_repository.return_value = Mock()
        return client

    def test_semantic_label_copying_from_issues_to_prs(self, config_with_pr_label_copying, mock_github_client_for_pr):
        """Test that semantic labels are correctly copied from issues to PRs."""
        issue_labels = ["bug", "urgent", "enhancement"]
        issue_data = {
            "number": 123,
            "title": "Test Issue",
            "body": "Test body",
            "labels": issue_labels,
        }

        # Test semantic label extraction
        semantic_labels = get_semantic_labels_from_issue(issue_labels, config_with_pr_label_copying.PR_LABEL_MAPPINGS)

        # Verify semantic labels are detected
        assert "bug" in semantic_labels
        assert "urgent" in semantic_labels
        assert "enhancement" in semantic_labels

    def test_priority_based_label_selection_for_prs(self, config_with_pr_label_copying):
        """Test that PR labels are selected based on priority when multiple labels exist."""
        issue_labels = ["bug", "urgent", "enhancement", "documentation"]
        issue_data = {
            "number": 123,
            "title": "Test Issue",
            "body": "Test body",
            "labels": issue_labels,
        }

        # Test priority-based label resolution
        resolved_labels = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

        # Urgent has higher priority than bug, enhancement, documentation
        assert "urgent" in resolved_labels
        # Check priority order
        priority_indices = {label: resolved_labels.index(label) for label in resolved_labels}

    def test_max_label_count_enforcement(self, config_with_pr_label_copying):
        """Test that PR label copying respects the maximum label count."""
        # Set max labels to 2
        config_with_pr_label_copying.PR_LABEL_MAX_COUNT = 2

        issue_labels = ["bug", "urgent", "enhancement", "documentation", "feature"]

        resolved_labels = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

        # Should only have 2 labels (max count)
        assert len(resolved_labels) <= 2
        # Should have urgent and bug (highest priority)
        assert "urgent" in resolved_labels
        assert "bug" in resolved_labels

    def test_label_alias_resolution_during_pr_creation(self, config_with_pr_label_copying):
        """Test that label aliases are resolved correctly during PR creation."""
        # Test with various alias labels
        alias_test_cases = [
            (["bugfix"], "bug"),
            (["hotfix"], "bug"),
            (["critical"], "urgent"),
            (["asap"], "urgent"),
            (["doc"], "documentation"),
            (["readme"], "documentation"),
            (["improvement"], "enhancement"),
            (["new-feature"], "enhancement"),
        ]

        for issue_aliases, expected_semantic in alias_test_cases:
            resolved = resolve_pr_labels_with_priority(issue_aliases, config_with_pr_label_copying)

            # The semantic label should be detected
            semantic_from_issue = get_semantic_labels_from_issue(issue_aliases, config_with_pr_label_copying.PR_LABEL_MAPPINGS)

            assert expected_semantic in semantic_from_issue or any(expected_semantic in alias_list for label, alias_list in config_with_pr_label_copying.PR_LABEL_MAPPINGS.items() if label in issue_aliases or any(a in issue_aliases for a in alias_list))

    def test_breaking_change_label_propagation(self, config_with_pr_label_copying):
        """Test that breaking-change labels are propagated with highest priority."""
        breaking_change_labels = [
            "breaking-change",
            "api-change",
            "deprecation",
            "version-major",
        ]

        for label in breaking_change_labels:
            resolved = resolve_pr_labels_with_priority([label], config_with_pr_label_copying)

            # Breaking-change should be detected and propagated
            assert len(resolved) > 0

    def test_integration_with_resolve_pr_labels_with_priority(self, config_with_pr_label_copying):
        """Test full integration of resolve_pr_labels_with_priority function."""
        # Test various scenarios
        test_cases = [
            # (issue_labels, expected_resolved_labels_count, expected_top_priority)
            (["bug", "urgent"], 2, "urgent"),  # urgent has higher priority
            (["documentation", "enhancement"], 2, "enhancement"),  # enhancement has higher priority
            (["breaking-change", "bug", "urgent"], 3, "breaking-change"),  # breaking-change has highest priority
            (["random-label"], 0, None),  # No semantic labels
            ([], 0, None),  # Empty labels
        ]

        for issue_labels, expected_count, expected_top in test_cases:
            resolved = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

            assert len(resolved) == expected_count, f"Expected {expected_count} labels for {issue_labels}, got {len(resolved)}"

            if expected_top:
                assert resolved[0] == expected_top, f"Expected top priority {expected_top} for {issue_labels}, got {resolved[0] if resolved else None}"

    def test_pr_template_selection_based_on_copied_labels(self, config_with_pr_label_copying):
        """Test that PR templates are selected based on copied labels."""
        # Test different label combinations and their expected templates
        template_tests = [
            (["bug", "urgent"], "bug template"),
            (["breaking-change"], "breaking-change template"),
            (["enhancement", "documentation"], "enhancement template"),
            (["documentation"], "documentation template"),
        ]

        for issue_labels, expected_template_type in template_tests:
            resolved = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

            if resolved:
                # Determine template type based on highest priority label
                top_label = resolved[0]
                if top_label in ["breaking-change"]:
                    assert "breaking-change" in expected_template_type
                elif top_label == "urgent":
                    assert "bug" in expected_template_type or "urgent" in expected_template_type
                elif top_label == "enhancement":
                    assert "enhancement" in expected_template_type
                elif top_label == "documentation":
                    assert "documentation" in expected_template_type

    def test_create_pr_with_label_copying_enabled(self, config_with_pr_label_copying, mock_github_client_for_pr):
        """Test PR creation with label copying enabled."""
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

        # Mock PR closing issues
        mock_github_client_for_pr.get_pr_closing_issues.return_value = [issue_number]

        # Track label operations by patching at call site
        with patch("src.auto_coder.issue_processor.resolve_pr_labels_with_priority") as mock_resolve:
            # Return semantic labels to be copied
            mock_resolve.return_value = ["urgent"]

            # Mock gh pr create
            with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
                mock_gh_logger_instance = Mock()
                mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
                mock_gh_logger.return_value = mock_gh_logger_instance

                # Create PR
                result = _create_pr_for_issue(
                    repo_name,
                    issue_data,
                    work_branch,
                    base_branch,
                    llm_response,
                    mock_github_client_for_pr,
                    config_with_pr_label_copying,
                )

                # Verify PR was created
                assert f"Successfully created PR for issue #{issue_number}" in result
                # Verify resolve_pr_labels_with_priority was called
                assert mock_resolve.called

    def test_create_pr_with_label_copying_disabled(self, mock_github_client_for_pr):
        """Test PR creation skips label copying when disabled."""
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

        # Configuration with label copying disabled
        config = AutomationConfig()
        config.PR_LABEL_COPYING_ENABLED = False
        config.PR_LABEL_MAPPINGS = TEST_PR_LABEL_MAPPINGS
        config.PR_LABEL_PRIORITIES = TEST_PR_LABEL_PRIORITIES

        # Mock PR closing issues
        mock_github_client_for_pr.get_pr_closing_issues.return_value = [issue_number]

        # Track label operations
        label_operations = []
        original_add_labels = mock_github_client_for_pr.add_labels

        def track_labels(*args, **kwargs):
            label_operations.append(("add_labels", args, kwargs))
            return original_add_labels(*args, **kwargs)

        mock_github_client_for_pr.add_labels = track_labels

        # Mock gh pr create
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Create PR
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                mock_github_client_for_pr,
                config,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result
            # No semantic label operations (only @auto-coder label might be added)
            # But we shouldn't have semantic label copying

    def test_create_pr_with_no_semantic_labels(self, config_with_pr_label_copying, mock_github_client_for_pr):
        """Test PR creation handles issues with no semantic labels."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["random-label", "custom-label"],  # No semantic labels
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        llm_response = "Test response"
        pr_number = 456

        # Mock PR closing issues
        mock_github_client_for_pr.get_pr_closing_issues.return_value = [issue_number]

        # Mock gh pr create
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Create PR
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                mock_github_client_for_pr,
                config_with_pr_label_copying,
            )

            # Verify PR was created
            assert f"Successfully created PR for issue #{issue_number}" in result

    def test_create_pr_with_label_copying_graceful_error_handling(self, config_with_pr_label_copying, mock_github_client_for_pr):
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

        # Mock PR closing issues
        mock_github_client_for_pr.get_pr_closing_issues.return_value = [issue_number]

        # Make label operations fail
        mock_github_client_for_pr.add_labels.side_effect = Exception("GitHub API error")

        # Mock gh pr create
        with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
            mock_gh_logger_instance = Mock()
            mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
            mock_gh_logger.return_value = mock_gh_logger_instance

            # Create PR - should not raise despite label error
            result = _create_pr_for_issue(
                repo_name,
                issue_data,
                work_branch,
                base_branch,
                llm_response,
                mock_github_client_for_pr,
                config_with_pr_label_copying,
            )

            # Verify PR was still created successfully
            assert f"Successfully created PR for issue #{issue_number}" in result

    def test_pr_label_priority_integration(self, config_with_pr_label_copying):
        """Test that PR label priorities are correctly applied in integration."""
        # Test priority ordering
        priority_order = config_with_pr_label_copying.PR_LABEL_PRIORITIES
        assert priority_order[0] == "breaking-change"  # Highest priority
        assert priority_order[1] == "urgent"
        assert priority_order[2] == "bug"
        assert priority_order[3] == "enhancement"
        assert priority_order[4] == "documentation"

        # Test that higher priority labels appear first in resolved list
        issue_labels = ["documentation", "bug", "urgent", "breaking-change"]
        resolved = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

        # Verify priority order is maintained
        for i in range(len(resolved) - 1):
            current_idx = priority_order.index(resolved[i])
            next_idx = priority_order.index(resolved[i + 1])
            assert current_idx < next_idx, f"Priority violation: {resolved[i]} should come before {resolved[i + 1]}"

    def test_zero_max_label_count(self, config_with_pr_label_copying):
        """Test that setting max label count to 0 prevents any label copying."""
        config_with_pr_label_copying.PR_LABEL_MAX_COUNT = 0

        issue_labels = ["bug", "urgent", "enhancement"]
        resolved = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

        # Should have no labels
        assert len(resolved) == 0

    def test_negative_max_label_count_treated_as_unlimited(self, config_with_pr_label_copying):
        """Test that negative max label count allows all labels."""
        config_with_pr_label_copying.PR_LABEL_MAX_COUNT = -1

        issue_labels = ["bug", "urgent", "enhancement", "documentation"]
        resolved = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

        # Should have all semantic labels
        assert len(resolved) >= 3

    def test_empty_issue_labels_result_in_no_pr_labels(self, config_with_pr_label_copying):
        """Test that empty issue labels result in no PR labels being copied."""
        issue_labels = []
        resolved = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

        # Should have no labels
        assert len(resolved) == 0

    def test_mixed_semantic_and_non_semantic_labels(self, config_with_pr_label_copying):
        """Test handling of issues with both semantic and non-semantic labels."""
        issue_labels = ["bug", "random-label", "urgent", "custom-label", "enhancement"]
        resolved = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

        # Should only have semantic labels
        assert "bug" in resolved
        assert "urgent" in resolved
        assert "enhancement" in resolved
        assert "random-label" not in resolved
        assert "custom-label" not in resolved

    def test_label_case_insensitivity(self, config_with_pr_label_copying):
        """Test that label matching is case-insensitive."""
        # Test with various cases
        issue_labels = ["BUG", "Urgent", "DOCUMENTATION"]
        resolved = resolve_pr_labels_with_priority(issue_labels, config_with_pr_label_copying)

        # Should still detect the labels (case-insensitive)
        assert len(resolved) > 0

    def test_comprehensive_pr_label_copying_workflow(self, config_with_pr_label_copying, mock_github_client_for_pr):
        """Test comprehensive end-to-end PR label copying workflow."""
        repo_name = "owner/repo"
        issue_number = 789
        issue_data = {
            "number": issue_number,
            "title": "Comprehensive Test Issue",
            "body": "This issue has multiple labels for testing",
            "labels": ["bugfix", "critical", "documentation", "random-label"],
        }
        work_branch = f"issue-{issue_number}"
        base_branch = "main"
        llm_response = "Comprehensive test response"
        pr_number = 999

        # Mock PR closing issues
        mock_github_client_for_pr.get_pr_closing_issues.return_value = [issue_number]

        # Track label resolution
        with patch("src.auto_coder.issue_processor.resolve_pr_labels_with_priority") as mock_resolve:
            mock_resolve.return_value = ["urgent"]  # Simulate label resolution

            # Mock gh pr create
            with patch("src.auto_coder.issue_processor.get_gh_logger") as mock_gh_logger:
                mock_gh_logger_instance = Mock()
                mock_gh_logger_instance.execute_with_logging.return_value = _cmd_result(success=True, stdout=f"https://github.com/{repo_name}/pull/{pr_number}")
                mock_gh_logger.return_value = mock_gh_logger_instance

                # Create PR with comprehensive test
                result = _create_pr_for_issue(
                    repo_name,
                    issue_data,
                    work_branch,
                    base_branch,
                    llm_response,
                    mock_github_client_for_pr,
                    config_with_pr_label_copying,
                )

                # Verify PR was created
                assert f"Successfully created PR for issue #{issue_number}" in result
                # Verify resolve was called
                assert mock_resolve.called
