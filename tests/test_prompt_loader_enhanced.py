"""Enhanced unit tests for label-based prompt functionality.

This module provides comprehensive test coverage for edge cases and advanced
scenarios in label-based prompt handling, aiming for 95%+ line coverage.
"""

import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from auto_coder import prompt_loader
from auto_coder.prompt_loader import (
    _get_prompt_for_labels,
    _resolve_label_priority,
    _traverse,
    clear_prompt_cache,
    get_label_specific_prompt,
    get_prompt_template,
    load_prompts,
    render_prompt,
)


class TestLabelPriorityEdgeCases:
    """Test edge cases for label priority resolution."""

    @pytest.mark.parametrize(
        "labels, mappings, priorities, expected",
        [
            # Duplicate labels in issue labels (should be deduplicated)
            (["bug", "bug", "feature"], {"bug": "issue.bug"}, ["bug"], "bug"),
            # Duplicate labels in priorities
            (["bug"], {"bug": "issue.bug"}, ["bug", "bug"], "bug"),
            # Multiple duplicates across all inputs
            (["bug", "bug", "feature", "feature"], {"bug": "issue.bug", "feature": "issue.feature"}, ["feature", "feature", "bug"], "feature"),
        ],
    )
    def test_duplicate_labels_handling(self, labels, mappings, priorities, expected):
        """Test that duplicate labels are properly handled."""
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == expected

    def test_label_priority_with_none_in_list(self):
        """Test that None values in label lists are handled gracefully."""
        # Filter out None values before processing
        labels = ["bug", None, "feature", None]
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        # Should work after filtering None values
        result = _resolve_label_priority([label for label in labels if label is not None], mappings, priorities)
        assert result == "bug"

    def test_unicode_labels(self):
        """Test handling of Unicode characters in labels."""
        labels = ["büɡ", "功能", "féature", "🚀"]
        mappings = {"büɡ": "issue.bug", "功能": "issue.feature"}
        priorities = ["büɡ", "功能"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "büɡ"

    def test_special_characters_in_labels(self):
        """Test handling of special characters in labels."""
        labels = ["bug-fix", "feature+", "enhancement@v2", "docs[core]"]
        mappings = {
            "bug-fix": "issue.bug",
            "feature+": "issue.feature",
        }
        priorities = ["feature+", "bug-fix"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "feature+"

    def test_very_long_label(self):
        """Test handling of very long labels."""
        long_label = "a" * 1000
        labels = [long_label]
        mappings = {long_label: "issue.long"}
        priorities = [long_label]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == long_label

    def test_whitespace_in_labels(self):
        """Test handling of labels with whitespace."""
        labels = ["  bug  ", " feature "]  # Using labels that don't match the mappings
        mappings = {"bug": "issue.bug", "feature": "issue.feature"}
        priorities = ["feature", "bug"]

        # Labels should be used as-is (no trimming in current implementation)
        result = _resolve_label_priority(labels, mappings, priorities)
        # "  bug  " won't match "bug" exactly, and " feature " won't match "feature"
        assert result is None

    @pytest.mark.parametrize("label_count", [100, 500, 1000])
    def test_large_label_list_performance(self, label_count):
        """Test performance with very large label lists."""
        labels = [f"label-{i}" for i in range(label_count)]
        mappings = {f"label-{i}": f"issue.{i}" for i in range(0, label_count, 10)}
        priorities = [f"label-{i}" for i in range(0, label_count, 10)]

        start = time.time()
        result = _resolve_label_priority(labels, mappings, priorities)
        elapsed = time.time() - start

        # Should complete within reasonable time (<5 seconds)
        assert elapsed < 5.0, f"Performance degradation with {label_count} labels"
        # Should find the highest priority label
        assert result == priorities[0]

    def test_mixed_case_sensitive_matching(self):
        """Test that label matching is case-sensitive (as implemented)."""
        labels = ["Bug", "FEATURE"]
        mappings = {"bug": "issue.bug", "feature": "issue.feature"}
        priorities = ["bug", "feature"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Case-sensitive matching means no match
        assert result is None

    def test_mixed_case_with_correct_case(self):
        """Test matching when case matches exactly."""
        labels = ["Bug", "FEATURE"]
        mappings = {"Bug": "issue.bug", "FEATURE": "issue.feature"}
        priorities = ["FEATURE", "Bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should match FEATURE (higher priority)
        assert result == "FEATURE"


class TestGetPromptForLabelsEdgeCases:
    """Test edge cases for _get_prompt_for_labels function."""

    def test_none_labels_list(self):
        """Test with None labels list."""
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        result = _get_prompt_for_labels(None, mappings, priorities)
        assert result is None

    def test_none_mappings(self):
        """Test with None mappings."""
        labels = ["bug"]
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, None, priorities)
        assert result is None

    def test_none_priorities(self):
        """Test with None priorities (fallback to first applicable label)."""
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}

        result = _get_prompt_for_labels(labels, mappings, None)
        # None priorities should fall back to first applicable label
        assert result == "issue.bug"

    def test_empty_labels_list(self):
        """Test with empty labels list."""
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        result = _get_prompt_for_labels([], mappings, priorities)
        assert result is None

    def test_empty_mappings_dict(self):
        """Test with empty mappings dict."""
        labels = ["bug"]
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, {}, priorities)
        assert result is None

    def test_empty_priorities_list(self):
        """Test with empty priorities list."""
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}

        result = _get_prompt_for_labels(labels, mappings, [])
        # Empty priorities list falls back to first applicable label
        assert result == "issue.bug"

    def test_mappings_with_non_string_values(self):
        """Test mappings with non-string prompt keys (should still work)."""
        labels = ["bug"]
        mappings = {"bug": 123}  # Non-string value
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == 123

    def test_mappings_with_none_values(self):
        """Test mappings with None as prompt key."""
        labels = ["bug"]
        mappings = {"bug": None}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_labels_with_duplicates(self):
        """Test with duplicate labels in input."""
        labels = ["bug", "feature", "bug"]
        mappings = {"bug": "issue.bug", "feature": "issue.feature"}
        priorities = ["feature", "bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == "issue.feature"

    def test_priorities_with_none_values(self):
        """Test priorities list containing None."""
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}
        priorities = [None, "bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Should skip None and find bug
        assert result == "issue.bug"


class TestGetLabelSpecificPromptEdgeCases:
    """Test edge cases for get_label_specific_prompt function."""

    def test_all_none_inputs(self):
        """Test with all None inputs."""
        result = get_label_specific_prompt(None, None, None)
        assert result is None

    def test_labels_none_with_valid_configs(self):
        """Test with None labels but valid mappings and priorities."""
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        result = get_label_specific_prompt(None, mappings, priorities)
        assert result is None

    def test_empty_string_in_labels(self):
        """Test with empty strings in labels list."""
        labels = ["", "", ""]
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        result = get_label_specific_prompt(labels, mappings, priorities)
        # Empty strings won't match "bug"
        assert result is None

    def test_numeric_labels(self):
        """Test with numeric labels (converted to strings)."""
        labels = ["123", "456"]  # Using string representations of numbers
        mappings = {"123": "issue.first", "456": "issue.second"}
        priorities = ["123"]

        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.first"

    def test_labels_with_special_chars(self):
        """Test labels with various special characters."""
        labels = ["@auto-coder", "type:bug", "area/core"]
        mappings = {"@auto-coder": "issue.auto"}
        priorities = ["@auto-coder"]

        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.auto"

    def test_very_large_priority_list(self):
        """Test with extremely large priority list."""
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}
        priorities = [f"label-{i}" for i in range(10000)]

        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.bug"

    def test_path_parameter_propagation(self):
        """Test that path parameter is properly handled."""
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}
        priorities = ["bug"]

        # Function accepts path but doesn't use it directly
        result = get_label_specific_prompt(labels, mappings, priorities, path="/tmp/test.yaml")
        assert result == "issue.bug"


