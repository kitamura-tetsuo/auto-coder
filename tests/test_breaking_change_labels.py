"""Tests for breaking-change label detection and handling.

This module provides comprehensive test coverage for the breaking-change
label detection functionality across all alias variations and edge cases.
"""

import pytest

from src.auto_coder.prompt_loader import _is_breaking_change_issue, _resolve_label_priority


class TestBreakingChangeAliases:
    """Test detection of all breaking-change label aliases."""

    def test_breaking_change_alias(self):
        """Test detection of 'breaking-change' label."""
        labels = ["bug", "breaking-change"]
        assert _is_breaking_change_issue(labels) is True

    def test_breaking_alias(self):
        """Test detection of 'breaking' label."""
        labels = ["feature", "breaking"]
        assert _is_breaking_change_issue(labels) is True

    def test_api_change_alias(self):
        """Test detection of 'api-change' label."""
        labels = ["enhancement", "api-change"]
        assert _is_breaking_change_issue(labels) is True

    def test_deprecation_alias(self):
        """Test detection of 'deprecation' label."""
        labels = ["documentation", "deprecation"]
        assert _is_breaking_change_issue(labels) is True

    def test_version_major_alias(self):
        """Test detection of 'version-major' label."""
        labels = ["feature", "version-major"]
        assert _is_breaking_change_issue(labels) is True

    @pytest.mark.parametrize(
        "label",
        [
            "breaking-change",
            "breaking",
            "api-change",
            "deprecation",
            "version-major",
        ],
    )
    def test_each_breaking_change_alias(self, label):
        """Parametrized test for each breaking-change alias."""
        labels = ["bug", label]
        assert _is_breaking_change_issue(labels) is True


class TestCaseInsensitiveDetection:
    """Test case-insensitive detection of breaking-change labels."""

    def test_uppercase_breaking_change(self):
        """Test uppercase BREAKING-CHANGE."""
        labels = ["Bug", "BREAKING-CHANGE"]
        assert _is_breaking_change_issue(labels) is True

    def test_uppercase_breaking(self):
        """Test uppercase BREAKING."""
        labels = ["Feature", "BREAKING"]
        assert _is_breaking_change_issue(labels) is True

    def test_uppercase_api_change(self):
        """Test uppercase API-CHANGE."""
        labels = ["Enhancement", "API-CHANGE"]
        assert _is_breaking_change_issue(labels) is True

    def test_uppercase_deprecation(self):
        """Test uppercase DEPRECATION."""
        labels = ["Documentation", "DEPRECATION"]
        assert _is_breaking_change_issue(labels) is True

    def test_uppercase_version_major(self):
        """Test uppercase VERSION-MAJOR."""
        labels = ["Feature", "VERSION-MAJOR"]
        assert _is_breaking_change_issue(labels) is True

    def test_mixed_case_breaking_change(self):
        """Test mixed case Breaking-Change."""
        labels = ["Bug", "Breaking-Change"]
        assert _is_breaking_change_issue(labels) is True

    def test_mixed_case_breaking(self):
        """Test mixed case Breaking."""
        labels = ["Feature", "Breaking"]
        assert _is_breaking_change_issue(labels) is True

    def test_lowercase_all(self):
        """Test lowercase versions."""
        labels = ["bug", "breaking-change", "feature"]
        assert _is_breaking_change_issue(labels) is True

    @pytest.mark.parametrize(
        "labels",
        [
            ["Bug", "breaking-change"],
            ["Feature", "BREAKING"],
            ["Enhancement", "Api-Change"],
            ["Docs", "DEPRECATION"],
            ["Feature", "version-major"],
            ["bug", "BREAKING-CHANGE"],
        ],
    )
    def test_parametrized_case_insensitive(self, labels):
        """Parametrized test for case-insensitive detection."""
        assert _is_breaking_change_issue(labels) is True


class TestMultipleBreakingChangeLabels:
    """Test scenarios with multiple breaking-change labels."""

    def test_two_breaking_change_aliases(self):
        """Test with two different breaking-change aliases."""
        labels = ["breaking-change", "breaking"]
        assert _is_breaking_change_issue(labels) is True

    def test_three_breaking_change_aliases(self):
        """Test with three different breaking-change aliases."""
        labels = ["breaking", "api-change", "deprecation"]
        assert _is_breaking_change_issue(labels) is True

    def test_all_five_breaking_change_aliases(self):
        """Test with all five breaking-change aliases."""
        labels = [
            "breaking-change",
            "breaking",
            "api-change",
            "deprecation",
            "version-major",
        ]
        assert _is_breaking_change_issue(labels) is True

    def test_multiple_copies_same_alias(self):
        """Test with multiple copies of same breaking-change alias."""
        labels = ["breaking-change", "breaking-change", "feature"]
        assert _is_breaking_change_issue(labels) is True

    def test_mixed_multiple_breaking_with_other_labels(self):
        """Test multiple breaking-change labels with other labels."""
        labels = [
            "breaking-change",
            "urgent",
            "api-change",
            "bug",
            "feature",
        ]
        assert _is_breaking_change_issue(labels) is True


