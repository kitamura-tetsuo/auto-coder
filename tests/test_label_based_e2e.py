"""End-to-end tests for label-based issue and PR processing."""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt
from tests.fixtures.label_prompt_fixtures import (
    TEST_ISSUE_DATA,
    TEST_LABEL_PRIORITIES,
    TEST_LABEL_PROMPT_MAPPINGS,
)


class TestLabelBasedIssueProcessingE2E:
    """End-to-end tests for label-based issue processing."""

    def test_complete_breaking_change_issue_workflow(self, tmp_path):
        """Test complete workflow for breaking-change labeled issues."""
        # Create test prompt file with breaking-change specific prompts
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "Global Header"\n' "issue:\n" '  action: "Default: $issue_number"\n' '  breaking_change: "BREAKING CHANGE - Version bump required for $issue_number"\n' '  bug: "Bug fix for $issue_number"\n' '  feature: "Feature for $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Simulate breaking-change issue
        issue_data = TEST_ISSUE_DATA["breaking_change"]
        labels = issue_data["labels"]
        issue_number = issue_data["number"]

        # Render prompt with breaking-change labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
            issue_number=str(issue_number),
            repo_name="test/repo",
        )

        # Verify breaking-change specific prompt was used
        assert "BREAKING CHANGE" in result
        assert "Version bump required" in result
        assert str(issue_number) in result
        assert "Global Header" in result

    def test_complete_bug_issue_workflow(self, tmp_path):
        """Test complete workflow for bug labeled issues."""
        # Create test prompt file with bug-specific prompts
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "Global Header"\n' "issue:\n" '  action: "Default: $issue_number"\n' '  bugfix: "BUG FIX REQUIRED - Root cause analysis for issue $issue_number"\n' '  enhancement: "Enhancement for $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Simulate bug issue
        issue_data = TEST_ISSUE_DATA["bug_fix"]
        labels = issue_data["labels"]
        issue_number = issue_data["number"]

        # Render prompt with bug labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
            issue_number=str(issue_number),
        )

        # Verify bug-specific prompt was used
        assert "BUG FIX REQUIRED" in result
        assert "Root cause analysis" in result
        assert str(issue_number) in result
        assert "Global Header" in result

    def test_complete_feature_issue_workflow(self, tmp_path):
        """Test complete workflow for feature labeled issues."""
        # Create test prompt file with feature-specific prompts
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default: $issue_number"\n' '  enhancement: "FEATURE IMPLEMENTATION - Design patterns for $issue_number"\n' '  bugfix: "Bug fix for $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Simulate feature issue
        issue_data = TEST_ISSUE_DATA["feature"]
        labels = issue_data["labels"]
        issue_number = issue_data["number"]

        # Render prompt with feature labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
            issue_number=str(issue_number),
        )

        # Verify feature-specific prompt was used
        assert "FEATURE IMPLEMENTATION" in result
        assert "Design patterns" in result
        assert str(issue_number) in result

    def test_complete_urgent_issue_workflow(self, tmp_path):
        """Test complete workflow for urgent labeled issues."""
        # Create test prompt file with urgent-specific prompts
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default: $issue_number"\n' '  urgent: "URGENT - Immediate attention required for $issue_number"\n' '  bug: "Bug fix for $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Simulate urgent issue
        issue_data = TEST_ISSUE_DATA["urgent"]
        labels = issue_data["labels"]
        issue_number = issue_data["number"]

        # Render prompt with urgent labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
            issue_number=str(issue_number),
        )

        # Verify urgent-specific prompt was used
        assert "URGENT" in result
        assert "Immediate attention" in result
        assert str(issue_number) in result

    def test_complete_documentation_issue_workflow(self, tmp_path):
        """Test complete workflow for documentation labeled issues."""
        # Create test prompt file with documentation-specific prompts
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default: $issue_number"\n' '  documentation: "DOCS - Clarity and completeness for $issue_number"\n' '  feature: "Feature for $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Simulate documentation issue
        issue_data = TEST_ISSUE_DATA["documentation"]
        labels = issue_data["labels"]
        issue_number = issue_data["number"]

        # Render prompt with documentation labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
            issue_number=str(issue_number),
        )

        # Verify documentation-specific prompt was used
        assert "DOCS" in result
        assert "Clarity and completeness" in result
        assert str(issue_number) in result

    def test_issue_with_multiple_labels_uses_highest_priority(self, tmp_path):
        """Test that issue with multiple labels uses the highest priority."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default"\n' '  bug: "Bug prompt"\n' '  feature: "Feature prompt"\n' '  urgent: "Urgent prompt"\n' '  breaking_change: "Breaking prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Simulate issue with multiple labels (bug, enhancement, urgent)
        issue_data = TEST_ISSUE_DATA["multiple_labels"]
        labels = issue_data["labels"]

        # Render prompt with multiple labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
        )

        # Urgent has higher priority than bug and enhancement
        assert "Urgent prompt" in result
        assert "Bug prompt" not in result
        assert "Feature prompt" not in result

    def test_issue_without_labels_falls_back_to_default(self, tmp_path):
        """Test that issue without labels falls back to default prompt."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default prompt"\n' '  bug: "Bug prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Simulate issue without labels
        issue_data = TEST_ISSUE_DATA["empty_labels"]
        labels = issue_data["labels"]

        # Render prompt without labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
        )

        # Should fall back to default
        assert "Default prompt" in result
        assert "Bug prompt" not in result

    def test_issue_with_custom_labels_uses_default(self, tmp_path):
        """Test that issue with custom (non-semantic) labels falls back to default."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default prompt for $repo_name"\n' '  bug: "Bug prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Simulate issue with custom labels
        issue_data = TEST_ISSUE_DATA["no_semantic_labels"]
        labels = issue_data["labels"]

        # Render prompt with custom labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
            repo_name="test/repo",
        )

        # Should fall back to default
        assert "Default prompt" in result
        assert "test/repo" in result

    def test_breaking_change_over_urgent_priority(self, tmp_path):
        """Test that breaking-change has higher priority than urgent."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default"\n' '  bug: "Bug prompt"\n' '  urgent: "Urgent prompt"\n' '  breaking_change: "Breaking prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Issue with both breaking-change and urgent labels
        labels = ["urgent", "breaking-change", "bug"]

        # Render prompt
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
        )

        # Breaking-change should win (highest priority)
        assert "Breaking prompt" in result
        assert "Urgent prompt" not in result
        assert "Bug prompt" not in result

    def test_urgent_over_bug_priority(self, tmp_path):
        """Test that urgent has higher priority than bug."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default"\n' '  bug: "Bug prompt"\n' '  urgent: "Urgent prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Issue with both urgent and bug labels
        labels = ["urgent", "bug"]

        # Render prompt
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
        )

        # Urgent should win (higher priority than bug)
        assert "Urgent prompt" in result
        assert "Bug prompt" not in result

    def test_enhancement_workflow_with_aliases(self, tmp_path):
        """Test that enhancement labels work with aliases."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default"\n' '  enhancement: "Enhancement prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Issue with feature label (alias for enhancement)
        labels = ["feature"]

        # Render prompt with feature label
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
        )

        # Should use enhancement prompt (feature maps to enhancement)
        assert "Enhancement prompt" in result

    def test_case_insensitive_label_matching(self, tmp_path):
        """Test that label matching is case-insensitive."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default"\n' '  urgent: "Urgent prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Issue with lowercase labels (matching the mappings)
        labels = ["bug", "urgent"]

        # Render prompt with labels
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
        )

        # Should match urgent (higher priority)
        assert "Urgent prompt" in result

    def test_prompt_rendering_with_data_parameters(self, tmp_path):
        """Test that prompt rendering preserves data parameters."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default: $issue_number in $repo_name"\n' '  bugfix: "Bug fix: $issue_number - $repo_name"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Issue with bug label
        labels = ["bug"]

        # Render prompt with data parameters
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
            data={
                "issue_number": "123",
                "repo_name": "owner/repo",
            },
        )

        # Verify data is included
        assert "Bug fix: 123 - owner/repo" in result

    def test_prompt_rendering_with_header(self, tmp_path):
        """Test that header is prepended to rendered prompts."""
        # Create test prompt file with header
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "=== SYSTEM PROMPT ==="\n' "issue:\n" '  action: "Default"\n' '  bugfix: "Bug fix prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Issue with bug label
        labels = ["bug"]

        # Render prompt
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
        )

        # Verify header is present
        assert result.startswith("=== SYSTEM PROMPT ===")
        assert "Bug fix prompt" in result

    def test_issue_with_enhancement_and_documentation_labels(self, tmp_path):
        """Test issue with both enhancement and documentation labels."""
        # Create test prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default"\n' '  enhancement: "Enhancement prompt"\n' '  documentation: "Documentation prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Issue with enhancement and documentation labels
        labels = ["enhancement", "documentation"]

        # Render prompt
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=TEST_LABEL_PROMPT_MAPPINGS,
            label_priorities=TEST_LABEL_PRIORITIES,
        )

        # Enhancement has higher priority than documentation
        assert "Enhancement prompt" in result
        assert "Documentation prompt" not in result