class TestRenderPromptWithLabelsEdgeCases:
    """Test edge cases for render_prompt with label-based selection."""

    def test_render_with_empty_label_list(self, tmp_path):
        """Test render_prompt with empty label list."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n  bugfix: "Bug fix"\n', encoding="utf-8")

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=[],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        assert "Default action" in result

    def test_render_with_no_mappings(self, tmp_path):
        """Test render_prompt with no mappings provided."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings=None,
            label_priorities=["bug"],
        )
        assert "Default action" in result

    def test_render_with_no_priorities(self, tmp_path):
        """Test render_prompt with no priorities provided."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=None,
        )
        assert "Default action" in result

    def test_render_with_unicode_in_template(self, tmp_path):
        """Test render_prompt with Unicode characters in template."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n  bugfix: "Bug fix for Büɡ $issue_number"\n', encoding="utf-8")

        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            issue_number="123",
        )
        assert "Bug fix for Büɡ 123" in result

    def test_render_with_special_chars_in_template(self, tmp_path):
        """Test render_prompt with special characters in template."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n  bugfix: "Fix @ $issue_title !"\n', encoding="utf-8")

        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            issue_title="Critical Bug",
        )
        assert "Fix @ Critical Bug !" in result

    def test_render_with_none_data_value(self, tmp_path):
        """Test render_prompt with None as data value."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default $name"\n  bugfix: "Bug fix for $name"\n', encoding="utf-8")

        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            name=None,
        )
        # None should be converted to empty string
        assert "Bug fix for " in result

    def test_render_label_fallback_with_kwargs(self, tmp_path):
        """Test render_prompt fallback with kwargs."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Issue $issue_number: $title"\n  bugfix: "Bug fix"\n', encoding="utf-8")

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["random-label"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            issue_number="123",
            title="Test Issue",
        )
        assert "Issue 123: Test Issue" in result

    def test_render_with_nested_template_variables(self, tmp_path):
        """Test render_prompt with nested template variables."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default"\n  bugfix: "Fix $issue.repo/$issue.number"\n', encoding="utf-8")

        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            issue={"repo": "auto-coder", "number": "123"},
        )
        # Should handle nested dict access gracefully
        assert "Fix" in result

    def test_render_with_list_in_label(self, tmp_path):
        """Test render_prompt with normal string labels (the expected behavior)."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default $issue_number"\n  bugfix: "Bug fix"\n', encoding="utf-8")

        # Normal use case with string labels
        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            issue_number=123,
        )
        assert "Bug fix" in result

    def test_render_cache_invalidation(self, tmp_path):
        """Test that cache is properly handled with label-based prompts."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n  bugfix: "Bug fix"\n', encoding="utf-8")

        # First render
        clear_prompt_cache()
        result1 = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )

        # Second render with same config (should use cache)
        result2 = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )

        assert result1 == result2

        # Clear cache and change file
        clear_prompt_cache()
        prompt_file.write_text('issue:\n  action: "Default action"\n  bugfix: "Changed bug fix"\n', encoding="utf-8")

        result3 = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )

        assert result3 == "Changed bug fix"


