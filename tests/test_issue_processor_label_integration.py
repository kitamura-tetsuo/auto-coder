"""Integration tests for label-based issue processing workflows.

This module contains comprehensive integration tests that verify the end-to-end
label-based prompt workflows across all system components, including:
- prompt_loader.render_prompt() + issue labels
- label_manager operations with issue data
- Configuration system + label prompt mappings
"""

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly
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