class TestLabelBasedPRProcessingE2E:
    """End-to-end tests for label-based PR processing."""

    def test_pr_with_bug_label(self, tmp_path):
        """Test PR processing with bug label."""
        # Create PR prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "PR Analysis"\n' "pr:\n" '  action: "Default PR"\n' '  bug: "BUG FIX PR - Test requirements for PR #$pr_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # PR with bug label
        pr_labels = ["bug"]

        # Simulate render_prompt for PR
        from src.auto_coder.prompt_loader import render_prompt

        result = render_prompt(
            "pr.action",
            path=str(prompt_file),
            labels=pr_labels,
            label_prompt_mappings={
                "bug": "pr.bug",
            },
            label_priorities=["bug"],
            pr_number="456",
        )

        assert "BUG FIX PR" in result
        assert "Test requirements" in result
        assert "PR #456" in result
        assert "PR Analysis" in result

    def test_pr_with_breaking_change_label(self, tmp_path):
        """Test PR processing with breaking-change label."""
        # Create PR prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "pr:\n" '  action: "Default PR"\n' '  breaking_change: "BREAKING CHANGE - Version bump for PR $pr_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # PR with breaking-change label
        pr_labels = ["breaking-change"]

        result = render_prompt(
            "pr.action",
            path=str(prompt_file),
            labels=pr_labels,
            label_prompt_mappings={
                "breaking-change": "pr.breaking_change",
            },
            label_priorities=["breaking-change"],
            pr_number="789",
        )

        assert "BREAKING CHANGE" in result
        assert "Version bump" in result

    def test_pr_label_priority_resolution(self, tmp_path):
        """Test PR label priority resolution."""
        # Create PR prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "pr:\n" '  action: "Default"\n' '  bug: "Bug PR"\n' '  feature: "Feature PR"\n' '  urgent: "Urgent PR"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # PR with multiple labels
        pr_labels = ["bug", "feature", "urgent"]

        result = render_prompt(
            "pr.action",
            path=str(prompt_file),
            labels=pr_labels,
            label_prompt_mappings={
                "bug": "pr.bug",
                "feature": "pr.feature",
                "urgent": "pr.urgent",
            },
            label_priorities=["urgent", "bug", "feature"],
            pr_number="999",
        )

        # Urgent should win
        assert "Urgent PR" in result
        assert "Bug PR" not in result
        assert "Feature PR" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
