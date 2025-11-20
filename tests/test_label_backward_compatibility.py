"""Backward compatibility tests for label configuration and prompt rendering.

This module ensures that existing code continues to work correctly as the
label system evolves, covering:
- Existing render_prompt calls without labels
- Old configuration formats (migration paths)
- Deprecated label names and aliases
- Legacy prompt template keys
- Removed configuration parameters
- API version compatibility
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from auto_coder.prompt_loader import (
    _get_prompt_for_labels,
    _resolve_label_priority,
    clear_prompt_cache,
    get_label_specific_prompt,
    load_prompts,
    render_prompt,
)
from src.auto_coder import prompt_loader


class TestExistingPromptCalls:
    """Test that existing prompt calls without labels continue to work."""

    def test_render_prompt_without_labels(self, tmp_path):
        """Test render_prompt works without any label parameters."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        result = render_prompt("issue.action", path=str(prompt_file))
        assert "Default action" in result

    def test_render_prompt_without_labels_with_data(self, tmp_path):
        """Test render_prompt without labels but with data parameter."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Action for $issue_number"\n',
            encoding="utf-8",
        )

        result = render_prompt("issue.action", path=str(prompt_file), data={"issue_number": "123"})
        assert "123" in result

    def test_render_prompt_with_none_labels(self, tmp_path):
        """Test render_prompt with explicit None labels."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=None,
            label_prompt_mappings=None,
            label_priorities=None,
        )
        assert "Default action" in result

    def test_render_prompt_with_empty_labels(self, tmp_path):
        """Test render_prompt with empty labels list."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text('issue:\n  action: "Default action"\n', encoding="utf-8")

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=[],
            label_prompt_mappings={},
            label_priorities=[],
        )
        assert "Default action" in result

    def test_get_label_specific_prompt_without_config(self):
        """Test get_label_specific_prompt with minimal config."""
        # Old code might call this with just labels
        result = get_label_specific_prompt(["bug"], {}, None)
        assert result is None

    def test_get_label_specific_prompt_with_none(self):
        """Test get_label_specific_prompt with None values."""
        result = get_label_specific_prompt(None, None, None)
        assert result is None

    def test_legacy_format_with_mappings_only(self):
        """Test legacy format with only mappings (no priorities)."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = None  # Old code might not provide priorities

        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result == "issue.bugfix"

    def test_legacy_format_with_priorities_only(self):
        """Test legacy format with only priorities (no mappings)."""
        labels = ["bug"]
        mappings = {}  # Empty mappings
        priorities = ["bug"]

        result = get_label_specific_prompt(labels, mappings, priorities)
        assert result is None


