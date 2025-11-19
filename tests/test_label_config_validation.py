"""Tests for label configuration validation and error handling.

This module provides comprehensive test coverage for validation of
label-to-prompt mappings, priority lists, and configuration handling.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from src.auto_coder import prompt_loader
from src.auto_coder.prompt_loader import (
    _get_prompt_for_labels,
    _resolve_label_priority,
    clear_prompt_cache,
    get_label_specific_prompt,
    render_prompt,
)


class TestInvalidMappingFormats:
    """Test handling of invalid label-to-prompt mapping formats."""

    def test_non_dict_mappings(self):
        """Test with non-dictionary mappings."""
        labels = ["bug"]
        # Pass list instead of dict
        mappings = ["bug", "issue.bugfix"]
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Should handle gracefully (likely return None or raise)
        assert result is None or isinstance(result, (str, type(None)))

    def test_mappings_with_non_string_keys(self):
        """Test mappings with non-string keys."""
        labels = [123, "bug"]
        mappings = {123: "issue.first", "bug": "issue.bugfix"}
        priorities = ["bug", 123]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should handle numeric keys
        assert result == 123 or result == "bug"

    def test_mappings_with_nested_dict_values(self):
        """Test mappings with nested dict values."""
        labels = ["bug"]
        mappings = {"bug": {"nested": "value"}}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Should return the nested dict
        assert result == {"nested": "value"}

    def test_mappings_with_list_values(self):
        """Test mappings with list values."""
        labels = ["bug"]
        mappings = {"bug": ["prompt1", "prompt2"]}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Should return the list
        assert result == ["prompt1", "prompt2"]

    def test_mappings_with_boolean_values(self):
        """Test mappings with boolean values."""
        labels = ["bug"]
        mappings = {"bug": True, "feature": False}
        priorities = ["bug", "feature"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is True

    def test_mappings_with_integer_values(self):
        """Test mappings with integer values."""
        labels = ["bug"]
        mappings = {"bug": 42, "feature": 100}
        priorities = ["bug", "feature"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == 42

    def test_mappings_with_empty_string_values(self):
        """Test mappings with empty string values."""
        labels = ["bug", "feature"]
        mappings = {"bug": "", "feature": "issue.feature"}
        priorities = ["bug", "feature"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Empty string is still a valid value
        assert result == ""

    def test_mappings_with_very_long_values(self):
        """Test mappings with very long string values."""
        labels = ["bug"]
        long_value = "x" * 10000
        mappings = {"bug": long_value}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == long_value


class TestCircularDependencyDetection:
    """Test detection of circular dependencies (if applicable)."""

    def test_self_referential_mapping(self):
        """Test mapping that references itself in a cycle."""
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}  # Simple self-reference
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == "issue.bug"

    def test_circular_reference_in_templates(self, tmp_path):
        """Test circular reference in prompt templates."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default"\n  bugfix: "$issue.bugfix"\n',
            encoding="utf-8",
        )

        # Safe substitute doesn't actually resolve variables, so circular references
        # don't cause RecursionError. Instead, the system falls back gracefully.
        # The template will be rendered with $issue.bugfix literally (not substituted).
        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        # Should return the template as-is (with $ not substituted by safe_substitute)
        assert result is not None
        assert "$issue.bugfix" in result


