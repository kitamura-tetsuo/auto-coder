"""Tests for prompt loader utilities."""

import pytest

from src.auto_coder import prompt_loader
from src.auto_coder.prompt_loader import DEFAULT_PROMPTS_PATH, _get_prompt_for_labels, _is_breaking_change_issue, _resolve_label_priority, clear_prompt_cache, get_label_specific_prompt, get_prompt_template, render_prompt


class TestLabelBasedPromptLoader:
    """Test label-based prompt selection functionality."""

    def test_label_priority_resolution(self):
        """Test label priority resolution with various combinations."""
        # Test single label
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

        # Test multiple labels with different priorities
        labels = ["bug", "feature", "enhancement"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["feature", "bug", "enhancement"]
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "feature"  # Highest priority

        # Test no applicable labels
        labels = ["documentation", "question"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result is None

        # Test empty label list
        result = _resolve_label_priority([], {"bug": "issue.bug"}, ["bug"])
        assert result is None

        # Test priority configuration validation
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = []  # Empty priorities
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"  # Should fallback to first applicable label

    def test_get_prompt_for_labels(self):
        """Test label-to-prompt mapping resolution."""
        # Test exact label matches
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == "issue.bugfix"

        # Test missing label mappings
        labels = ["custom-label"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

        # Test fallback behavior
        labels = ["documentation"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_render_prompt_with_labels(self):
        """Test enhanced render_prompt function with labels."""
        # Test label-based prompt selection
        # (Additional test cases covered in other tests below)
        pass

    def test_label_detection_patterns(self):
        """Test label detection patterns for breaking changes."""
        # Test breaking-change label variants
        assert _is_breaking_change_issue(["breaking-change"]) is True
        assert _is_breaking_change_issue(["breaking"]) is True
        assert _is_breaking_change_issue(["api-change"]) is True
        assert _is_breaking_change_issue(["deprecation"]) is True
        assert _is_breaking_change_issue(["version-major"]) is True

        # Test bug label family
        assert _is_breaking_change_issue(["bug"]) is False

        # Test documentation label family
        assert _is_breaking_change_issue(["documentation"]) is False

        # Test enhancement label family
        assert _is_breaking_change_issue(["enhancement"]) is False

        # Test case-insensitive matching
        assert _is_breaking_change_issue(["Breaking-Change"]) is True
        assert _is_breaking_change_issue(["BREAKING"]) is True

    def test_get_prompt_for_labels_with_priority_ordering(self):
        """Test that label priorities are respected in prompt selection."""
        labels = ["urgent", "bug", "enhancement"]
        mappings = {
            "urgent": "issue.urgent",
            "bug": "issue.bugfix",
            "enhancement": "issue.enhancement",
        }
        priorities = ["urgent", "bug", "enhancement"]
        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result == "issue.urgent"  # Highest priority

    def test_render_prompt_with_complex_label_config(self, tmp_path):
        """Test render_prompt with complex label configuration."""
        # Create a temporary prompt file
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "Global header"\n'
            "issue:\n"
            '  action: "Default issue action"\n'
            '  bugfix: "Bug fix prompt with $issue_number"\n'
            '  feature: "Feature prompt with $issue_number"\n'
            '  enhancement: "Enhancement prompt with $issue_number"\n'
            '  breaking_change: "Breaking change prompt - DELETE TESTS with $issue_number"\n'
            '  urgent: "Urgent prompt with $issue_number"\n'
            '  documentation: "Documentation prompt with $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = ["bug"]
        mappings = {
            "bug": "issue.bugfix",
            "feature": "issue.feature",
            "enhancement": "issue.enhancement",
            "breaking_change": "issue.breaking_change",
            "urgent": "issue.urgent",
            "documentation": "issue.documentation",
        }
        priorities = [
            "breaking-change",
            "urgent",
            "bug",
            "enhancement",
            "documentation",
            "feature",
        ]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="123",
        )

        assert "Bug fix prompt with 123" in result
        assert "Global header" in result

    def test_render_prompt_fallback_with_empty_mappings(self, tmp_path):
        """Test fallback when label mappings are empty."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default issue action"\n  bugfix: "Bug fix prompt"\n  feature: "Feature prompt"\n  enhancement: "Enhancement prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = ["bug"]
        mappings = {}  # Empty mappings
        priorities = ["bug"]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
        )

        # Should fall back to default
        assert "Default issue action" in result

    def test_render_prompt_fallback_with_empty_priorities(self, tmp_path):
        """Test fallback when label priorities are empty."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Default issue action"\n  bugfix: "Bug fix prompt"\n  feature: "Feature prompt"\n  enhancement: "Enhancement prompt"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = []  # Empty priorities

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
        )

        # Should fall back to default
        assert "Default issue action" in result

    def test_render_prompt_with_none_label_config(self, temp_prompt_file):
        """Test that None label config falls back gracefully."""
        clear_prompt_cache()
        result = render_prompt(
            "category.message",
            path=str(temp_prompt_file),
            labels=None,
            label_prompt_mappings=None,
            label_priorities=None,
            name="Test",
        )
        assert result == "Hello Test!"

    def test_render_prompt_priority_conflict_resolution(self, tmp_path):
        """Test that priority conflicts are resolved correctly."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "Global header"\n'
            "issue:\n"
            '  action: "Default issue action"\n'
            '  bugfix: "Bug fix prompt with $issue_number"\n'
            '  feature: "Feature prompt with $issue_number"\n'
            '  enhancement: "Enhancement prompt with $issue_number"\n'
            '  breaking_change: "Breaking change prompt - DELETE TESTS with $issue_number"\n'
            '  urgent: "Urgent prompt with $issue_number"\n'
            '  documentation: "Documentation prompt with $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        # Test when multiple high-priority labels are present
        labels = ["urgent", "breaking-change", "bug"]
        mappings = {
            "urgent": "issue.urgent",
            "breaking-change": "issue.breaking_change",
            "bug": "issue.bugfix",
        }
        priorities = [
            "breaking-change",
            "urgent",
            "bug",
        ]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="456",
        )

        # Breaking-change should win (highest priority)
        assert "Breaking change prompt - DELETE TESTS with 456" in result

    def test_get_label_specific_prompt_with_semantic_labels(self):
        """Test label-specific prompt selection with semantic labels."""
        labels = ["bugfix", "defect"]  # Synonyms for bug
        mappings = {"bug": "issue.bugfix", "defect": "issue.bugfix"}
        priorities = ["bug", "defect"]

        result = get_label_specific_prompt(labels, mappings, priorities)
        # Should match one of the mappings
        assert result is not None
        assert "bugfix" in result

    def test_label_priority_with_missing_mappings(self):
        """Test behavior when priorities reference unmapped labels."""
        labels = ["bug", "custom-label"]
        mappings = {"bug": "issue.bugfix"}  # custom-label not in mappings
        priorities = ["custom-label", "bug"]  # Priority order has unmapped label first

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"  # Should skip unmapped priority and use bug

    def test_render_prompt_preserves_data_params(self, tmp_path):
        """Test that data parameters are preserved in label-specific prompts."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "Global header"\n'
            "issue:\n"
            '  action: "Default issue action"\n'
            '  bugfix: "Bug fix prompt with $issue_number"\n'
            '  feature: "Feature prompt with $issue_number"\n'
            '  enhancement: "Enhancement prompt with $issue_number"\n'
            '  breaking_change: "Breaking change prompt - DELETE TESTS with $issue_number"\n'
            '  urgent: "Urgent prompt with $issue_number"\n'
            '  documentation: "Documentation prompt with $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            data={"issue_number": "789", "repo_name": "test/repo"},
        )

        # Data should be available in the rendered prompt
        assert "789" in result
        # repo_name is not used in the template, so just check that it doesn't error

    def test_render_prompt_header_integration(self, tmp_path):
        """Test that header is prepended to label-specific prompts."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "Global header"\n'
            "issue:\n"
            '  action: "Default issue action"\n'
            '  bugfix: "Bug fix prompt with $issue_number"\n'
            '  feature: "Feature prompt with $issue_number"\n'
            '  enhancement: "Enhancement prompt with $issue_number"\n'
            '  breaking_change: "Breaking change prompt - DELETE TESTS with $issue_number"\n'
            '  urgent: "Urgent prompt with $issue_number"\n'
            '  documentation: "Documentation prompt with $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug"]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
        )

        # Header should be present
        assert result.startswith("Global header")

    def test_breaking_change_priority_over_urgent(self):
        """Test that breaking-change has higher priority than urgent."""
        labels = ["urgent", "breaking-change"]
        mappings = {
            "urgent": "issue.urgent",
            "breaking-change": "issue.breaking_change",
        }
        priorities = ["breaking-change", "urgent"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "breaking-change"

    def test_render_prompt_with_enhancement_aliases(self, tmp_path):
        """Test that enhancement aliases work correctly."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'header: "Global header"\n'
            "issue:\n"
            '  action: "Default issue action"\n'
            '  bugfix: "Bug fix prompt with $issue_number"\n'
            '  feature: "Feature prompt with $issue_number"\n'
            '  enhancement: "Enhancement prompt with $issue_number"\n'
            '  breaking_change: "Breaking change prompt - DELETE TESTS with $issue_number"\n'
            '  urgent: "Urgent prompt with $issue_number"\n'
            '  documentation: "Documentation prompt with $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()
        labels = ["feature", "improvement"]
        mappings = {
            "feature": "issue.enhancement",
            "improvement": "issue.enhancement",
        }
        priorities = ["feature", "improvement"]

        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            labels=labels,
            label_prompt_mappings=mappings,
            label_priorities=priorities,
            issue_number="999",
        )

        # Should use enhancement prompt
        assert "Enhancement prompt with 999" in result

    def test_label_priority_exact_match_precedence(self):
        """Test that exact matches take precedence over partial matches."""
        labels = ["breaking"]
        mappings = {
            "breaking": "issue.breaking_change",
            "breaking-change": "issue.breaking_change",
        }
        priorities = ["breaking-change", "breaking"]

        result = _resolve_label_priority(labels, mappings, priorities)
        # Should match "breaking" exactly
        assert result == "breaking"