class TestPriorityOrderingWithBreakingChange:
    """Test priority ordering when breaking-change labels are present."""

    def test_breaking_change_has_highest_priority(self):
        """Test that breaking-change has higher priority than urgent."""
        labels = ["urgent", "breaking-change"]
        mappings = {
            "breaking-change": "issue.breaking_change",
            "urgent": "issue.urgent",
        }
        priorities = [
            "breaking-change",
            "breaking",
            "api-change",
            "deprecation",
            "version-major",
            "urgent",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking-change"

    def test_breaking_over_bug(self):
        """Test that breaking-change has higher priority than bug."""
        labels = ["bug", "breaking-change"]
        mappings = {
            "breaking-change": "issue.breaking_change",
            "bug": "issue.bugfix",
        }
        priorities = [
            "breaking-change",
            "breaking",
            "api-change",
            "deprecation",
            "version-major",
            "bug",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking-change"

    def test_breaking_over_feature(self):
        """Test that breaking-change has higher priority than feature."""
        labels = ["feature", "breaking"]
        mappings = {
            "breaking": "issue.breaking_change",
            "feature": "issue.feature",
        }
        priorities = [
            "breaking",
            "breaking-change",
            "api-change",
            "deprecation",
            "version-major",
            "feature",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking"

    def test_different_breaking_aliases_priority(self):
        """Test priority among different breaking-change aliases."""
        labels = ["breaking", "api-change"]
        mappings = {
            "breaking": "issue.breaking_1",
            "api-change": "issue.breaking_2",
        }
        priorities = [
            "breaking",
            "api-change",
            "deprecation",
            "version-major",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        # "breaking" has higher priority in the list
        assert result == "breaking"

    def test_deprecation_vs_version_major(self):
        """Test priority between deprecation and version-major."""
        labels = ["deprecation", "version-major"]
        mappings = {
            "deprecation": "issue.deprecation",
            "version-major": "issue.version_major",
        }
        priorities = [
            "breaking-change",
            "breaking",
            "api-change",
            "deprecation",
            "version-major",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "deprecation"

    def test_breaking_change_not_in_priorities(self):
        """Test breaking-change label when not in priorities list."""
        labels = ["breaking-change", "bug"]
        mappings = {
            "breaking-change": "issue.breaking_change",
            "bug": "issue.bugfix",
        }
        priorities = ["bug", "feature"]  # No breaking-change

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should fall back to first applicable (bug)
        assert result == "bug"


class TestBreakingChangePlusUrgentCombinations:
    """Test combinations of breaking-change with urgent labels."""

    def test_breaking_change_with_urgent(self):
        """Test breaking-change combined with urgent."""
        labels = ["breaking-change", "urgent"]
        assert _is_breaking_change_issue(labels) is True

    def test_breaking_with_urgent(self):
        """Test breaking combined with urgent."""
        labels = ["breaking", "urgent"]
        assert _is_breaking_change_issue(labels) is True

    def test_all_three_together(self):
        """Test breaking-change, breaking, and urgent together."""
        labels = ["breaking-change", "breaking", "urgent"]
        assert _is_breaking_change_issue(labels) is True

    def test_urgent_priority_with_breaking_mappings(self):
        """Test urgent priority when breaking-change is in labels."""
        labels = ["urgent", "breaking-change", "bug"]
        mappings = {
            "urgent": "issue.urgent",
            "breaking-change": "issue.breaking_change",
            "bug": "issue.bugfix",
        }
        priorities = [
            "breaking-change",
            "breaking",
            "urgent",
            "bug",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking-change"

    def test_priority_order_respects_breaking_first(self):
        """Test that priority order correctly places breaking-change first."""
        labels = ["urgent", "bug", "feature", "enhancement", "breaking-change"]
        mappings = {
            "urgent": "issue.urgent",
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
            "breaking-change": "issue.breaking_change",
        }
        priorities = [
            "breaking-change",
            "urgent",
            "bug",
            "feature",
            "enhancement",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking-change"


class TestIntegrationWithLabelPriority:
    """Test integration of breaking-change detection with label priority resolution."""

    def test_real_world_scenario_1(self):
        """Test real-world scenario with bug and breaking-change."""
        labels = ["bug", "urgent", "breaking-change"]
        mappings = {
            "breaking-change": "issue.breaking_change",
            "urgent": "issue.urgent",
            "bug": "issue.bugfix",
        }
        priorities = [
            "breaking-change",
            "urgent",
            "bug",
            "feature",
            "enhancement",
            "documentation",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking-change"

    def test_real_world_scenario_2(self):
        """Test real-world scenario with feature and api-change."""
        labels = ["feature", "api-change", "enhancement"]
        mappings = {
            "api-change": "issue.breaking_change",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
        }
        priorities = [
            "breaking-change",
            "api-change",
            "feature",
            "enhancement",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "api-change"

    def test_real_world_scenario_3(self):
        """Test real-world scenario with deprecation and documentation."""
        labels = ["deprecation", "documentation"]
        mappings = {
            "deprecation": "issue.breaking_change",
            "documentation": "issue.documentation",
        }
        priorities = [
            "breaking-change",
            "deprecation",
            "documentation",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "deprecation"

    def test_mixed_priority_scenario(self):
        """Test mixed scenario with all label types."""
        labels = [
            "bug",
            "feature",
            "urgent",
            "breaking-change",
            "documentation",
            "enhancement",
        ]
        mappings = {
            "breaking-change": "issue.breaking_change",
            "urgent": "issue.urgent",
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
            "documentation": "issue.documentation",
        }
        priorities = [
            "breaking-change",
            "urgent",
            "bug",
            "feature",
            "enhancement",
            "documentation",
        ]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking-change"


class TestEdgeCases:
    """Test edge cases for breaking-change detection."""

    def test_empty_list(self):
        """Test with empty label list."""
        assert _is_breaking_change_issue([]) is False

    def test_none_input(self):
        """Test with None input (should raise or return False)."""
        # Function expects a list, so passing None will likely raise TypeError
        with pytest.raises(TypeError):
            _is_breaking_change_issue(None)

    def test_only_non_breaking_labels(self):
        """Test with only non-breaking labels."""
        labels = ["bug", "feature", "enhancement", "documentation", "question"]
        assert _is_breaking_change_issue(labels) is False

    def test_labels_with_substring_matches(self):
        """Test labels that contain breaking-change as substring but aren't exact matches."""
        labels = ["breaking-change-label", "not-breaking", "api-changed"]
        # These are NOT exact matches, so should return False
        assert _is_breaking_change_issue(labels) is False

    def test_labels_with_breaking_as_substring(self):
        """Test that labels containing 'breaking' aren't matched unless exact."""
        labels = ["breaking-change-label"]
        assert _is_breaking_change_issue(labels) is False

    def test_breaking_change_with_special_chars(self):
        """Test breaking-change label with special characters."""
        labels = ["breaking-change!", "breaking@work"]
        assert _is_breaking_change_issue(labels) is False

    def test_unicode_breaking_change(self):
        """Test Unicode variations of breaking-change."""
        labels = ["brëaking-change", "bréaking"]
        # Case-insensitive but still exact matching
        assert _is_breaking_change_issue(labels) is False


class TestBreakingChangeWithRenderPrompt:
    """Test breaking-change detection in context of render_prompt."""

    def test_isolation_from_priority_resolution(self):
        """Test that breaking-change detection works independently of priority resolution."""
        # These labels should trigger breaking-change detection
        labels = ["breaking-change", "feature"]

        # Check breaking-change detection
        assert _is_breaking_change_issue(labels) is True

        # But priority resolution depends on mappings and priorities
        mappings = {"feature": "issue.feature"}  # No breaking-change mapping
        priorities = ["feature"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should get "feature" since breaking-change isn't mapped
        assert result == "feature"

    def test_complete_workflow_with_breaking(self):
        """Test complete workflow with breaking-change label."""
        labels = ["breaking-change", "urgent", "bug"]
        mappings = {
            "breaking-change": "issue.breaking_change",
            "urgent": "issue.urgent",
            "bug": "issue.bugfix",
        }
        priorities = [
            "breaking-change",
            "urgent",
            "bug",
            "feature",
        ]

        # Detection should work
        assert _is_breaking_change_issue(labels) is True

        # Priority resolution should work
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking-change"


@pytest.mark.parametrize(
    "labels, expected",
    [
        # Breaking-change aliases
        (["breaking-change"], True),
        (["breaking"], True),
        (["api-change"], True),
        (["deprecation"], True),
        (["version-major"], True),
        # Combinations
        (["breaking", "urgent"], True),
        (["breaking-change", "bug"], True),
        # Non-breaking
        (["bug"], False),
        (["feature"], False),
        (["enhancement"], False),
        (["documentation"], False),
        # Empty
        ([], False),
    ],
)
def test_parametrized_breaking_change_detection(labels, expected):
    """Parametrized test for breaking-change detection."""
    assert _is_breaking_change_issue(labels) == expected


@pytest.mark.parametrize(
    "labels, mappings, priorities, expected",
    [
        # Test breaking-change priority over other labels
        (
            ["breaking-change", "bug"],
            {"breaking-change": "prompt.1", "bug": "prompt.2"},
            ["breaking-change", "bug"],
            "breaking-change",
        ),
        # Test breaking priority
        (
            ["breaking", "feature"],
            {"breaking": "prompt.1", "feature": "prompt.2"},
            ["breaking", "feature"],
            "breaking",
        ),
        # Test api-change priority
        (
            ["api-change", "enhancement"],
            {"api-change": "prompt.1", "enhancement": "prompt.2"},
            ["api-change", "enhancement"],
            "api-change",
        ),
        # Test urgent priority with breaking-change
        (
            ["urgent", "breaking-change"],
            {"urgent": "prompt.1", "breaking-change": "prompt.2"},
            ["breaking-change", "urgent"],
            "breaking-change",
        ),
    ],
)
def test_parametrized_breaking_change_priority(labels, mappings, priorities, expected):
    """Parametrized test for breaking-change priority resolution."""
    result = _resolve_label_priority(labels, mappings, priorities)
    assert result == expected