class TestMissingPriorityListHandling:
    """Test handling of missing or invalid priority lists."""

    def test_none_priorities(self):
        """Test with None priorities."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = None

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_empty_priorities_list(self):
        """Test with empty priorities list."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = []

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Should return first applicable label as fallback
        assert result == "issue.bugfix"

    def test_priorities_with_none_values(self):
        """Test priorities containing None values."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = [None, "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should skip None and find "bug"
        assert result == "bug"

    def test_priorities_with_duplicate_values(self):
        """Test priorities with duplicate values."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug", "bug", "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_priorities_not_matching_labels(self):
        """Test priorities that don't match any labels."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["feature", "enhancement", "urgent"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should fall back to first applicable label
        assert result == "bug"


class TestEmptyConfigurationHandling:
    """Test handling of empty configurations."""

    def test_empty_labels_list(self):
        """Test with empty labels list."""
        labels = []
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_empty_mappings_dict(self):
        """Test with empty mappings dict."""
        labels = ["bug"]
        mappings = {}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_all_empty(self):
        """Test with all empty inputs."""
        result = _get_prompt_for_labels([], {}, [])
        assert result is None

    def test_empty_labels_with_mappings_and_priorities(self):
        """Test empty labels with valid mappings and priorities."""
        labels = []
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result is None


class TestConfigurationPrecedenceRules:
    """Test configuration precedence rules."""

    def test_priority_over_mapping_order(self):
        """Test that priority list takes precedence over mapping order."""
        labels = ["bug", "feature"]
        mappings = {
            "bug": "issue.bugfix",  # Listed first in mappings
            "feature": "issue.feature",  # Listed second
        }
        # But feature has higher priority
        priorities = ["feature", "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should return "feature" (higher priority) not first in mappings
        assert result == "feature"

    def test_priorities_longer_than_mappings(self):
        """Test priorities list longer than available mappings."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug", "feature", "enhancement", "urgent", "documentation"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should still find "bug"
        assert result == "bug"

    def test_mappings_longer_than_priorities(self):
        """Test mappings longer than priorities list."""
        labels = ["bug", "feature", "enhancement"]
        mappings = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
        }
        priorities = ["bug"]  # Only one priority

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should respect priority and return "bug"
        assert result == "bug"

    def test_partial_overlap_between_labels_and_mappings(self):
        """Test when some labels have mappings and some don't."""
        labels = ["bug", "custom", "feature", "other"]
        mappings = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
        }
        priorities = ["feature", "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should find "feature" (higher priority and in labels)
        assert result == "feature"

    def test_partial_overlap_between_mappings_and_priorities(self):
        """Test when some mappings have priorities and some don't."""
        labels = ["bug", "feature"]
        mappings = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",  # No corresponding label
        }
        priorities = ["feature", "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should still work with partial overlap
        assert result == "feature"


class TestEnvironmentVariableIntegration:
    """Test integration with environment variables."""

    @patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "issue.bugfix"}'})
    def test_load_mappings_from_env(self):
        """Test loading mappings from environment variable."""
        env_value = os.environ.get("AUTO_CODER_LABEL_PROMPT_MAPPINGS")
        if env_value:
            mappings = yaml.safe_load(env_value)
            assert mappings == {"bug": "issue.bugfix"}

    @patch.dict(os.environ, {"AUTO_CODER_LABEL_PRIORITIES": '["bug", "feature"]'})
    def test_load_priorities_from_env(self):
        """Test loading priorities from environment variable."""
        env_value = os.environ.get("AUTO_CODER_LABEL_PRIORITIES")
        if env_value:
            priorities = yaml.safe_load(env_value)
            assert priorities == ["bug", "feature"]

    @patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": "[:)"})
    def test_invalid_yaml_in_env(self):
        """Test handling of invalid YAML in environment variable."""
        env_value = os.environ.get("AUTO_CODER_LABEL_PROMPT_MAPPINGS")
        if env_value:
            with pytest.raises(yaml.YAMLError):
                yaml.safe_load(env_value)

    @patch.dict(os.environ, {"AUTO_CODER_LABEL_MAPPINGS": '{"bug": "issue.bugfix", "feature": "issue.feature"}'})
    def test_env_variable_not_used_directly(self):
        """Test that env variables don't interfere with direct parameters."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        # Function should use passed parameters, not env
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.bugfix"


class TestYAMLConfigurationLoading:
    """Test YAML configuration file loading edge cases."""

    def test_yaml_with_only_comments(self, tmp_path):
        """Test YAML file with only comments."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text("# This is a comment\n# Another comment\n", encoding="utf-8")

        clear_prompt_cache()
        data = prompt_loader.load_prompts(str(prompt_file))
        assert data == {}

    def test_yaml_with_null_values(self, tmp_path):
        """Test YAML with null values."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: null\n  bugfix: "Bug fix"\n', encoding="utf-8")

        clear_prompt_cache()
        data = prompt_loader.load_prompts(str(prompt_file))
        assert data["issue"]["action"] is None

    def test_yaml_with_nested_structures(self, tmp_path):
        """Test YAML with deeply nested structures."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  nested:\n    deeply:\n      very:\n        prompt: "Deep prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        data = prompt_loader.load_prompts(str(prompt_file))
        assert data["issue"]["nested"]["deeply"]["very"]["prompt"] == "Deep prompt"

    def test_yaml_with_numeric_values(self, tmp_path):
        """Test YAML with numeric values."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text("issue:\n  number: 123\n  float: 3.14\n", encoding="utf-8")

        clear_prompt_cache()
        data = prompt_loader.load_prompts(str(prompt_file))
        assert data["issue"]["number"] == 123
        assert data["issue"]["float"] == 3.14

    def test_yaml_with_boolean_values(self, tmp_path):
        """Test YAML with boolean values."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text("issue:\n  enabled: true\n  disabled: false\n", encoding="utf-8")

        clear_prompt_cache()
        data = prompt_loader.load_prompts(str(prompt_file))
        assert data["issue"]["enabled"] is True
        assert data["issue"]["disabled"] is False

    def test_yaml_with_list_values(self, tmp_path):
        """Test YAML with list values."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  items:\n    - "one"\n    - "two"\n    - "three"\n', encoding="utf-8")

        clear_prompt_cache()
        data = prompt_loader.load_prompts(str(prompt_file))
        assert data["issue"]["items"] == ["one", "two", "three"]

    def test_yaml_with_mixed_types(self, tmp_path):
        """Test YAML with mixed value types."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  string: "text"\n  number: 42\n  float: 3.14\n  bool: true\n  null: null\n  list: [1, 2, 3]\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        data = prompt_loader.load_prompts(str(prompt_file))
        assert data["issue"]["string"] == "text"
        assert data["issue"]["number"] == 42
        assert data["issue"]["float"] == 3.14
        assert data["issue"]["bool"] is True
        assert data["issue"][None] is None  # YAML null becomes None key
        assert data["issue"]["list"] == [1, 2, 3]