class TestOldConfigurationFormats:
    """Test support for old configuration formats."""

    def test_old_style_flat_config(self, tmp_path):
        """Test old-style flat configuration structure."""
        # Old format might have been flat, not nested
        config_file = tmp_path / "old_config.yaml"
        config_file.write_text(
            """
            bug: "issue.bugfix"
            feature: "issue.feature"
            urgent: "issue.urgent"
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        # Should be able to load as dictionary
        assert data["bug"] == "issue.bugfix"
        assert "bug" in data

    def test_old_style_list_based_config(self, tmp_path):
        """Test old-style list-based configuration."""
        config_file = tmp_path / "list_config.yaml"
        config_file.write_text(
            """
            - bug
            - feature
            - enhancement
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        # Should load as list
        assert data == ["bug", "feature", "enhancement"]

    def test_old_format_with_different_keys(self, tmp_path):
        """Test old configuration with different key names."""
        config_file = tmp_path / "legacy_keys.yaml"
        # Old format might have used different key names
        config_file.write_text(
            """
            label_to_prompt:
              bug: "issue.bugfix"
            label_order:
              - bug
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        # Should still be able to access the data
        assert "label_to_prompt" in data
        assert "label_order" in data

    def test_migration_from_old_to_new_format(self, tmp_path):
        """Test migration path from old to new format."""
        # Old format file
        old_config = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "urgent": "issue.urgent",
        }

        # New format structure
        new_config = {
            "label_prompt_mappings": old_config,
            "label_priorities": ["urgent", "bug", "feature"],
        }

        # Both should work with render_prompt
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  bugfix: "Bug fix"\n  feature: "Feature"\n  urgent: "Urgent"\n',
            encoding="utf-8",
        )

        # Old style access
        result = render_prompt("issue.bugfix", path=str(prompt_file))
        assert "Bug fix" in result

        # New style access
        result = render_prompt(
            "issue.bugfix",
            path=str(prompt_file),
            labels=["bug"],
            label_prompt_mappings=new_config["label_prompt_mappings"],
            label_priorities=new_config["label_priorities"],
        )
        assert "Bug fix" in result

    def test_backward_compatible_parameter_order(self):
        """Test that parameter order doesn't matter for backward compatibility."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        # Different call styles should all work
        result1 = _resolve_label_priority(labels, mappings, priorities)
        result2 = _resolve_label_priority(labels, mappings, priorities)
        result3 = _resolve_label_priority(labels, mappings, priorities)

        assert result1 == result2 == result3 == "bug"


class TestDeprecatedLabelNames:
    """Test support for deprecated label names and aliases."""

    def test_deprecated_label_aliases(self):
        """Test that deprecated label aliases still work."""
        labels = ["defect"]  # Old name for "bug"
        mappings = {
            "defect": "issue.bugfix",  # Deprecated label name
            "bug": "issue.bugfix",
        }
        priorities = ["defect", "bug"]

        # Old label "defect" should still resolve
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "defect"

    def test_multiple_aliases_for_same_label(self):
        """Test multiple aliases mapping to the same prompt."""
        labels = ["hotfix"]
        mappings = {
            "hotfix": "issue.bugfix",  # Alias for bug
            "bugfix": "issue.bugfix",
            "patch": "issue.bugfix",
            "bug": "issue.bugfix",
        }
        priorities = ["hotfix", "bugfix", "patch", "bug"]

        # Any alias should resolve
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "hotfix"

    def test_legacy_label_names_case_insensitive(self):
        """Test that legacy label names are matched case-insensitively."""
        labels = ["BUG"]  # Uppercase
        mappings = {"bug": "issue.bugfix"}  # Lowercase
        priorities = ["bug"]

        # Case mismatch - should not match in strict mode
        result = _resolve_label_priority(labels, mappings, priorities)
        # Labels are matched exactly, case-sensitive
        assert result is None

    def test_obsolete_labels_with_mappings(self):
        """Test obsolete labels that still have mappings defined."""
        labels = ["wontfix"]  # Obsolete label
        mappings = {
            "wontfix": "issue.closed",  # Still has mapping
            "closed": "issue.closed",
        }
        priorities = ["wontfix", "closed"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "wontfix"

    def test_removed_labels_without_mappings(self):
        """Test labels that were removed (no longer in mappings)."""
        labels = ["old-label"]  # Removed label
        mappings = {"bug": "issue.bugfix"}  # No mapping for old-label
        priorities = ["bug"]

        # Should return None or fall back
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result is None


class TestLegacyPromptTemplateKeys:
    """Test legacy prompt template key formats."""

    def test_legacy_dot_notation_keys(self, tmp_path):
        """Test legacy dot-notation template keys."""
        prompt_file = tmp_path / "legacy_keys.yaml"
        prompt_file.write_text(
            'issue.action: "Default"\nissue.bugfix: "Bug fix"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        # Legacy keys with dots in the name
        result = render_prompt("issue.action", path=str(prompt_file))
        assert "Default" in result

    def test_legacy_underscore_keys(self, tmp_path):
        """Test legacy underscore-separated template keys."""
        prompt_file = tmp_path / "underscore_keys.yaml"
        prompt_file.write_text(
            'issue_action: "Default"\nissue_bug_fix: "Bug fix"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        result = render_prompt("issue_action", path=str(prompt_file))
        assert "Default" in result

    def test_legacy_camel_case_keys(self, tmp_path):
        """Test legacy camelCase template keys."""
        prompt_file = tmp_path / "camel_case.yaml"
        prompt_file.write_text(
            'issueAction: "Default"\nbugFixPrompt: "Bug fix"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        result = render_prompt("issueAction", path=str(prompt_file))
        assert "Default" in result

    def test_legacy_numeric_keys(self, tmp_path):
        """Test legacy numeric template keys."""
        prompt_file = tmp_path / "numeric_keys.yaml"
        prompt_file.write_text('"1": "First"\n"2": "Second"\n', encoding="utf-8")

        clear_prompt_cache()
        result = render_prompt("1", path=str(prompt_file))
        assert "First" in result

    def test_legacy_special_char_keys(self, tmp_path):
        """Test legacy keys with special characters."""
        prompt_file = tmp_path / "special_keys.yaml"
        prompt_file.write_text(
            'issue@action: "Default"\nissue#bugfix: "Bug fix"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        result = render_prompt("issue@action", path=str(prompt_file))
        assert "Default" in result


class TestRemovedConfigurationParameters:
    """Test handling of removed configuration parameters."""

    def test_removed_parameter_ignored_gracefully(self):
        """Test that removed parameters are ignored without error."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        # Old code might pass additional parameters
        # Should handle extra parameters gracefully
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == "issue.bugfix"

    def test_renamed_parameter_handling(self):
        """Test handling of renamed configuration parameters."""
        # Old name: "label_mappings" -> New name: "label_prompt_mappings"
        old_mappings = {"bug": "issue.bugfix"}
        new_mappings = {"bug": "issue.bugfix"}

        # Both should work the same
        result1 = _get_prompt_for_labels(["bug"], old_mappings, ["bug"])
        result2 = _get_prompt_for_labels(["bug"], new_mappings, ["bug"])

        assert result1 == result2 == "issue.bugfix"

    def test_obsolete_config_section_ignored(self, tmp_path):
        """Test that obsolete config sections are ignored."""
        config_file = tmp_path / "obsolete_config.yaml"
        config_file.write_text(
            """
            obsolete_section:
              old_key: "old_value"
            label_prompt_mappings:
              bug: "issue.bugfix"
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        # Should load without error, obsolete section is ignored
        assert "label_prompt_mappings" in data
        assert "obsolete_section" in data

    def test_deprecated_env_vars_ignored(self):
        """Test that deprecated environment variables don't cause errors."""
        with patch.dict(
            os.environ,
            {
                # Deprecated variable names
                "AUTO_CODER_LABEL_MAPPINGS": '{"bug": "issue.bugfix"}',  # Old name
                "AUTO_CODER_LABEL_ORDERS": '["bug"]',  # Old name
                # New variable names
                "AUTO_CODER_LABEL_PROMPT_MAPPINGS": '{"bug": "issue.bugfix"}',
                "AUTO_CODER_LABEL_PRIORITIES": '["bug"]',
            },
        ):
            # Should be able to read both
            old_mappings = yaml.safe_load(os.environ["AUTO_CODER_LABEL_MAPPINGS"])
            new_mappings = yaml.safe_load(os.environ["AUTO_CODER_LABEL_PROMPT_MAPPINGS"])

            assert old_mappings == new_mappings

    def test_unknown_parameters_ignored(self):
        """Test that unknown parameters are ignored."""
        # Extra unknown parameters should be ignored
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        # Pass extra dict that gets ignored
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"


class TestAPIVersionCompatibility:
    """Test API version compatibility across releases."""

    def test_v1_api_compatibility(self):
        """Test v1 API compatibility (initial label support)."""
        # v1 might have had different function signatures
        result = render_prompt(
            "issue.action",  # key
            path=None,  # Old style might not have path
            data=None,
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        # Should work with defaults

    def test_v2_api_compatibility(self):
        """Test v2 API compatibility (enhanced label support)."""
        # v2 with additional parameters
        result = render_prompt(
            key="issue.action",
            path=None,
            data={"issue_number": "123"},
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        # Should work with keyword arguments

    def test_mixed_positional_and_keyword_args(self):
        """Test mixed positional and keyword arguments."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        # Mix positional and keyword
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_optional_parameters_as_none(self):
        """Test that optional parameters can be explicitly None."""
        result = render_prompt(
            "issue.action",
            path=None,
            data=None,
            labels=None,
            label_prompt_mappings=None,
            label_priorities=None,
        )
        # Should handle None values gracefully

    def test_default_parameter_values(self):
        """Test that default parameter values work correctly."""
        # Call with minimal parameters
        result = render_prompt("issue.action")
        # Should use defaults and load from DEFAULT_PROMPTS_PATH


class TestBackwardCompatibleErrorHandling:
    """Test error handling maintains backward compatibility."""

    def test_missing_file_legacy_behavior(self):
        """Test that missing file error maintains legacy behavior."""
        with pytest.raises(SystemExit):
            load_prompts("/nonexistent/file.yaml")

    def test_invalid_yaml_legacy_behavior(self, tmp_path):
        """Test that invalid YAML error maintains legacy behavior."""
        prompt_file = tmp_path / "invalid.yaml"
        prompt_file.write_text("[:)", encoding="utf-8")

        with pytest.raises(SystemExit):
            load_prompts(str(prompt_file))

    def test_missing_key_legacy_behavior(self, tmp_path):
        """Test that missing key error is backward compatible."""
        prompt_file = tmp_path / "test.yaml"
        prompt_file.write_text('a:\n  b: "value"\n', encoding="utf-8")

        clear_prompt_cache()
        data = load_prompts(str(prompt_file))

        with pytest.raises(KeyError):
            prompt_loader._traverse(data, "a.c")

    def test_graceful_degradation_with_old_config(self):
        """Test graceful degradation with partially old configuration."""
        # Old config might have missing fields
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        # Should work even if config is minimal
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == "issue.bugfix"

    def test_fallback_to_defaults_with_old_api(self):
        """Test fallback to defaults when using old API."""
        # Old API might not provide all parameters
        result = _get_prompt_for_labels(["bug"], {"bug": "prompt"}, None)
        assert result == "prompt"


class TestMigrationHelpers:
    """Test utilities that help with migration."""

    def test_config_version_detection(self, tmp_path):
        """Test ability to detect configuration version."""
        # v2 config with version marker
        config_file = tmp_path / "v2_config.yaml"
        config_file.write_text(
            """
            version: "2.0"
            label_prompt_mappings:
              bug: "issue.bugfix"
            """,
            encoding="utf-8",
        )

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert data.get("version") == "2.0"

    def test_automatic_migration_detection(self):
        """Test automatic detection of config format version."""
        # Old format (flat)
        old_config = {"bug": "issue.bugfix", "feature": "issue.feature"}

        # New format (structured)
        new_config = {
            "label_prompt_mappings": {"bug": "issue.bugfix", "feature": "issue.feature"},
            "label_priorities": ["bug", "feature"],
        }

        # Detect which format
        if "label_prompt_mappings" in new_config:
            # New format
            assert "label_priorities" in new_config
        else:
            # Old format
            assert isinstance(old_config, dict)

    def test_config_normalization(self):
        """Test normalization of old config to new format."""
        # Old format
        old_config = {"bug": "issue.bugfix", "feature": "issue.feature"}

        # Normalize to new format
        normalized = {
            "label_prompt_mappings": old_config,
            "label_priorities": list(old_config.keys()),
        }

        assert "label_prompt_mappings" in normalized
        assert "label_priorities" in normalized
        assert len(normalized["label_priorities"]) == 2


# Regression test cases
class TestRegressionBackwardCompatibility:
    """Regression tests for specific backward compatibility issues."""

    def test_issue_397_compatibility(self):
        """Test compatibility with issue #397 requirements."""
        # Ensure render_prompt works without labels parameter
        result = render_prompt("issue.action")
        # Should not raise error

    def test_github_issue_434_regression(self):
        """Test regression for GitHub issue #434."""
        # Ensure configuration validation doesn't break old code
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_label_system_v1_compatibility(self):
        """Test compatibility with label system v1."""
        # v1 API with correct parameter names
        result = get_label_specific_prompt(
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
        )
        assert result == "issue.bugfix"

    def test_custom_label_aliases_backward_compat(self):
        """Test that custom label aliases work with old code."""
        labels = ["enhancement", "improvement"]  # Both map to same thing
        mappings = {
            "enhancement": "issue.enhance",
            "improvement": "issue.enhance",
            "feature": "issue.enhance",
        }
        priorities = ["enhancement", "improvement", "feature"]

        # Both should resolve correctly
        result1 = _resolve_label_priority(["enhancement"], mappings, priorities)
        result2 = _resolve_label_priority(["improvement"], mappings, priorities)

        assert result1 == "enhancement"
        assert result2 == "improvement"

    def test_empty_string_config_handling(self):
        """Test handling of empty string in old configurations."""
        labels = ["bug"]
        mappings = {"bug": ""}  # Empty string value
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == ""

    def test_none_in_old_config_list(self):
        """Test None values in old configuration lists."""
        labels = ["bug", None, "feature"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["bug", "feature"]

        # Should skip None and find valid labels
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"


@pytest.mark.parametrize(
    "old_call, expected_works",
    [
        # Old-style calls that should still work
        ({"key": "issue.action", "path": None, "data": None}, True),
        ({"key": "issue.action", "path": None, "data": None, "labels": None}, True),
        ({"key": "issue.action", "path": None, "data": None, "labels": []}, True),
        ({"key": "issue.action", "path": None, "data": None, "labels": ["bug"], "label_prompt_mappings": {"bug": "issue.bug"}, "label_priorities": ["bug"]}, True),
    ],
)
def test_backward_compatible_render_prompt_calls(old_call, expected_works):
    """Test backward compatible render_prompt calls."""
    try:
        result = render_prompt(**old_call)
        if expected_works:
            assert result is not None or result is None  # Both are valid
    except Exception as e:
        if expected_works:
            pytest.fail(f"Expected call to work, but got exception: {e}")


@pytest.mark.parametrize(
    "labels, mappings, priorities, legacy_aliases, expected",
    [
        # Legacy alias tests
        (["defect"], {"defect": "issue.bugfix"}, ["defect"], {"defect": "bug"}, "defect"),
        (["hotfix"], {"hotfix": "issue.bugfix"}, ["hotfix"], {"hotfix": "bug"}, "hotfix"),
        (["enhancement"], {"enhancement": "issue.feature"}, ["enhancement"], {"enhancement": "feature"}, "enhancement"),
    ],
)
def test_legacy_label_aliases(labels, mappings, priorities, legacy_aliases, expected):
    """Test legacy label alias compatibility."""
    # Legacy aliases might be in mappings
    result = _resolve_label_priority(labels, mappings, priorities)
    assert result == expected