@pytest.fixture
def temp_prompt_file(tmp_path):
    path = tmp_path / "prompts.yaml"
    path.write_text('category:\n  message: "Hello $name!"\n', encoding="utf-8")
    return path


@pytest.fixture
def label_prompt_file(tmp_path):
    """Create a test prompt file with label-specific prompts."""
    path = tmp_path / "prompts.yaml"
    path.write_text(
        'issue:\n  action: "Default issue action"\n  bugfix: "Bug fix prompt"\n  feature: "Feature prompt"\n  enhancement: "Enhancement prompt"\n',
        encoding="utf-8",
    )
    return path


def test_render_prompt_with_custom_path(temp_prompt_file):
    clear_prompt_cache()
    result = render_prompt("category.message", path=str(temp_prompt_file), name="World")
    assert result == "Hello World!"


def test_get_prompt_template_uses_cache(temp_prompt_file):
    clear_prompt_cache()
    # First load caches the file
    first = get_prompt_template("category.message", path=str(temp_prompt_file))
    assert "Hello $name!" in first

    # Overwrite file; cached version should still be returned
    temp_prompt_file.write_text('category:\n  message: "Changed"\n', encoding="utf-8")
    cached = get_prompt_template("category.message", path=str(temp_prompt_file))
    assert cached == first

    clear_prompt_cache()
    refreshed = get_prompt_template("category.message", path=str(temp_prompt_file))
    assert refreshed == "Changed"


