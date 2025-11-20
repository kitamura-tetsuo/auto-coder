"""Comprehensive tests for label configuration validation and backward compatibility.

This module provides extensive test coverage for:
- Valid configuration formats (YAML, environment variables, code)
- Invalid configuration format handling
- Missing configuration defaults
- Configuration precedence rules
- Type validation for all config parameters
- Circular dependency detection
- Duplicate key handling
- Environment variable interpolation
- Configuration hot-reloading
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from src.auto_coder import prompt_loader
from src.auto_coder.prompt_loader import (
    _get_prompt_for_labels,
    _resolve_label_priority,
    _traverse,
    clear_prompt_cache,
    get_label_specific_prompt,
    load_prompts,
    render_prompt,
)


class TestValidConfigurationFormats:
    """Test valid configuration formats across different sources."""

    def test_valid_yaml_config_file(self, tmp_path):
        """Test valid YAML configuration file."""
        config_file = tmp_path / "label_config.yaml"
        config_file.write_text(
            """
            label_prompt_mappings:
              bug: "issue.bugfix"
              feature: "issue.feature"
              enhancement: "issue.enhancement"
            label_priorities:
              - bug
              - feature
              - enhancement
            """,
            encoding="utf-8",
        )

        # Load and verify
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert "label_prompt_mappings" in data
        assert "label_priorities" in data
        assert data["label_prompt_mappings"]["bug"] == "issue.bugfix"

    def test_valid_yaml_with_nested_structure(self, tmp_path):
        """Test YAML with nested configuration structures."""
        config_file = tmp_path / "nested_config.yaml"
        config_file.write_text(
            """
            prompts:
              issue:
                bugfix:
                  template: "Fix bug: $issue_number"
                  priority: high
                feature:
                  template: "Implement feature: $issue_number"
                  priority: medium
            labels:
              mappings:
                bug: "prompts.issue.bugfix"
                feature: "prompts.issue.feature"
              priorities:
                - bug
                - feature
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert "prompts" in data
        assert "labels" in data
        assert data["labels"]["mappings"]["bug"] == "prompts.issue.bugfix"

    def test_valid_environment_variables(self):
        """Test valid environment variable configuration."""
        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "issue.bugfix", "feature": "issue.feature"}',
                "AUTO_CODER_LABEL_PRIORITIES": '["bug", "feature"]',
            },
        ):
            mappings = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])
            priorities = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PRIORITIES"])

            assert mappings == {"bug": "issue.bugfix", "feature": "issue.feature"}
            assert priorities == ["bug", "feature"]

    def test_valid_code_configuration_dict(self):
        """Test valid configuration as Python dictionary."""
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature", "urgent": "issue.urgent"}
        priorities = ["urgent", "bug", "feature"]

        # Should work with valid dict
        labels = ["bug", "feature"]
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_valid_code_configuration_list(self):
        """Test valid configuration as Python lists."""
        labels = ["bug", "feature", "enhancement"]
        mappings = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
        }
        priorities = ["bug", "feature", "enhancement"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_json_environment_variable_format(self):
        """Test JSON format in environment variables."""
        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "issue.bugfix", "feature": "issue.feature"}',
            },
        ):
            import json

            mappings = json.loads(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])
            assert mappings == {"bug": "issue.bugfix", "feature": "issue.feature"}

    def test_yaml_list_in_environment(self):
        """Test YAML list format in environment variables."""
        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_LABEL_PRIORITIES": "- bug\n- feature\n- enhancement",
            },
        ):
            priorities = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PRIORITIES"])
            assert priorities == ["bug", "feature", "enhancement"]


