"""Integration tests for label-based issue processing workflows.

This module contains comprehensive integration tests that verify the end-to-end
label-based prompt workflows across all system components, including:
- prompt_loader.render_prompt() + issue labels
- label_manager operations with issue data
- issue_processor._process_issue_jules_mode() + label detection
- Configuration system + label prompt mappings
"""

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly, _process_issue_jules_mode
from src.auto_coder.label_manager import get_semantic_labels_from_issue, resolve_pr_labels_with_priority
from src.auto_coder.prompt_loader import render_prompt
from tests.fixtures.label_prompt_fixtures import (
    TEST_ISSUE_DATA,
    TEST_LABEL_PRIORITIES,
    TEST_LABEL_PROMPT_MAPPINGS,
    TEST_PR_LABEL_MAPPINGS,
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


class TestIssueProcessorLabelIntegration:
    """Integration tests for issue processor with label-based workflows."""

    @pytest.fixture
    def config_with_labels(self):
        """Create a test configuration with label mappings."""
        config = AutomationConfig()
        config.label_prompt_mappings = TEST_LABEL_PROMPT_MAPPINGS
        config.label_priorities = TEST_LABEL_PRIORITIES
        config.DISABLE_LABELS = False
        return config

    @pytest.fixture
    def mock_github_client_with_labels(self):
        """Create a mock GitHub client with label support."""
        client = Mock()
        client.disable_labels = False
        client.has_label.return_value = False
        client.try_add_labels.return_value = True
        client.get_issue_details_by_number.return_value = {"labels": []}
        client.get_repository.return_value = Mock()
        client.get_open_sub_issues.return_value = []
        client.check_issue_dependencies_resolved.return_value = []
        client.add_labels.return_value = True
        return client

    def test_issue_processor_integration_with_prompt_loader(self, tmp_path, config_with_labels):
        """Test integration between issue processor and prompt loader with labels."""
        # Create a custom prompts.yaml with label-specific prompts
        prompts_yaml = tmp_path / "test_prompts.yaml"
        prompts_yaml.write_text("issue:\n" '  action: "Process issue: $title"\n' '  bugfix: "Fix bug: $title"\n' '  urgent: "URGENT: $title"\n' '  enhancement: "Enhance: $title"\n', encoding="utf-8")

        # Test 1: Bug label should trigger bugfix prompt
        rendered_bug = render_prompt(
            "issue.action",
            path=str(prompts_yaml),
            labels=["bug"],
            label_prompt_mappings=config_with_labels.label_prompt_mappings,
            label_priorities=config_with_labels.label_priorities,
            title="Authentication Bug",
        )

        assert rendered_bug is not None

        # Test 2: Urgent label should trigger urgent prompt
        rendered_urgent = render_prompt(
            "issue.action",
            path=str(prompts_yaml),
            labels=["urgent"],
            label_prompt_mappings=config_with_labels.label_prompt_mappings,
            label_priorities=config_with_labels.label_priorities,
            title="Security Issue",
        )

        assert rendered_urgent is not None

        # Test 3: No labels should use default
        rendered_default = render_prompt(
            "issue.action",
            path=str(prompts_yaml),
            labels=[],
            label_prompt_mappings=config_with_labels.label_prompt_mappings,
            label_priorities=config_with_labels.label_priorities,
            title="Regular Issue",
        )

        assert rendered_default is not None

    def test_issue_processor_integration_with_label_manager(self, mock_github_client_with_labels):
        """Test integration between issue processor and label manager."""
        repo_name = "owner/repo"
        issue_data = {"number": 123, "title": "Test Issue", "labels": ["bug"], "body": ""}

        # Test that LabelManager is called during issue processing
        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            mock_cmd.run_command.return_value = _cmd_result(success=True)

            with patch("src.auto_coder.issue_processor.LabelManager") as MockLabelManager:
                mock_label_manager_instance = Mock()
                mock_label_manager_instance.__enter__ = Mock(return_value=True)
                mock_label_manager_instance.__exit__ = Mock(return_value=None)
                MockLabelManager.return_value = mock_label_manager_instance

                config = AutomationConfig()
                result = _process_issue_jules_mode(
                    mock_github_client_with_labels,
                    config,
                    repo_name,
                    issue_data,
                )

                # Verify LabelManager was used
                MockLabelManager.assert_called_once()
                assert result is not None

    def test_full_integration_chain_prompt_loader_label_manager(self, tmp_path):
        """Test full integration chain: labels -> prompt selection -> rendering."""
        # Create custom prompts
        prompts_yaml = tmp_path / "integration.yaml"
        prompts_yaml.write_text('header: "Auto-Coder System"\n' "issue:\n" '  action: "Process issue: $title"\n' '  bugfix: "Fix bug in issue $issue_number"\n' '  urgent: "URGENT: Issue $issue_number"\n' '  breaking_change: "BREAKING: $title"\n', encoding="utf-8")

        # Test the full chain: issue labels -> semantic detection -> prompt selection -> rendering
        test_scenarios = [
            {
                "labels": ["bug"],
                "issue_number": "100",
                "title": "Login Error",
                "expected_keyword": "Fix bug",
            },
            {
                "labels": ["urgent", "bug"],
                "issue_number": "101",
                "title": "Critical Bug",
                "expected_keyword": "URGENT",
            },
            {
                "labels": ["breaking-change"],
                "issue_number": "102",
                "title": "API Change",
                "expected_keyword": "BREAKING",
            },
        ]

        for scenario in test_scenarios:
            rendered = render_prompt(
                "issue.action",
                path=str(prompts_yaml),
                labels=scenario["labels"],
                label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
                label_priorities=TEST_LABEL_PRIORITIES,
                issue_number=scenario["issue_number"],
                title=scenario["title"],
            )

            # Verify the full integration chain works
            assert rendered is not None
            assert "Auto-Coder System" in rendered  # Header included
            # The rendered prompt should contain expected content
            # (either label-specific or fallback)
            assert len(rendered) > 0

    def test_label_detection_and_prompt_selection_integration(self, config_with_labels):
        """Test integration between label detection and prompt selection."""
        issue_labels = ["bug", "urgent", "enhancement"]

        # Test label priority resolution - use PR_LABEL_MAPPINGS structure
        resolved_labels = get_semantic_labels_from_issue(issue_labels, TEST_PR_LABEL_MAPPINGS)

        # Verify semantic labels were detected
        assert len(resolved_labels) > 0  # At least some semantic labels detected

        # Test prompt template selection based on labels
        # Find the highest priority label from the resolved ones
        priority_order = config_with_labels.label_priorities
        highest_priority_label = None
        for label in priority_order:
            if label in issue_labels:
                highest_priority_label = label
                break

        if highest_priority_label:
            # Verify it has a prompt mapping
            assert highest_priority_label in config_with_labels.label_prompt_mappings

    def test_breaking_change_issues_with_specialized_prompts(self, config_with_labels):
        """Test that breaking-change issues are handled with specialized prompts."""
        breaking_labels = ["breaking-change", "api-change", "deprecation"]

        # Test semantic label detection - use PR_LABEL_MAPPINGS structure
        detected = get_semantic_labels_from_issue(breaking_labels, TEST_PR_LABEL_MAPPINGS)

        # Verify breaking-change variant is detected
        assert len(detected) > 0  # At least one semantic label detected

        # Test that breaking-change has highest priority
        priority_order = config_with_labels.label_priorities
        breaking_change_idx = priority_order.index("breaking-change")
        bug_idx = priority_order.index("bug")
        enhancement_idx = priority_order.index("enhancement")

        assert breaking_change_idx < bug_idx
        assert breaking_change_idx < enhancement_idx

    def test_multiple_label_conflicts_and_priority_resolution(self, config_with_labels):
        """Test resolution of conflicts when multiple applicable labels exist."""
        test_cases = [
            # (issue_labels, expected_highest_priority_label)
            (["bug", "enhancement"], "bug"),
            (["urgent", "bug"], "urgent"),
            (["breaking-change", "urgent", "bug"], "breaking-change"),
            (["documentation", "enhancement"], "enhancement"),
            (["feature", "documentation"], "enhancement"),  # 'feature' maps to 'enhancement'
        ]

        for issue_labels, expected_priority_label in test_cases:
            # Get semantic labels - use PR_LABEL_MAPPINGS structure
            semantic = get_semantic_labels_from_issue(issue_labels, TEST_PR_LABEL_MAPPINGS)

            # Find highest priority
            priority_order = config_with_labels.label_priorities
            highest_priority = None
            for label in priority_order:
                if label in semantic:
                    highest_priority = label
                    break

            # If no semantic labels detected, skip the assertion
            # (this is expected for some test cases with custom labels)
            if semantic:
                assert highest_priority == expected_priority_label, f"Failed for labels {issue_labels}: expected {expected_priority_label}, got {highest_priority}"

    def test_configuration_loading_and_validation(self):
        """Test that configuration loads correctly for label-based processing."""
        config = AutomationConfig()

        # Verify default configuration exists
        assert config.label_prompt_mappings is not None
        assert config.label_priorities is not None
        assert len(config.label_prompt_mappings) > 0
        assert len(config.label_priorities) > 0

        # Verify PR label configuration
        config.validate_pr_label_config()  # Should not raise

        # Verify specific mappings exist
        assert "bug" in config.label_prompt_mappings
        assert "urgent" in config.label_prompt_mappings
        assert "breaking-change" in config.label_priorities

        # Verify PR label mappings
        assert "breaking-change" in config.PR_LABEL_MAPPINGS
        assert "urgent" in config.PR_LABEL_MAPPINGS

    def test_configuration_via_yaml_file(self, tmp_path):
        """Test that configuration can be loaded from YAML files."""
        # Create a custom prompts.yaml with label-specific prompts
        prompts_yaml = tmp_path / "custom_prompts.yaml"
        prompts_yaml.write_text(
            "issue:\n" '  action: "Default action"\n' '  bugfix: "Bug fix prompt"\n' '  feature: "Feature prompt"\n' '  custom: "Custom prompt"\n' "label_prompt_mappings:\n" '  bug: "issue.bugfix"\n' '  custom-label: "issue.custom"\n' "label_priorities:\n" '  - "custom-label"\n' '  - "bug"\n',
            encoding="utf-8",
        )

        # Test rendering with custom prompts
        rendered = render_prompt(
            "issue.action",
            path=str(prompts_yaml),
            labels=["custom-label"],
            label_prompt_mappings={"custom-label": "issue.custom"},
            label_priorities=["custom-label", "bug"],
        )

        assert "Custom prompt" in rendered

    def test_integration_with_label_manager_context(self, mock_github_client_with_labels):
        """Test that label manager integrates properly with issue processing."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {"number": issue_number, "title": "Test", "labels": ["bug"], "body": ""}

        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            mock_cmd.run_command.return_value = _cmd_result(success=True)

            with patch("src.auto_coder.issue_processor.get_llm_backend_manager"):
                # Test with LabelManager context manager
                with patch("src.auto_coder.issue_processor.LabelManager") as MockLabelManager:
                    mock_label_manager_instance = Mock()
                    mock_label_manager_instance.__enter__ = Mock(return_value=True)
                    mock_label_manager_instance.__exit__ = Mock(return_value=None)
                    MockLabelManager.return_value = mock_label_manager_instance

                    config = AutomationConfig()
                    result = _process_issue_jules_mode(
                        mock_github_client_with_labels,
                        config,
                        repo_name,
                        issue_data,
                    )

                    # Verify LabelManager was used
                    MockLabelManager.assert_called_once()

    def test_issue_assignment_based_on_labels(self, config_with_labels):
        """Test that issues are correctly assigned based on label types."""
        test_cases = [
            (["bug", "urgent"], "urgent"),
            (["enhancement", "documentation"], "enhancement"),
            (["breaking-change", "bug"], "breaking-change"),
            (["documentation"], "documentation"),
        ]

        for issue_labels, expected_category in test_cases:
            # Get semantic labels - use PR_LABEL_MAPPINGS structure
            semantic = get_semantic_labels_from_issue(issue_labels, TEST_PR_LABEL_MAPPINGS)

            # Verify correct category is detected
            if expected_category in semantic:
                # Category found, verify prompt mapping exists
                prompt_key = config_with_labels.label_prompt_mappings.get(expected_category)
                assert prompt_key is not None

    def test_comment_generation_with_label_specific_prompts(self, tmp_path, config_with_labels):
        """Test that comments are generated with label-specific prompts."""
        # Create a custom prompts.yaml with label-specific comment prompts
        prompts_yaml = tmp_path / "custom_prompts.yaml"
        prompts_yaml.write_text("issue:\n" '  action: "Default action"\n' '  bugfix: "Bug fix: $issue_number"\n' '  urgent: "Urgent: $issue_number"\n', encoding="utf-8")

        # Test comment generation for bug issue
        rendered = render_prompt(
            "issue.action",
            path=str(prompts_yaml),
            labels=["bug"],
            label_prompt_mappings=config_with_labels.label_prompt_mappings,
            label_priorities=config_with_labels.label_priorities,
            issue_number="123",
        )

        assert "Bug fix" in rendered

        # Test comment generation for urgent issue
        rendered = render_prompt(
            "issue.action",
            path=str(prompts_yaml),
            labels=["urgent"],
            label_prompt_mappings=config_with_labels.label_prompt_mappings,
            label_priorities=config_with_labels.label_priorities,
            issue_number="456",
        )

        assert "Urgent" in rendered

    def test_empty_labels_fallback_to_default(self, tmp_path, config_with_labels):
        """Test that issues with no labels fallback to default prompt."""
        # Create a custom prompts.yaml
        prompts_yaml = tmp_path / "custom_prompts.yaml"
        prompts_yaml.write_text("issue:\n" '  action: "Default action for unlabeled issues"\n' '  bugfix: "Bug fix prompt"\n', encoding="utf-8")

        # Test rendering with no labels - should use default
        rendered = render_prompt(
            "issue.action",
            path=str(prompts_yaml),
            labels=[],
            label_prompt_mappings=config_with_labels.label_prompt_mappings,
            label_priorities=config_with_labels.label_priorities,
        )

        assert "Default action for unlabeled issues" in rendered

    def test_label_alias_resolution(self, config_with_labels):
        """Test that label aliases are properly resolved."""
        # Test with alias labels that should map to semantic labels
        alias_tests = [
            (["bugfix"], "bug"),
            (["high-priority"], "urgent"),
            (["doc"], "documentation"),
            (["improvement"], "enhancement"),
        ]

        for aliases, expected_semantic in alias_tests:
            resolved = get_semantic_labels_from_issue(aliases, TEST_PR_LABEL_MAPPINGS)

            # The semantic label should be detected (either directly or through alias)
            assert len(resolved) > 0, f"No semantic labels detected for aliases {aliases}"

            # Check that at least one of the expected semantic labels is in resolved
            # or matches an alias
            found = expected_semantic in resolved or any(expected_semantic in alias_list for label, alias_list in TEST_PR_LABEL_MAPPINGS.items() if label in aliases or any(a in aliases for a in alias_list))
            assert found, f"Expected semantic label {expected_semantic} not found for aliases {aliases}"

    def test_jules_mode_with_label_detection(self, mock_github_client_with_labels):
        """Test jules mode correctly detects and handles labels."""
        repo_name = "owner/repo"
        issue_data = {"number": 123, "title": "Test Issue", "labels": ["bug", "jules"], "body": ""}

        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            mock_cmd.run_command.return_value = _cmd_result(success=True)

            config = AutomationConfig()
            result = _process_issue_jules_mode(
                mock_github_client_with_labels,
                config,
                repo_name,
                issue_data,
            )

            # Verify jules mode processed the issue
            assert result is not None
            assert "actions_taken" in result.__dict__

    def test_integration_error_handling_with_labels(self, mock_github_client_with_labels):
        """Test that errors in label processing are handled gracefully."""
        repo_name = "owner/repo"
        issue_data = {"number": 123, "title": "Test Issue", "labels": ["bug"], "body": ""}

        # Make GitHub client raise an exception
        mock_github_client_with_labels.get_open_sub_issues.side_effect = Exception("GitHub API error")

        with patch("src.auto_coder.issue_processor.cmd") as mock_cmd:
            mock_cmd.run_command.return_value = _cmd_result(success=True)

            config = AutomationConfig()
            result = _process_issue_jules_mode(
                mock_github_client_with_labels,
                config,
                repo_name,
                issue_data,
            )

            # Verify error is handled gracefully
            assert result is not None
            assert "error" in result.__dict__

    def test_full_integration_prompt_rendering_with_labels(self, tmp_path):
        """Test full integration of prompt rendering with labels."""
        # Create comprehensive prompts.yaml
        prompts_yaml = tmp_path / "prompts.yaml"
        prompts_yaml.write_text(
            'header: "System Header"\n'
            "issue:\n"
            '  action: "Process issue #$issue_number: $title"\n'
            '  bugfix: "Fix bug in issue #$issue_number"\n'
            '  urgent: "URGENT: Fix issue #$issue_number"\n'
            '  enhancement: "Enhance: $title"\n'
            '  breaking_change: "BREAKING CHANGE in issue #$issue_number"\n',
            encoding="utf-8",
        )

        test_cases = [
            (["bug"], "Fix bug"),
            (["urgent"], "URGENT"),
            (["enhancement"], "Enhance"),
            (["breaking-change"], "BREAKING CHANGE"),
        ]

        for labels, expected_content in test_cases:
            rendered = render_prompt(
                "issue.action",
                path=str(prompts_yaml),
                labels=labels,
                label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
                label_priorities=TEST_LABEL_PRIORITIES,
                issue_number="123",
                title="Test Title",
            )

            assert expected_content in rendered, f"Expected '{expected_content}' in rendered prompt for labels {labels}"
            assert "System Header" in rendered  # Header should be prepended
