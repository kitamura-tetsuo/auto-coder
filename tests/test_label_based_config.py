"""Tests for label-based configuration functionality."""

import json
import os
from unittest.mock import patch

import pytest

from src.auto_coder.automation_config import AutomationConfig


class TestLabelBasedConfig:
    """Test label-based configuration functionality."""

    def test_label_prompt_mappings_config_default(self):
        """Test default label-to-prompt mappings configuration."""
        config = AutomationConfig(env_override=False)

        # Verify default mappings exist and are correct
        assert "breaking-change" in config.label_prompt_mappings
        assert config.label_prompt_mappings["breaking-change"] == "issue.breaking_change"
        assert "bug" in config.label_prompt_mappings
        assert config.label_prompt_mappings["bug"] == "issue.bug"
        assert "urgent" in config.label_prompt_mappings
        assert config.label_prompt_mappings["urgent"] == "issue.urgent"

    def test_label_priorities_config_default(self):
        """Test default label priorities configuration."""
        config = AutomationConfig(env_override=False)

        # Verify default priorities exist and breaking-change is first
        assert "breaking-change" in config.label_priorities
        assert "bug" in config.label_priorities
        assert "urgent" in config.label_priorities
        assert "enhancement" in config.label_priorities
        assert "documentation" in config.label_priorities

        # Breaking-change should have higher priority than urgent
        assert config.label_priorities.index("breaking-change") < config.label_priorities.index("urgent")
        # Urgent should have higher priority than bug
        assert config.label_priorities.index("urgent") < config.label_priorities.index("bug")

    def test_pr_label_mappings_config(self):
        """Test PR-specific label mappings configuration."""
        config = AutomationConfig(env_override=False)

        # Verify PR mappings exist
        assert "breaking-change" in config.pr_label_prompt_mappings
        assert "bug" in config.pr_label_prompt_mappings
        assert config.pr_label_prompt_mappings["breaking-change"] == "pr.breaking_change"
        assert config.pr_label_prompt_mappings["bug"] == "pr.bug"

    def test_pr_label_priorities_config(self):
        """Test PR-specific label priorities configuration."""
        config = AutomationConfig(env_override=False)

        # Verify PR priorities exist
        assert "urgent" in config.PR_LABEL_PRIORITIES
        assert "breaking-change" in config.PR_LABEL_PRIORITIES
        assert "bug" in config.PR_LABEL_PRIORITIES

        # Urgent should have highest priority
        assert config.PR_LABEL_PRIORITIES[0] == "urgent"

    def test_label_prompt_mappings_config_with_custom_values(self):
        """Test label-to-prompt mappings with custom values."""
        custom_mappings = {
            "custom-bug": "issue.custom_bug",
            "custom-feature": "issue.custom_feature",
        }

        config = AutomationConfig(
            env_override=False,
            custom_label_mappings=custom_mappings,
            replace_mappings=True,
        )

        # Verify custom mappings replace defaults
        assert config.label_prompt_mappings == custom_mappings

    def test_label_priorities_config_with_custom_values(self):
        """Test label priorities with custom values."""
        custom_priorities = ["custom-label-1", "custom-label-2", "custom-label-3"]

        config = AutomationConfig(
            env_override=False,
            custom_priorities=custom_priorities,
        )

        # Verify custom priorities are used
        assert config.label_priorities == custom_priorities

    def test_label_config_validation_valid(self):
        """Test that valid label configurations pass validation."""
        config = AutomationConfig(env_override=False)

        # Should not raise any exception
        config.validate_pr_label_config()

    def test_label_config_validation_invalid_max_count(self):
        """Test that invalid max count raises ValueError."""
        config = AutomationConfig(env_override=False)
        config.PR_LABEL_MAX_COUNT = 15  # Too high (max is 10)

        with pytest.raises(ValueError, match="PR_LABEL_MAX_COUNT must be between 0 and 10"):
            config.validate_pr_label_config()

    def test_label_config_validation_negative_max_count(self):
        """Test that negative max count raises ValueError."""
        config = AutomationConfig(env_override=False)
        config.PR_LABEL_MAX_COUNT = -1

        with pytest.raises(ValueError, match="PR_LABEL_MAX_COUNT must be between 0 and 10"):
            config.validate_pr_label_config()

    def test_label_config_validation_zero_max_count(self):
        """Test that zero max count is valid (no labels copied)."""
        config = AutomationConfig(env_override=False)
        config.PR_LABEL_MAX_COUNT = 0

        # Should not raise
        config.validate_pr_label_config()

    def test_label_config_validation_max_count_boundary(self):
        """Test boundary values for max count."""
        config = AutomationConfig(env_override=False)

        # Max valid value (10) should work
        config.PR_LABEL_MAX_COUNT = 10
        config.validate_pr_label_config()

        # Min valid value (0) should work
        config.PR_LABEL_MAX_COUNT = 0
        config.validate_pr_label_config()