def test_default_prompt_file_exists():
    path = DEFAULT_PROMPTS_PATH
    assert path.exists(), f"Default prompt file missing at {path}"


def test_missing_prompt_file_causes_system_exit(tmp_path):
    prompt_loader.clear_prompt_cache()
    original = prompt_loader.DEFAULT_PROMPTS_PATH
    try:
        missing = tmp_path / "no_such_prompts.yaml"
        prompt_loader.DEFAULT_PROMPTS_PATH = missing
        with pytest.raises(SystemExit):
            # Any key is fine; loading will fail before lookup
            render_prompt("any.key")
    finally:
        prompt_loader.DEFAULT_PROMPTS_PATH = original
        prompt_loader.clear_prompt_cache()


def test_invalid_yaml_causes_system_exit(tmp_path):
    prompt_loader.clear_prompt_cache()
    original = prompt_loader.DEFAULT_PROMPTS_PATH
    try:
        bad = tmp_path / "prompts.yaml"
        bad.write_text(":-: not yaml\n", encoding="utf-8")
        prompt_loader.DEFAULT_PROMPTS_PATH = bad
        with pytest.raises(SystemExit):
            render_prompt("any.key")
    finally:
        prompt_loader.DEFAULT_PROMPTS_PATH = original
        prompt_loader.clear_prompt_cache()