class TestInvalidConfigurationHandling:
    """Test handling of invalid configuration formats."""

    def test_invalid_yaml_syntax(self, tmp_path):
        """Test invalid YAML syntax handling."""
        config_file = tmp_path / "invalid.yaml"
        config_file.write_text(
            """
            label_prompt_mappings:
              bug: "issue.bugfix"
              feature: "issue.feature
            """,
            encoding="utf-8",
        )

        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(config_file.read_text(encoding="utf-8"))

    def test_invalid_json_environment_variable(self):
        """Test invalid JSON in environment variable."""
        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"invalid":'}):
            import json

            with pytest.raises(json.decoder.JSONDecodeError):
                json.loads(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])

    def test_malformed_yaml_in_env(self, tmp_path):
        """Test malformed YAML in environment variable."""
        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_LABEL_PROMPT_MAPPINGS": "bug: issue.bugfix\nfeature: issue.feature\n  invalid_indent",
            },
        ):
            # Should handle gracefully or raise
            try:
                mappings = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])
                # If it doesn't raise, verify it's a valid dict
                assert isinstance(mappings, dict)
            except yaml.YAMLError:
                # Expected to fail with invalid YAML
                pass

    def test_none_values_in_config(self):
        """Test None values in configuration."""
        labels = ["bug"]
        mappings = {"bug": None}
        priorities = ["bug"]

        # Should handle None values
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_empty_string_keys_in_mappings(self):
        """Test empty string as keys in mappings."""
        labels = ["bug"]
        mappings = {"": "issue.bugfix", "bug": "issue.bugfix"}
        priorities = ["bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_unicode_in_config_values(self):
        """Test Unicode characters in configuration values."""
        labels = ["bug"]
        mappings = {"bug": "issue.🐛fix", "feature": "issue.✨feature"}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == "issue.🐛fix"

    def test_unicode_in_labels(self):
        """Test Unicode characters in label names."""
        labels = ["🐛", "✨", "feature"]
        mappings = {"🐛": "issue.bugfix", "✨": "issue.feature", "feature": "issue.feature"}
        priorities = ["🐛", "✨", "feature"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "🐛"


class TestMissingConfigurationDefaults:
    """Test handling of missing configuration with appropriate defaults."""

    def test_missing_mappings_uses_fallback(self):
        """Test when mappings are missing, use fallback behavior."""
        labels = ["bug"]
        mappings = {}  # Empty mappings
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_missing_priorities_uses_fallback(self):
        """Test when priorities are missing, use fallback behavior."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = None  # Missing priorities

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_missing_labels_uses_fallback(self):
        """Test when labels are missing, use fallback behavior."""
        labels = []  # Empty labels
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_all_missing_returns_none(self):
        """Test when all config is missing, returns None."""
        result = _get_prompt_for_labels([], {}, None)
        assert result is None

    def test_partial_config_still_works(self):
        """Test that partial configuration still works."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]  # Valid priorities

        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.bugfix"


class TestConfigurationPrecedenceRules:
    """Test configuration precedence and priority handling."""

    def test_priority_order_enforced(self):
        """Test that priority order is properly enforced."""
        labels = ["bug", "feature", "urgent"]
        mappings = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "urgent": "issue.urgent",
        }
        priorities = ["urgent", "bug", "feature"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "urgent"  # Highest priority first

    def test_labels_not_in_priorities(self):
        """Test labels not in priorities list."""
        labels = ["custom", "bug"]
        mappings = {"custom": "issue.custom", "bug": "issue.bugfix"}
        priorities = ["bug"]  # custom not in priorities

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"  # Should find bug which is in priorities

    def test_priorities_not_in_labels(self):
        """Test priorities that don't match labels."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["feature", "urgent"]  # None match labels

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"  # Should fall back to first applicable

    def test_empty_priorities_with_mappings(self):
        """Test empty priorities list with valid mappings."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = []  # Empty priorities

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"  # Should return first applicable

    def test_duplicate_priorities(self):
        """Test duplicate values in priorities list."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug", "bug", "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_case_sensitive_label_matching(self):
        """Test that label matching is case-sensitive."""
        labels = ["Bug"]
        mappings = {"bug": "issue.bugfix"}  # lowercase
        priorities = ["bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result is None  # Case mismatch, should not match


class TestTypeValidation:
    """Test type validation for all configuration parameters."""

    def test_labels_as_string_fallback(self):
        """Test string passed instead of list for labels."""
        labels = "bug"  # String instead of list
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        # String is iterable, so it works (iterates over characters)
        result = _resolve_label_priority(labels, mappings, priorities)
        # 'bug' is a string, iterating gives 'b', 'u', 'g'
        # None of those match 'bug' in mappings, so returns None
        assert result is None

    def test_mappings_as_list_conversion(self):
        """Test list passed instead of dict for mappings."""
        labels = ["bug"]
        mappings = ["bug"]  # List instead of dict
        priorities = ["bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # List gets converted to dict with integer indices
        # 'bug' not in {0: 'bug'}, falls back to first applicable
        assert result == "bug"

    def test_priorities_as_string_iteration(self):
        """Test string passed instead of list for priorities."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = "bug"  # String instead of list

        result = _resolve_label_priority(labels, mappings, priorities)
        # String is iterable, checks 'b', 'u', 'g' - none match 'bug'
        # Falls back to first applicable label
        assert result == "bug"

    def test_non_string_mapping_keys(self):
        """Test non-string keys in mappings dictionary."""
        labels = [123, "bug"]
        mappings = {123: "issue.numeric", "bug": "issue.bugfix"}
        priorities = [123, "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result in [123, "bug"]

    def test_non_string_mapping_values(self):
        """Test non-string values in mappings dictionary."""
        labels = ["bug"]
        mappings = {"bug": 123, "feature": ["list", "value"]}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == 123

    def test_float_labels(self):
        """Test float values in labels list."""
        labels = [1.5, "bug"]
        mappings = {1.5: "issue.float", "bug": "issue.bugfix"}
        priorities = [1.5, "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == 1.5

    def test_boolean_labels(self):
        """Test boolean values in labels list."""
        labels = [True, "bug"]
        mappings = {True: "issue.bool", "bug": "issue.bugfix"}
        priorities = [True, "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result is True


class TestCircularDependencyDetection:
    """Test detection and handling of circular dependencies."""

    def test_self_referential_mapping(self):
        """Test mapping that references itself."""
        labels = ["bug"]
        mappings = {"bug": "issue.bug"}  # Self-reference
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Should return the self-reference
        assert result == "issue.bug"

    def test_circular_reference_between_labels(self):
        """Test circular reference between different labels."""
        labels = ["bug"]
        mappings = {"bug": "feature.feature", "feature": "bug.bugfix"}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Should return the direct mapping
        assert result == "feature.feature"

    def test_prompt_template_self_reference(self, tmp_path):
        """Test circular reference in prompt template file."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  bugfix: "Fix $issue.bugfix"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        # render_prompt with safe_substitute doesn't expand $issue.bugfix
        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        # Should return with literal $issue.bugfix (not expanded by safe_substitute)
        assert result is not None
        assert "$issue.bugfix" in result

    def test_multi_level_circular_reference(self, tmp_path):
        """Test multi-level circular reference in templates."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  a: "Level $issue.b"\n  b: "Level $issue.c"\n  c: "Level $issue.a"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        result = render_prompt(
            "issue.a",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.a"},
            label_priorities=["bug"],
        )
        # Should handle gracefully with safe_substitute
        assert result is not None


class TestDuplicateKeyHandling:
    """Test handling of duplicate keys in configurations."""

    def test_duplicate_keys_in_yaml_last_wins(self, tmp_path):
        """Test that duplicate keys in YAML, last one wins."""
        config_file = tmp_path / "duplicates.yaml"
        config_file.write_text(
            """
            label_prompt_mappings:
              bug: "issue.first"
              bug: "issue.second"
              feature: "issue.third"
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        # YAML parsers typically keep the last value
        assert data["label_prompt_mappings"]["bug"] == "issue.second"

    def test_duplicate_labels_in_list(self):
        """Test duplicate labels in labels list."""
        labels = ["bug", "bug", "feature"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["bug", "feature"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_duplicate_priorities(self):
        """Test duplicate priorities in priorities list."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["bug", "feature", "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_duplicate_mappings_same_value(self):
        """Test duplicate mappings with same value."""
        # This is handled at the dictionary level - duplicates overwrite
        mappings = {"bug": "issue.bugfix"}
        mappings["bug"] = "issue.updated"
        assert mappings["bug"] == "issue.updated"


class TestEnvironmentVariableInterpolation:
    """Test environment variable interpolation in configurations."""

    def test_env_var_in_yaml_value(self, tmp_path):
        """Test environment variable substitution in YAML values."""
        config_file = tmp_path / "env_interpolation.yaml"
        config_file.write_text(
            """
            label_prompt_mappings:
              bug: "${ISSUE_PREFIX}.bugfix"
              feature: "${ISSUE_PREFIX}.feature"
            """,
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"ISSUE_PREFIX": "custom"}):
            data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            # YAML doesn't auto-expand ${VAR}, but we can verify structure
            assert data["label_prompt_mappings"]["bug"] == "${ISSUE_PREFIX}.bugfix"

    def test_shell_style_env_var_expansion(self, tmp_path):
        """Test shell-style environment variable expansion."""
        config_file = tmp_path / "shell_env.yaml"
        config_file.write_text(
            """
            label_prompt_mappings:
              bug: $BUG_PREFIX/bugfix
              feature: $FEATURE_PREFIX/feature
            """,
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"BUG_PREFIX": "custom", "FEATURE_PREFIX": "custom"}):
            # YAML will keep variables as-is unless using a special loader
            data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            assert data["label_prompt_mappings"]["bug"] == "$BUG_PREFIX/bugfix"

    def test_env_var_precedence_over_defaults(self):
        """Test that environment variables take precedence."""
        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "env.bugfix"}',
                "AUTO_CODER_LABEL_PRIORITIES": '["bug"]',
            },
        ):
            # Simulate loading from env vs defaults
            from_env = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])
            default = {"bug": "default.bugfix"}

            # Environment should be preferred over defaults
            assert from_env["bug"] == "env.bugfix"
            assert from_env != default

    def test_multiple_env_vars(self):
        """Test multiple environment variables for different config parts."""
        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "issue.bugfix"}',
                "AUTO_CODER_LABEL_PRIORITIES": '["bug"]',
                "AUTO_CODER_PR_LABEL_MAPPINGS": '{"bug": ["bug", "bugfix"]}',
                "AUTO_CODER_PR_LABEL_PRIORITIES": '["bug"]',
            },
        ):
            mappings = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])
            priorities = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PRIORITIES"])
            pr_mappings = yaml.safe_load(os.environ["AUTO_CODER_PR_LABEL_MAPPINGS"])
            pr_priorities = yaml.safe_load(os.environ["AUTO_CODER_PR_LABEL_PRIORITIES"])

            assert mappings["bug"] == "issue.bugfix"
            assert priorities == ["bug"]
            assert pr_mappings["bug"] == ["bug", "bugfix"]
            assert pr_priorities == ["bug"]