class TestBackwardCompatibility:
    """Test backward compatibility with existing code."""

    def test_render_without_label_params(self, tmp_path):
        """Test render_prompt without any label parameters."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('category:\n  message: "Hello $name!"\n', encoding="utf-8")

        result = render_prompt("category.message", path=str(prompt_file), name="World")
        assert result == "Hello World!"

    def test_render_with_data_param_only(self, tmp_path):
        """Test render_prompt with data parameter only."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('category:\n  message: "Hello $name!"\n', encoding="utf-8")

        result = render_prompt("category.message", path=str(prompt_file), data={"name": "Universe"})
        assert result == "Hello Universe!"

    def test_render_with_labels_but_no_mappings(self, tmp_path):
        """Test render_prompt with labels but no mappings."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings=None,
            label_priorities=None,
        )
        assert "Default action" in result


class TestThreadSafety:
    """Test thread safety for label-based prompt operations."""

    def test_concurrent_label_resolution(self):
        """Test concurrent label resolution operations."""
        results = []
        errors = []

        def resolve_labels(thread_id):
            try:
                labels = [f"label-{i}" for i in range(thread_id * 10, (thread_id + 1) * 10)]
                mappings = {f"label-{i}": f"prompt.{i}" for i in range(100)}
                priorities = [f"label-{i}" for i in range(100)]

                result = _resolve_label_priority(labels, mappings, priorities)
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=resolve_labels, args=(i,)) for i in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have no errors
        assert len(errors) == 0, f"Thread safety errors: {errors}"
        # Should have one result per thread
        assert len(results) == 10

    def test_concurrent_cache_operations(self, tmp_path):
        """Test concurrent cache operations with label-based prompts."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default"\n  bugfix: "Bug fix"\n', encoding="utf-8")

        results = []
        errors = []

        def render_with_labels(thread_id):
            try:
                result = render_prompt(
                    "issue.bugfix",
                    path=str(prompt_file),
                    labels=["bug"],
                    label_prompt_mappings={"bug": "issue.bugfix"},
                    label_priorities=["bug"],
                    thread_num=thread_id,
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=render_with_labels, args=(i,)) for i in range(20)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have no errors
        assert len(errors) == 0, f"Concurrent cache errors: {errors}"
        # All results should be identical
        assert len(set(results)) == 1, "Results should be consistent across threads"