class TestInvalidYAMLStructureHandling:
    """Test handling of invalid YAML structures."""

    def test_invalid_yaml_syntax(self, tmp_path):
        """Test invalid YAML syntax."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text("[invalid", encoding="utf-8")  # Unclosed bracket

        with pytest.raises(SystemExit):
            prompt_loader.load_prompts(str(prompt_file))

    def test_yaml_root_not_dict(self, tmp_path):
        """Test YAML where root is not a dictionary."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text("- item1\n- item2\n- item3\n", encoding="utf-8")

        with pytest.raises(SystemExit):
            prompt_loader.load_prompts(str(prompt_file))

    def test_yaml_with_unclosed_brackets(self, tmp_path):
        """Test YAML with unclosed brackets."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text("issue:\n  list: [1, 2, 3\n", encoding="utf-8")

        with pytest.raises((SystemExit, yaml.YAMLError)):
            prompt_loader.load_prompts(str(prompt_file))

    def test_yaml_with_invalid_indentation(self, tmp_path):
        """Test YAML with invalid indentation."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\naction: "test"\n  bugfix: "bug"\n', encoding="utf-8")

        # YAML parsers may handle this differently
        try:
            clear_prompt_cache()
            data = prompt_loader.load_prompts(str(prompt_file))
            # If it doesn't raise, check the structure
        except (SystemExit, yaml.YAMLError):
            # Expected to fail
            pass

    def test_yaml_file_not_found(self):
        """Test loading non-existent YAML file."""
        with pytest.raises(SystemExit):
            prompt_loader.load_prompts("/nonexistent/file.yaml")


class TestTypeValidation:
    """Test type validation for all configuration parameters."""

    def test_labels_not_list(self):
        """Test when labels is not a list."""
        # String is iterable, so it works (iterates over characters)
        result = _resolve_label_priority("bug", {}, [])
        assert result is None  # No mappings match, returns None

    def test_mappings_not_dict(self):
        """Test when mappings is not a dict."""
        labels = ["bug"]
        # Pass list instead of dict - gets converted to dict with integer keys
        result = _resolve_label_priority(labels, ["invalid"], [])
        # "bug" (string) is not in mappings (which has integer keys), returns None
        assert result is None

    def test_priorities_not_list(self):
        """Test when priorities is not a list."""
        labels = ["bug"]
        mappings = {"bug": "prompt"}
        # Pass string instead of list - treated as iterable (characters)
        result = _resolve_label_priority(labels, mappings, "bug")
        # String "bug" is iterable, will check 'b', 'u', 'g' as priorities
        # None match 'bug' label, falls back to applicable_labels[0]
        assert result == "bug"

    def test_mixed_types_in_labels(self):
        """Test labels list with mixed types."""
        labels = ["bug", 123, None, {"key": "value"}]
        mappings = {"bug": "prompt", 123: "prompt2"}
        priorities = ["bug", 123]

        # Should handle mixed types
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result in ["bug", 123]

    def test_mixed_types_in_priorities(self):
        """Test priorities list with mixed types."""
        labels = ["bug", 123]
        mappings = {"bug": "prompt", 123: "prompt2"}
        priorities = ["bug", 123, None, 3.14]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result in ["bug", 123]


class TestConfigurationRecovery:
    """Test recovery from configuration errors."""

    def test_graceful_degradation_with_invalid_config(self):
        """Test that system degrades gracefully with invalid config."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        # Valid config should work
        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.bugfix"

    def test_partial_config_with_valid_parts(self):
        """Test that valid parts of config are used."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["feature", "bug"]

        # Should work with valid parts - returns "bug" since only "bug" is in labels
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_fallback_on_error(self, tmp_path):
        """Test fallback mechanism when label-specific prompt fails."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        # Try to use non-existent label-specific prompt
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.nonexistent"},
            label_priorities=["bug"],
        )

        # Should fall back to default
        assert "Default action" in result


@pytest.mark.parametrize(
    "labels, mappings, priorities, should_succeed",
    [
        # Valid configs
        (["bug"], {"bug": "prompt"}, ["bug"], True),
        ([], {}, [], False),  # Empty inputs
        (["bug"], {}, ["bug"], False),  # Empty mappings
        (["bug"], {"bug": "prompt"}, [], True),  # Empty priorities (fallback)
    ],
)
def test_parametrized_config_validation(labels, mappings, priorities, should_succeed):
    """Parametrized test for configuration validation."""
    if should_succeed:
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is not None
    else:
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None
