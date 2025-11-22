"""Tests for automation_config configuration validation."""

import pytest

from auto_coder.automation_config import AutomationConfig


def test_label_prompt_mappings_default_config():
    """Test that default label prompt mappings are correctly configured."""
    config = AutomationConfig()

    # Check that default mappings exist for all semantic label types
    assert "breaking-change" in config.label_prompt_mappings
    assert "urgent" in config.label_prompt_mappings
    assert "bug" in config.label_prompt_mappings
    assert "enhancement" in config.label_prompt_mappings
    assert "documentation" in config.label_prompt_mappings

    # Check that mappings point to correct prompt templates
    assert config.label_prompt_mappings["breaking-change"] == "issue.breaking_change"
    assert config.label_prompt_mappings["urgent"] == "issue.urgent"
    assert config.label_prompt_mappings["bug"] == "issue.bug"
    assert config.label_prompt_mappings["enhancement"] == "issue.enhancement"
    assert config.label_prompt_mappings["documentation"] == "issue.documentation"


def test_label_prompt_mappings_with_aliases():
    """Test that label mappings include common aliases."""
    config = AutomationConfig()

    # Check breaking-change aliases
    breaking_change_aliases = ["breaking", "api-change", "deprecation", "version-major"]
    for alias in breaking_change_aliases:
        assert alias in config.label_prompt_mappings
        assert config.label_prompt_mappings[alias] == "issue.breaking_change"

    # Check bug aliases
    bug_aliases = ["bugfix", "defect", "error", "fix"]
    for alias in bug_aliases:
        assert alias in config.label_prompt_mappings
        assert config.label_prompt_mappings[alias] == "issue.bug"

    # Check enhancement aliases
    enhancement_aliases = ["feature", "improvement", "new-feature"]
    for alias in enhancement_aliases:
        assert alias in config.label_prompt_mappings
        assert config.label_prompt_mappings[alias] == "issue.enhancement"

    # Check documentation aliases
    documentation_aliases = ["docs", "doc"]
    for alias in documentation_aliases:
        assert alias in config.label_prompt_mappings
        assert config.label_prompt_mappings[alias] == "issue.documentation"

    # Check urgent aliases
    urgent_aliases = ["high-priority", "critical", "blocker"]
    for alias in urgent_aliases:
        assert alias in config.label_prompt_mappings
        assert config.label_prompt_mappings[alias] == "issue.urgent"


def test_label_priorities_order():
    """Test that label priorities are correctly ordered."""
    config = AutomationConfig()

    # Breaking-change should be highest priority
    assert config.label_priorities.index("breaking-change") < config.label_priorities.index("urgent")
    assert config.label_priorities.index("breaking-change") < config.label_priorities.index("bug")
    assert config.label_priorities.index("breaking-change") < config.label_priorities.index("enhancement")
    assert config.label_priorities.index("breaking-change") < config.label_priorities.index("documentation")

    # Urgent should be higher than bug, enhancement, documentation
    assert config.label_priorities.index("urgent") < config.label_priorities.index("bug")
    assert config.label_priorities.index("urgent") < config.label_priorities.index("enhancement")
    assert config.label_priorities.index("urgent") < config.label_priorities.index("documentation")

    # Bug should be higher than enhancement, documentation
    assert config.label_priorities.index("bug") < config.label_priorities.index("enhancement")
    assert config.label_priorities.index("bug") < config.label_priorities.index("documentation")

    # Enhancement should be higher than documentation
    assert config.label_priorities.index("enhancement") < config.label_priorities.index("documentation")


def test_label_priorities_include_all_mappings():
    """Test that all label mappings are included in priorities."""
    config = AutomationConfig()

    # All mapped labels should have priorities
    for label in config.label_prompt_mappings.keys():
        assert label in config.label_priorities


def test_config_customization():
    """Test that configuration can be customized."""
    # Create custom config with modified mappings
    config = AutomationConfig()

    # Modify label mappings
    custom_mappings = {"custom-label": "issue.custom"}
    config.label_prompt_mappings.update(custom_mappings)

    assert "custom-label" in config.label_prompt_mappings
    assert config.label_prompt_mappings["custom-label"] == "issue.custom"

    # Modify priorities
    config.label_priorities.insert(0, "custom-label")
    assert config.label_priorities[0] == "custom-label"


def test_pr_label_config_validation():
    """Test PR label configuration validation."""
    config = AutomationConfig()

    # Valid configuration should not raise
    config.validate_pr_label_config()

    # Invalid max count should raise
    config.PR_LABEL_MAX_COUNT = -1
    with pytest.raises(ValueError, match="PR_LABEL_MAX_COUNT must be between 0 and 10"):
        config.validate_pr_label_config()

    config.PR_LABEL_MAX_COUNT = 11
    with pytest.raises(ValueError, match="PR_LABEL_MAX_COUNT must be between 0 and 10"):
        config.validate_pr_label_config()


def test_pr_label_copying_defaults():
    """Test PR label copying defaults."""
    config = AutomationConfig()

    # Default settings
    assert config.PR_LABEL_COPYING_ENABLED is True
    assert config.PR_LABEL_MAX_COUNT == 3
    assert "breaking-change" in config.PR_LABEL_PRIORITIES
    assert "urgent" in config.PR_LABEL_PRIORITIES
    assert "bug" in config.PR_LABEL_PRIORITIES
    assert "enhancement" in config.PR_LABEL_PRIORITIES
    assert "documentation" in config.PR_LABEL_PRIORITIES


def test_get_reports_dir():
    """Test reports directory generation."""
    config = AutomationConfig()

    reports_dir = config.get_reports_dir("owner/repo")
    assert "owner_repo" in reports_dir
    assert ".auto-coder" in reports_dir
    assert reports_dir.endswith("owner_repo")


def test_default_config_values():
    """Test other default configuration values."""
    config = AutomationConfig()

    # Test standard config values
    assert config.REPORTS_DIR == "reports"
    assert config.TEST_SCRIPT_PATH == "scripts/test.sh"
    assert config.MAX_PR_DIFF_SIZE == 2000
    assert config.MAX_PROMPT_SIZE == 1000
    assert config.MAX_RESPONSE_SIZE == 200
    assert config.MAX_FIX_ATTEMPTS == 30
    assert config.MAIN_BRANCH == "main"
    assert config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL is True
    assert config.IGNORE_DEPENDABOT_PRS is False
    assert config.FORCE_CLEAN_BEFORE_CHECKOUT is False
    assert config.DISABLE_LABELS is False
    assert config.CHECK_LABELS is True
    assert config.CHECK_DEPENDENCIES is True
    assert config.SEARCH_GITHUB_ACTIONS_HISTORY is True
    assert config.ENABLE_ACTIONS_HISTORY_FALLBACK is True
    assert config.MERGE_METHOD == "--squash"
    assert config.MERGE_AUTO is True