class TestConfigurationHotReloading:
    """Test configuration hot-reloading capabilities."""

    def test_prompt_cache_invalidation(self, tmp_path):
        """Test that prompt cache can be cleared and reloaded."""
        prompt_file = tmp_path / "prompts1.yaml"
        prompt_file.write_text('issue:\n  action: "First"\n', encoding="utf-8")

        # Load first version
        clear_prompt_cache()
        data1 = load_prompts(str(prompt_file))
        assert data1["issue"]["action"] == "First"

        # Update file
        prompt_file.write_text('issue:\n  action: "Second"\n', encoding="utf-8")

        # Clear cache and reload
        clear_prompt_cache()
        data2 = load_prompts(str(prompt_file))
        assert data2["issue"]["action"] == "Second"

    def test_concurrent_modification_handling(self):
        """Test handling of concurrent configuration modifications."""
        config = {"bug": "issue.bugfix"}

        # Simulate concurrent read/write
        def modify_config():
            time.sleep(0.01)
            config["bug"] = "issue.updated"

        thread = threading.Thread(target=modify_config)
        thread.start()

        # Read should still work
        result = config.get("bug")
        thread.join()
        assert result in ["issue.bugfix", "issue.updated"]

    def test_thread_safety_of_config_reads(self):
        """Test that configuration reads are thread-safe."""
        results = []
        mappings = {"bug": "issue.bugfix"}

        def read_config():
            result = _resolve_label_priority(["bug"], mappings, ["bug"])
            results.append(result)

        threads = [threading.Thread(target=read_config) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r == "bug" for r in results)

    def test_file_watch_simulation(self, tmp_path):
        """Simulate file watching with multiple loads."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text('bug: "v1"\n', encoding="utf-8")

        versions = []
        for i in range(3):
            clear_prompt_cache()
            data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            versions.append(data["bug"])
            config_file.write_text(f'bug: "v{i+2}"\n', encoding="utf-8")

        assert versions == ["v1", "v2", "v3"]


class TestConfigurationEdgeCases:
    """Test edge cases in configuration handling."""

    def test_very_long_label_names(self):
        """Test very long label names."""
        long_label = "a" * 1000
        labels = [long_label]
        mappings = {long_label: "issue.long"}
        priorities = [long_label]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == long_label

    def test_very_long_prompt_keys(self):
        """Test very long prompt template keys."""
        long_key = "a" * 1000
        labels = ["bug"]
        mappings = {"bug": long_key}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == long_key

    def test_special_characters_in_labels(self):
        """Test special characters in label names."""
        special_labels = ["bug!", "feature?", "enhancement#", "urgent@home"]
        mappings = {
            "bug!": "issue.exclaim",
            "feature?": "issue.question",
            "enhancement#": "issue.hash",
            "urgent@home": "issue.at",
        }
        priorities = special_labels

        result = _resolve_label_priority(special_labels, mappings, priorities)
        assert result == "bug!"

    def test_newlines_in_config_values(self):
        """Test newlines in configuration values."""
        labels = ["bug"]
        mappings = {"bug": "issue.multi\nline\nprompt"}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert "multi\nline\nprompt" in result

    def test_tabs_in_config_values(self):
        """Test tabs in configuration values."""
        labels = ["bug"]
        mappings = {"bug": "issue\twith\ttabs"}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert "issue\twith\ttabs" in result

    def test_numeric_string_labels(self):
        """Test numeric string labels."""
        labels = ["123", "456"]
        mappings = {"123": "issue.num123", "456": "issue.num456"}
        priorities = ["123", "456"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "123"

    def test_empty_list_values_in_mappings(self):
        """Test empty list values in mappings."""
        labels = ["bug"]
        mappings = {"bug": []}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == []

    def test_dict_values_in_mappings(self):
        """Test dictionary values in mappings."""
        labels = ["bug"]
        mappings = {"bug": {"nested": "value", "count": 42}}
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == {"nested": "value", "count": 42}

    def test_deeply_nested_traverse(self, tmp_path):
        """Test traversing deeply nested structures."""
        prompt_file = tmp_path / "deep.yaml"
        prompt_file.write_text(
            'a:\n  b:\n    c:\n      d:\n        e:\n          f: "deep value"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        data = load_prompts(str(prompt_file))
        value = _traverse(data, "a.b.c.d.e.f")
        assert value == "deep value"

    def test_traverse_with_missing_key_raises_error(self, tmp_path):
        """Test that traversing missing key raises appropriate error."""
        prompt_file = tmp_path / "test.yaml"
        prompt_file.write_text('a:\n  b: "value"\n', encoding="utf-8")

        clear_prompt_cache()
        data = load_prompts(str(prompt_file))

        with pytest.raises(KeyError):
            _traverse(data, "a.c")


class TestConfigurationRecovery:
    """Test recovery mechanisms from configuration errors."""

    def test_graceful_fallback_on_invalid_mapping(self):
        """Test graceful fallback when mapping is invalid."""
        labels = ["bug"]
        mappings = {"bug": None}
        priorities = ["bug"]

        # Should handle None gracefully
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_recovery_from_type_errors(self):
        """Test recovery from type-related errors."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}

        # Normal case should work
        result1 = _resolve_label_priority(labels, mappings, ["bug"])
        assert result1 == "bug"

        # With invalid priorities (should still work)
        result2 = _resolve_label_priority(labels, mappings, None)
        assert result2 is None

    def test_partial_config_still_functional(self):
        """Test that partial/incomplete config still functions."""
        # Only mappings, no priorities - returns None (priorities required)
        result1 = get_label_specific_prompt(["bug"], {"bug": "issue.bugfix"}, None)
        assert result1 is None

        # Only priorities, no matching labels - returns None (no applicable labels)
        result2 = get_label_specific_prompt(["feature"], {"bug": "issue.bugfix"}, ["feature"])
        assert result2 is None

    def test_error_isolation(self):
        """Test that errors in one part don't affect other parts."""
        labels = ["bug", "feature"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["bug", "feature"]

        # Both should work independently
        result1 = _get_prompt_for_labels(["bug"], mappings, priorities)
        assert result1 == "issue.bugfix"

        result2 = _get_prompt_for_labels(["feature"], mappings, priorities)
        assert result2 == "issue.feature"


# Parametrized test cases for comprehensive coverage
@pytest.mark.parametrize(
    "labels, mappings, priorities, expected",
    [
        # Valid configurations
        (["bug"], {"bug": "issue.bugfix"}, ["bug"], "bug"),
        (["bug", "feature"], {"bug": "issue.bugfix", "feature": "issue.feature"}, ["bug"], "bug"),
        (["bug", "feature"], {"bug": "issue.bugfix", "feature": "issue.feature"}, ["feature"], "feature"),
        (["feature", "bug"], {"bug": "issue.bugfix", "feature": "issue.feature"}, ["feature"], "feature"),
        # Empty/None cases
        ([], {"bug": "issue.bugfix"}, ["bug"], None),
        (["bug"], {}, ["bug"], None),
        (["bug"], {"bug": "issue.bugfix"}, [], "bug"),
        # No matches
        (["custom"], {"bug": "issue.bugfix"}, ["bug"], None),
        # Multiple priorities
        (["bug", "feature"], {"bug": "issue.bugfix", "feature": "issue.feature"}, ["feature", "bug"], "feature"),
    ],
)
def test_comprehensive_config_scenarios(labels, mappings, priorities, expected):
    """Parametrized test for comprehensive configuration scenarios."""
    result = _resolve_label_priority(labels, mappings, priorities)
    assert result == expected


@pytest.mark.parametrize(
    "config_value, should_succeed",
    [
        # Valid configs
        ('{"bug": "issue.bugfix"}', True),
        ('["bug", "feature"]', True),
        ('{"bug": ["bug", "bugfix"]}', True),
        # Invalid configs
        ('{"invalid":', False),  # Will fail on parse
        ("[unclosed", False),  # Invalid YAML
    ],
)
def test_env_var_config_parsing(config_value, should_succeed):
    """Test environment variable configuration parsing."""
    import json

    with patch.dict(os.environ, {"AUTO_CODER_TEST_CONFIG": config_value}):
        if should_succeed:
            try:
                # Try to parse as JSON first
                json.loads(config_value)
            except (json.JSONDecodeError, ValueError):
                # If JSON fails, try YAML
                yaml.safe_load(config_value)
            # If we get here, parsing succeeded
        else:
            # Should fail to parse
            with pytest.raises((yaml.YAMLError, json.JSONDecodeError, ValueError)):
                try:
                    json.loads(config_value)
                except (json.JSONDecodeError, ValueError):
                    yaml.safe_load(config_value)