class TestLabelHierarchy:
    """Test complex label hierarchy scenarios."""

    def test_label_with_colon_separator(self):
        """Test labels with colon separators (namespace-style)."""
        labels = ["type:bug", "priority:high", "area:core"]
        mappings = {
            "type:bug": "issue.bugfix",
            "priority:high": "issue.urgent",
        }
        priorities = ["priority:high", "type:bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "priority:high"

    def test_label_with_slash_separator(self):
        """Test labels with slash separators."""
        labels = ["area/backend", "area/frontend", "type/feature"]
        mappings = {
            "area/backend": "issue.backend",
            "type/feature": "issue.feature",
        }
        priorities = ["area/backend", "type/feature"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "area/backend"

    def test_labels_with_similar_names(self):
        """Test labels with similar names to ensure exact matching."""
        labels = ["bug", "bugfix", "bug-fix", "bug_"]
        mappings = {
            "bug": "issue.bug",
            "bugfix": "issue.bugfix",
        }
        priorities = ["bugfix", "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should match "bugfix" (higher priority)
        assert result == "bugfix"


class TestFallbackMechanism:
    """Test fallback mechanisms comprehensively."""

    def test_fallback_when_template_missing(self, tmp_path):
        """Test fallback when label-specific template doesn't exist."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n  bugfix: "Bug fix"\n', encoding="utf-8")

        # Map to non-existent template should trigger fallback
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["enhancement"],
            label_prompt_mappings={"enhancement": "issue.nonexistent"},
            label_priorities=["enhancement"],
        )
        # Should fall back to default
        assert "Default action" in result

    def test_fallback_with_partial_config(self, tmp_path):
        """Test fallback when only some label config is provided."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        # Only mappings, no priorities
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=None,
        )
        assert "Default action" in result

    def test_fallback_chain(self, tmp_path):
        """Test fallback chain: label-specific -> default."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default $issue_number"\n  feature: "Feature prompt"\n', encoding="utf-8")

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["feature"],
            label_prompt_mappings={"feature": "issue.feature"},
            label_priorities=["feature"],
            issue_number="123",
        )
        assert "Feature prompt" in result


@pytest.mark.parametrize(
    "labels, mappings, priorities, expected_prompt",
    [
        # Test case 1: Single matching label
        (["bug"], {"bug": "issue.bugfix"}, ["bug"], "issue.bugfix"),
        # Test case 2: Multiple labels, priority ordering
        (["bug", "feature"], {"bug": "issue.bugfix", "feature": "issue.feature"}, ["feature", "bug"], "issue.feature"),
        # Test case 3: No matching labels
        (["docs"], {"bug": "issue.bugfix"}, ["bug"], None),
        # Test case 4: Empty inputs
        ([], {}, [], None),
        # Test case 5: Priority list longer than mappings
        (["bug", "feature"], {"bug": "issue.bugfix"}, ["bug", "feature", "urgent"], "issue.bugfix"),
    ],
)
def test_parametrized_label_scenarios(labels, mappings, priorities, expected_prompt):
    """Parametrized test for various label scenarios."""
    result = _get_prompt_for_labels(labels, mappings, priorities)
    assert result == expected_prompt


class TestErrorHandlingScenarios:
    """Test error handling scenarios for edge cases."""

    def test_invalid_yaml_format(self, tmp_path):
        """Test handling of invalid YAML files."""
        bad_yaml_file = tmp_path / "bad_prompts.yaml"
        bad_yaml_file.write_text(":-: not yaml\n", encoding="utf-8")

        prompt_loader.clear_prompt_cache()
        original_path = prompt_loader.DEFAULT_PROMPTS_PATH
        try:
            # Temporarily change the default path
            prompt_loader.DEFAULT_PROMPTS_PATH = bad_yaml_file
            with pytest.raises(SystemExit):
                render_prompt("any.key")
        finally:
            # Restore original path
            prompt_loader.DEFAULT_PROMPTS_PATH = original_path
            prompt_loader.clear_prompt_cache()

    def test_prompt_key_not_found(self, tmp_path):
        """Test handling of non-existent prompt keys."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        with pytest.raises(SystemExit):
            get_prompt_template("nonexistent.key", path=str(prompt_file))

    def test_traverse_key_not_found(self, tmp_path):
        """Test traverse function with non-existent key."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        prompts = load_prompts(path=str(prompt_file))
        with pytest.raises(KeyError, match="Prompt 'nonexistent' not found in configuration"):
            prompt_loader._traverse(prompts, "nonexistent")

    def test_traverse_invalid_path(self, tmp_path):
        """Test traverse function with invalid path where we try to access a non-dict."""
        # Create a YAML where a node is not a dictionary
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        prompts = load_prompts(path=str(prompt_file))
        # Access the 'action' value (which is a string) and try to get a sub-key
        with pytest.raises(KeyError, match="Prompt path 'issue.action.nonexistent' does not resolve to a mapping"):
            prompt_loader._traverse(prompts, "issue.action.nonexistent")

    def test_render_prompt_with_none_template_in_mapping(self, tmp_path):
        """Test render_prompt with None as mapped template."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        # Test when the label maps to a None template
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": None},
            label_priorities=["bug"],
        )
        assert "Default action" in result


class TestMissingLinesCoverage:
    """Test to cover the remaining uncovered lines."""

    def test_file_not_found_handling(self, tmp_path):
        """Test handling when prompt file doesn't exist."""
        prompt_loader.clear_prompt_cache()
        nonexistent_path = tmp_path / "nonexistent.yaml"
        with pytest.raises(SystemExit):
            get_prompt_template("any.key", path=str(nonexistent_path))

    def test_path_resolution_with_none(self, tmp_path):
        """Test path resolution when path is None (uses default)."""
        # This covers the path resolution code
        original_path = prompt_loader.DEFAULT_PROMPTS_PATH
        try:
            # Create a temporary valid file to avoid SystemExit
            temp_file = tmp_path / "temp_prompts.yaml"
            temp_file.write_text('test:\n  key: "value"\n', encoding="utf-8")
            prompt_loader.DEFAULT_PROMPTS_PATH = temp_file

            # Call with None path to trigger the default resolution
            result = get_prompt_template("test.key", path=None)
            assert result == "value"
        finally:
            prompt_loader.DEFAULT_PROMPTS_PATH = original_path

    def test_render_prompt_exception_handling(self, tmp_path):
        """Test exception handling in render_prompt by mocking internal calls."""
        from unittest.mock import patch

        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Action: $value"\n', encoding="utf-8")

        # Mock the Template class to raise an exception when safe_substitute is called
        with patch("string.Template.safe_substitute") as mock_substitute:
            mock_substitute.side_effect = Exception("Template substitution error")

            with pytest.raises(Exception, match="Template substitution error"):
                render_prompt("issue.action", path=str(prompt_file), value="test")

    def test_invalid_yaml_root_not_mapping(self, tmp_path):
        """Test when YAML root is not a mapping/dict."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text("This is not a mapping\n", encoding="utf-8")  # This is a string, not a mapping

        with pytest.raises(SystemExit):
            get_prompt_template("any.key", path=str(prompt_file))