# Tests for label priority resolution


def test_resolve_label_priority_single_applicable_label():
    """Test that a single applicable label is returned."""
    labels = ["bug", "feature"]
    mappings = {"bug": "issue.bugfix"}
    priorities = ["feature", "bug"]

    result = _resolve_label_priority(labels, mappings, priorities)
    assert result == "bug"


def test_resolve_label_priority_multiple_applicable_labels():
    """Test that highest priority applicable label is returned."""
    labels = ["bug", "enhancement", "feature"]
    mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
    priorities = ["feature", "bug", "enhancement"]

    result = _resolve_label_priority(labels, mappings, priorities)
    assert result == "feature"  # Highest priority


def test_resolve_label_priority_no_applicable_labels():
    """Test that None is returned when no labels have mappings."""
    labels = ["documentation", "question"]
    mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
    priorities = ["feature", "bug"]

    result = _resolve_label_priority(labels, mappings, priorities)
    assert result is None


def test_resolve_label_priority_empty_inputs():
    """Test that None is returned for empty inputs."""
    result = _resolve_label_priority([], {}, [])
    assert result is None


def test_resolve_label_priority_no_mappings():
    """Test that None is returned when no mappings exist."""
    labels = ["bug", "feature"]
    mappings = {}
    priorities = ["feature", "bug"]

    result = _resolve_label_priority(labels, mappings, priorities)
    assert result is None


def test_resolve_label_priority_no_priorities():
    """Test that first applicable label is returned when no priorities specified."""
    labels = ["bug", "feature"]
    mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
    priorities = []

    result = _resolve_label_priority(labels, mappings, priorities)
    assert result == "bug"  # Fallback to first applicable label


# Tests for label-to-prompt mapping


def test_get_prompt_for_labels_valid_mapping():
    """Test that valid label mapping returns the correct prompt key."""
    labels = ["bug", "feature"]
    mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
    priorities = ["feature", "bug"]

    result = _get_prompt_for_labels(labels, mappings, priorities)
    assert result == "issue.feature"  # Highest priority


def test_get_prompt_for_labels_no_applicable_labels():
    """Test that None is returned when no labels have mappings."""
    labels = ["documentation", "question"]
    mappings = {"bug": "issue.bugfix"}
    priorities = ["bug"]

    result = _get_prompt_for_labels(labels, mappings, priorities)
    assert result is None


def test_get_prompt_for_labels_empty_inputs():
    """Test that None is returned for empty inputs."""
    result = _get_prompt_for_labels([], {}, [])
    assert result is None


def test_get_prompt_for_labels_no_mappings():
    """Test that None is returned when no mappings provided."""
    labels = ["bug", "feature"]
    mappings = {}
    priorities = ["feature", "bug"]

    result = _get_prompt_for_labels(labels, mappings, priorities)
    assert result is None