class TestLabelEnvironmentConfig:
    """Test environment variable configuration for label-based prompts."""

    def test_auto_coder_label_prompt_mappings_env_var(self):
        """Test LABEL_PROMPT_MAPPINGS environment variable."""
        test_mappings = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "urgent": "issue.urgent",
        }

        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": json.dumps(test_mappings)}):
            config = AutomationConfig(env_override=True)

            # Verify mappings were loaded from environment (merged with defaults)
            # Environment variables should override/add to defaults, not replace them
            assert config.label_prompt_mappings["bug"] == "issue.bugfix"
            assert config.label_prompt_mappings["feature"] == "issue.feature"
            assert config.label_prompt_mappings["urgent"] == "issue.urgent"
            # Other default mappings should still exist
            assert "breaking-change" in config.label_prompt_mappings
            assert "enhancement" in config.label_prompt_mappings
            assert "documentation" in config.label_prompt_mappings

    def test_auto_coder_label_priorities_env_var(self):
        """Test LABEL_PRIORITIES environment variable."""
        test_priorities = ["bug", "feature", "urgent", "documentation"]

        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PRIORITIES": json.dumps(test_priorities)}):
            config = AutomationConfig(env_override=True)

            # Verify priorities were loaded from environment
            assert config.label_priorities == test_priorities

    def test_auto_coder_pr_label_mappings_env_var(self):
        """Test PR_LABEL_MAPPINGS environment variable."""
        test_mappings = {
            "bug": ["bug", "bugfix"],
            "feature": ["feature", "enhancement"],
        }

        with patch.dict(os.environ, {"AUTO_CODER_PR_LABEL_MAPPINGS": json.dumps(test_mappings)}):
            config = AutomationConfig(env_override=True)

            # Verify PR mappings were loaded from environment (merged with defaults)
            # Environment variables should override/add to defaults, not replace them
            assert config.PR_LABEL_MAPPINGS["bug"] == ["bug", "bugfix"]
            assert config.PR_LABEL_MAPPINGS["feature"] == ["feature", "enhancement"]
            # Other default mappings should still exist
            assert "urgent" in config.PR_LABEL_MAPPINGS
            assert "breaking-change" in config.PR_LABEL_MAPPINGS
            assert "documentation" in config.PR_LABEL_MAPPINGS

    def test_auto_coder_pr_label_priorities_env_var(self):
        """Test PR_LABEL_PRIORITIES environment variable."""
        test_priorities = ["urgent", "bug", "feature"]

        with patch.dict(os.environ, {"AUTO_CODER_PR_LABEL_PRIORITIES": json.dumps(test_priorities)}):
            config = AutomationConfig(env_override=True)

            # Verify PR priorities were loaded from environment
            assert config.PR_LABEL_PRIORITIES == test_priorities

    def test_env_var_malformed_json(self):
        """Test handling of malformed JSON in environment variables."""
        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": "not valid json"}):
            # Should not raise during initialization, but may log warning
            config = AutomationConfig(env_override=True)
            # Should fall back to defaults
            assert isinstance(config.label_prompt_mappings, dict)

    def test_env_var_empty_json(self):
        """Test handling of empty JSON in environment variables."""
        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": "{}"}):
            config = AutomationConfig(env_override=True)
            # Empty env var should merge with defaults (no changes)
            # All default mappings should still exist
            assert len(config.label_prompt_mappings) > 0
            assert "bug" in config.label_prompt_mappings
            assert "breaking-change" in config.label_prompt_mappings

    def test_env_var_missing_variables(self):
        """Test that missing environment variables use defaults."""
        with patch.dict(os.environ, {}, clear=True):
            config = AutomationConfig(env_override=True)

            # Should have default mappings
            assert len(config.label_prompt_mappings) > 0
            assert "bug" in config.label_prompt_mappings

            # Should have default priorities
            assert len(config.label_priorities) > 0
            assert "breaking-change" in config.label_priorities

    def test_env_var_override_with_custom_config(self):
        """Test that environment variables override custom config."""
        custom_mappings = {"custom": "issue.custom"}
        test_mappings = {"bug": "issue.bugfix"}

        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": json.dumps(test_mappings)}):
            config = AutomationConfig(
                env_override=True,
                custom_label_mappings=custom_mappings,
            )

            # Environment should override custom mappings (merged)
            assert config.label_prompt_mappings["bug"] == "issue.bugfix"
            assert config.label_prompt_mappings["custom"] == "issue.custom"
            # Other default mappings should still exist
            assert "breaking-change" in config.label_prompt_mappings
            assert "urgent" in config.label_prompt_mappings

    def test_env_var_empty_string(self):
        """Test handling of empty string in environment variables."""
        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": ""}):
            config = AutomationConfig(env_override=True)
            # Should fall back to defaults or handle gracefully
            assert isinstance(config.label_prompt_mappings, dict)

    def test_env_var_with_special_characters(self):
        """Test environment variables with special characters."""
        test_mappings = {
            "bug-fix": "issue.bugfix",
            "type:feature": "issue.feature",
        }

        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": json.dumps(test_mappings)}):
            config = AutomationConfig(env_override=True)
            # Special character labels should be added to defaults
            assert config.label_prompt_mappings["bug-fix"] == "issue.bugfix"
            assert config.label_prompt_mappings["type:feature"] == "issue.feature"
            # Default mappings should still exist
            assert "bug" in config.label_prompt_mappings
            assert "breaking-change" in config.label_prompt_mappings

    def test_env_var_with_nested_structures(self):
        """Test that nested structures are preserved and merged with defaults."""
        test_mappings = {
            "bug": {
                "prompt": "issue.bugfix",
                "priority": 1,
            },
        }

        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": json.dumps(test_mappings)}):
            config = AutomationConfig(env_override=True)
            # Nested structure should be preserved for "bug"
            assert config.label_prompt_mappings["bug"] == {
                "prompt": "issue.bugfix",
                "priority": 1,
            }
            # Default mappings should still exist
            assert "breaking-change" in config.label_prompt_mappings
            assert "urgent" in config.label_prompt_mappings
            assert "enhancement" in config.label_prompt_mappings

    def test_env_var_priority_preserves_order(self):
        """Test that priority order is preserved from environment."""
        test_priorities = ["z-label", "a-label", "m-label"]

        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PRIORITIES": json.dumps(test_priorities)}):
            config = AutomationConfig(env_override=True)
            # Should preserve the order
            assert config.label_priorities == test_priorities

    def test_env_var_multiple_variables_together(self):
        """Test that multiple environment variables work together and merge with defaults."""
        test_mappings = {"bug": "issue.bugfix"}
        test_priorities = ["bug", "feature"]
        test_pr_mappings = {"bug": ["bug", "bugfix"]}
        test_pr_priorities = ["bug", "feature"]

        env_vars = {
            "AUTO_CODER_LABEL_PROMPT_MAPPINGS": json.dumps(test_mappings),
            "AUTO_CODER_LABEL_PRIORITIES": json.dumps(test_priorities),
            "AUTO_CODER_PR_LABEL_MAPPINGS": json.dumps(test_pr_mappings),
            "AUTO_CODER_PR_LABEL_PRIORITIES": json.dumps(test_pr_priorities),
        }

        with patch.dict(os.environ, env_vars):
            config = AutomationConfig(env_override=True)

            # Label prompt mappings should be merged
            assert config.label_prompt_mappings["bug"] == "issue.bugfix"
            assert "breaking-change" in config.label_prompt_mappings
            assert "urgent" in config.label_prompt_mappings
            # Priorities should be replaced (as they're lists, not merged)
            assert config.label_priorities == test_priorities
            # PR label mappings should be merged
            assert config.PR_LABEL_MAPPINGS["bug"] == ["bug", "bugfix"]
            assert "urgent" in config.PR_LABEL_MAPPINGS
            # PR priorities should be replaced
            assert config.PR_LABEL_PRIORITIES == test_pr_priorities

    def test_env_var_disabled_with_env_override_false(self):
        """Test that environment variables are ignored when env_override=False."""
        test_mappings = {"bug": "issue.bugfix"}

        with patch.dict(os.environ, {"AUTO_CODER_LABEL_PROMPT_MAPPINGS": json.dumps(test_mappings)}):
            config = AutomationConfig(env_override=False)

            # Should use defaults, not environment
            assert config.label_prompt_mappings != test_mappings
            assert "bug" in config.label_prompt_mappings

    def test_config_with_disabled_labels(self):
        """Test configuration with labels disabled."""
        config = AutomationConfig(env_override=False)
        config.DISABLE_LABELS = True

        # Label operations should be skipped when disabled
        assert config.DISABLE_LABELS is True

    def test_config_with_check_labels_enabled(self):
        """Test configuration with label checking enabled."""
        config = AutomationConfig(env_override=False)

        # Label checking should be enabled by default
        assert config.CHECK_LABELS is True

    def test_config_with_pr_label_copying_enabled(self):
        """Test configuration with PR label copying enabled."""
        config = AutomationConfig(env_override=False)

        # PR label copying should be enabled by default
        assert config.PR_LABEL_COPYING_ENABLED is True

    def test_config_default_max_pr_label_count(self):
        """Test default maximum PR label count."""
        config = AutomationConfig(env_override=False)

        # Default should be 3
        assert config.PR_LABEL_MAX_COUNT == 3

    def test_config_custom_max_pr_label_count(self):
        """Test custom maximum PR label count."""
        config = AutomationConfig(env_override=False)
        config.PR_LABEL_MAX_COUNT = 5

        assert config.PR_LABEL_MAX_COUNT == 5

    def test_config_merge_label_priorities_with_mappings(self):
        """Test that priorities and mappings work together correctly."""
        config = AutomationConfig(env_override=False)

        # All priorities should have corresponding mappings
        for priority_label in config.label_priorities:
            if priority_label in config.label_prompt_mappings:
                # Priority label should map to a valid prompt
                prompt_key = config.label_prompt_mappings[priority_label]
                assert prompt_key.startswith("issue.") or prompt_key.startswith("pr.")

    def test_config_pr_label_max_count_boundary_conditions(self):
        """Test boundary conditions for PR label max count."""
        config = AutomationConfig(env_override=False)

        # Test valid boundary values
        for count in [0, 1, 5, 10]:
            config.PR_LABEL_MAX_COUNT = count
            config.validate_pr_label_config()  # Should not raise
            assert config.PR_LABEL_MAX_COUNT == count

        # Test invalid values
        for count in [-1, 11, 100]:
            config.PR_LABEL_MAX_COUNT = count
            with pytest.raises(ValueError):
                config.validate_pr_label_config()

    def test_config_label_priorities_no_duplicates(self):
        """Test that default priorities don't have duplicates."""
        config = AutomationConfig(env_override=False)

        # Check for duplicates
        priorities = config.label_priorities
        assert len(priorities) == len(set(priorities)), "Priorities should not have duplicates"

    def test_config_pr_label_mappings_values_are_lists(self):
        """Test that PR label mapping values are lists."""
        config = AutomationConfig(env_override=False)

        for semantic_label, label_list in config.PR_LABEL_MAPPINGS.items():
            assert isinstance(label_list, list), f"PR_LABEL_MAPPINGS[{semantic_label}] should be a list"
            assert len(label_list) > 0, f"PR_LABEL_MAPPINGS[{semantic_label}] should not be empty"

    def test_config_label_prompt_mappings_values_are_strings(self):
        """Test that label prompt mapping values are strings."""
        config = AutomationConfig(env_override=False)

        for label, prompt_key in config.label_prompt_mappings.items():
            assert isinstance(prompt_key, str), f"label_prompt_mappings[{label}] should be a string"
            assert len(prompt_key) > 0, f"label_prompt_mappings[{label}] should not be empty"

    def test_config_custom_mapping_overrides_default(self):
        """Test that custom mappings properly override defaults."""
        custom = {"my-custom-label": "issue.custom"}
        config = AutomationConfig(
            env_override=False,
            custom_label_mappings=custom,
            replace_mappings=True,
        )

        # Custom mappings should replace defaults
        assert config.label_prompt_mappings == custom
        assert "my-custom-label" in config.label_prompt_mappings

    def test_config_custom_priorities_replaces_default(self):
        """Test that custom priorities replace defaults."""
        custom = ["my-priority-1", "my-priority-2"]
        config = AutomationConfig(
            env_override=False,
            custom_priorities=custom,
        )

        assert config.label_priorities == custom
        assert len(config.label_priorities) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