def test_get_prompt_for_labels_no_priorities():
    """Test that first applicable label is returned as fallback when priorities is empty list."""
    labels = ["bug", "feature"]
    mappings = {"bug": "issue.bugfix"}
    priorities = []

    result = _get_prompt_for_labels(labels, mappings, priorities)
    # Empty priorities list falls back to first applicable label
    assert result == "issue.bugfix"


# Tests for get_label_specific_prompt function


def test_get_label_specific_prompt_valid():
    """Test that valid inputs return correct prompt key."""
    labels = ["bug"]
    mappings = {"bug": "issue.bugfix"}
    priorities = ["bug"]

    result = get_label_specific_prompt(labels, mappings, priorities)
    assert result == "issue.bugfix"


def test_get_label_specific_prompt_no_labels():
    """Test that None is returned when no labels provided."""
    mappings = {"bug": "issue.bugfix"}
    priorities = ["bug"]

    result = get_label_specific_prompt([], mappings, priorities)
    assert result is None


def test_get_label_specific_prompt_no_mappings():
    """Test that None is returned when no mappings provided."""
    labels = ["bug"]
    priorities = ["bug"]

    result = get_label_specific_prompt(labels, {}, priorities)
    assert result is None


def test_get_label_specific_prompt_no_priorities():
    """Test that first applicable label is returned when priorities is empty list."""
    labels = ["bug"]
    mappings = {"bug": "issue.bugfix"}

    result = get_label_specific_prompt(labels, mappings, [])
    # Empty priorities list should fall back to first applicable label
    assert result == "issue.bugfix"


def test_get_label_specific_prompt_none_inputs():
    """Test that None is returned for None inputs."""
    result = get_label_specific_prompt(None, None, None)
    assert result is None


# Tests for render_prompt with label-based selection


def test_render_prompt_with_labels(label_prompt_file):
    """Test that render_prompt uses label-specific prompt when provided."""
    clear_prompt_cache()
    labels = ["bug"]
    mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
    priorities = ["bug", "feature"]

    result = render_prompt(
        "issue.action",
        path=str(label_prompt_file),
        labels=labels,
        label_prompt_mappings=mappings,
        label_priorities=priorities,
    )

    assert "Bug fix prompt" in result


def test_render_prompt_with_labels_priority(label_prompt_file):
    """Test that render_prompt respects label priority."""
    labels = ["bug", "feature"]
    mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
    priorities = ["feature", "bug"]

    result = render_prompt(
        "issue.action",
        path=str(label_prompt_file),
        labels=labels,
        label_prompt_mappings=mappings,
        label_priorities=priorities,
    )

    # Should use feature prompt (higher priority)
    assert "Feature prompt" in result


def test_render_prompt_label_fallback_no_applicable(label_prompt_file):
    """Test that render_prompt falls back to default when no applicable labels."""
    clear_prompt_cache()
    labels = ["documentation", "question"]
    mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
    priorities = ["bug", "feature"]

    result = render_prompt(
        "issue.action",
        path=str(label_prompt_file),
        labels=labels,
        label_prompt_mappings=mappings,
        label_priorities=priorities,
    )

    # Should fall back to default
    assert "Default issue action" in result


def test_render_prompt_label_fallback_missing_template(label_prompt_file):
    """Test that render_prompt falls back when label-specific template fails."""
    clear_prompt_cache()
    labels = ["enhancement"]
    # Map to a non-existent template
    mappings = {"enhancement": "nonexistent.template"}
    priorities = ["enhancement"]

    # SystemExit is now caught and handled, causing fallback to default template
    result = render_prompt(
        "issue.action",
        path=str(label_prompt_file),
        labels=labels,
        label_prompt_mappings=mappings,
        label_priorities=priorities,
    )

    # Should fall back to the original key
    assert "Default issue action" in result


def test_render_prompt_backward_compatibility(temp_prompt_file):
    """Test that render_prompt maintains backward compatibility without labels."""
    clear_prompt_cache()
    result = render_prompt("category.message", path=str(temp_prompt_file), name="World")
    assert result == "Hello World!"


def test_render_prompt_backward_compatibility_with_data(temp_prompt_file):
    """Test backward compatibility with data parameter."""
    clear_prompt_cache()
    result = render_prompt("category.message", path=str(temp_prompt_file), data={"name": "Universe"})
    assert result == "Hello Universe!"


def test_render_prompt_partial_label_config(label_prompt_file):
    """Test that render_prompt falls back when only some label config is provided."""
    clear_prompt_cache()
    labels = ["bug"]

    # Only provide mappings, no priorities
    mappings = {"bug": "issue.bugfix"}

    result = render_prompt(
        "issue.action",
        path=str(label_prompt_file),
        labels=labels,
        label_prompt_mappings=mappings,
        # label_priorities missing
    )

    # Should fall back to default (no priorities)
    assert "Default issue action" in result


def test_render_prompt_empty_labels(label_prompt_file):
    """Test that render_prompt works with empty label list."""
    clear_prompt_cache()
    mappings = {"bug": "issue.bugfix"}
    priorities = ["bug"]

    result = render_prompt(
        "issue.action",
        path=str(label_prompt_file),
        labels=[],
        label_prompt_mappings=mappings,
        label_priorities=priorities,
    )

    # Should fall back to default
    assert "Default issue action" in result


# Tests for breaking-change label detection


def test_is_breaking_change_issue_with_breaking_change_label():
    """Test that breaking-change label is detected."""
    labels = ["bug", "breaking-change"]
    assert _is_breaking_change_issue(labels) is True


def test_is_breaking_change_issue_with_breaking_label():
    """Test that breaking label is detected."""
    labels = ["feature", "breaking"]
    assert _is_breaking_change_issue(labels) is True


def test_is_breaking_change_issue_with_api_change_label():
    """Test that api-change label is detected."""
    labels = ["enhancement", "api-change"]
    assert _is_breaking_change_issue(labels) is True


def test_is_breaking_change_issue_with_deprecation_label():
    """Test that deprecation label is detected."""
    labels = ["documentation", "deprecation"]
    assert _is_breaking_change_issue(labels) is True


def test_is_breaking_change_issue_with_version_major_label():
    """Test that version-major label is detected."""
    labels = ["feature", "version-major"]
    assert _is_breaking_change_issue(labels) is True


def test_is_breaking_change_issue_without_breaking_labels():
    """Test that non-breaking labels are not detected."""
    labels = ["bug", "feature", "enhancement", "documentation"]
    assert _is_breaking_change_issue(labels) is False


def test_is_breaking_change_issue_with_empty_list():
    """Test that empty label list returns False."""
    assert _is_breaking_change_issue([]) is False


def test_is_breaking_change_issue_case_insensitive():
    """Test that label detection is case-insensitive."""
    labels = ["Feature", "BUG", "Breaking-Change"]
    assert _is_breaking_change_issue(labels) is True


def test_breaking_change_label_has_highest_priority():
    """Test that breaking-change label has higher priority than urgent."""
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


def test_render_prompt_with_breaking_change_label(label_prompt_file):
    """Test that render_prompt uses breaking-change prompt when label is present."""
    clear_prompt_cache()

    # Update the label_prompt_file to include breaking_change prompt
    from pathlib import Path

    label_prompt_file.write_text(
        'issue:\n  action: "Default issue action"\n  breaking_change: "Breaking change prompt"\n',
        encoding="utf-8",
    )
    clear_prompt_cache()

    labels = ["breaking-change"]
    mappings = {
        "breaking-change": "issue.breaking_change",
    }
    priorities = ["breaking-change"]

    result = render_prompt(
        "issue.action",
        path=str(label_prompt_file),
        labels=labels,
        label_prompt_mappings=mappings,
        label_priorities=priorities,
    )

    assert "Breaking change prompt" in result
