"""Tests for semantic GitHub label handling."""

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.label_manager import get_semantic_labels_from_issue, resolve_pr_labels_with_priority


class TestSemanticLabelFunctions:
    """Test semantic label detection and priority resolution functions."""

    def test_get_semantic_labels_from_issue_with_exact_match(self):
        """Test semantic label detection with exact label matches."""
        issue_labels = ["bug", "urgent", "documentation"]
        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": ["bug", "bugfix"],
            "documentation": ["documentation", "docs"],
            "enhancement": ["enhancement", "feature"],
            "urgent": ["urgent"],
        }

        result = get_semantic_labels_from_issue(issue_labels, label_mappings)
        assert set(result) == {"bug", "urgent", "documentation"}

    def test_get_semantic_labels_from_issue_with_aliases(self):
        """Test semantic label detection with label aliases."""
        issue_labels = ["bugfix", "high-priority", "doc", "feature"]
        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": ["bug", "bugfix", "defect"],
            "documentation": ["documentation", "docs", "doc"],
            "enhancement": ["enhancement", "feature", "improvement"],
            "urgent": ["urgent", "high-priority", "critical"],
        }

        result = get_semantic_labels_from_issue(issue_labels, label_mappings)
        assert set(result) == {"bug", "documentation", "enhancement", "urgent"}

    def test_get_semantic_labels_from_issue_case_insensitive(self):
        """Test semantic label detection is case-insensitive."""
        issue_labels = ["BUG", "URGENT", "Documentation"]
        label_mappings = {
            "bug": ["bug", "bugfix"],
            "documentation": ["documentation", "docs"],
            "urgent": ["urgent"],
        }

        result = get_semantic_labels_from_issue(issue_labels, label_mappings)
        assert set(result) == {"bug", "urgent", "documentation"}

    def test_get_semantic_labels_from_issue_no_matches(self):
        """Test semantic label detection with no matching labels."""
        issue_labels = ["random-label", "another-label"]
        label_mappings = {
            "bug": ["bug", "bugfix"],
            "urgent": ["urgent"],
        }

        result = get_semantic_labels_from_issue(issue_labels, label_mappings)
        assert result == []

    def test_get_semantic_labels_from_issue_empty(self):
        """Test semantic label detection with empty labels."""
        issue_labels = []
        label_mappings = {
            "bug": ["bug", "bugfix"],
            "urgent": ["urgent"],
        }

        result = get_semantic_labels_from_issue(issue_labels, label_mappings)
        assert result == []

    def test_get_semantic_labels_no_duplicates(self):
        """Test semantic label detection doesn't create duplicates."""
        issue_labels = ["bug", "bugfix", "BugFix"]
        label_mappings = {
            "bug": ["bug", "bugfix", "defect"],
        }

        result = get_semantic_labels_from_issue(issue_labels, label_mappings)
        assert result == ["bug"]


class TestLegacyAutoCoderLabelIsSemanticallyInert:
    """FTR-1792: the exact retired '@auto-coder' label must not affect semantic
    PR-label resolution (REQ-003/REQ-004/REQ-005/REQ-007, AS-005, AS-006)."""

    def test_filter_strips_only_exact_legacy_label_from_strings(self):
        from src.auto_coder.label_manager import filter_legacy_auto_coder_label

        result = filter_legacy_auto_coder_label(["bug", "@auto-coder", "urgent"])
        assert result == ["bug", "urgent"]

    def test_filter_preserves_near_miss_labels(self):
        """AS-002/AS-006: near-misses are not the retired label and must survive."""
        from src.auto_coder.label_manager import filter_legacy_auto_coder_label

        result = filter_legacy_auto_coder_label(["auto-coder", "@auto-coder-old", "@Auto-Coder"])
        assert result == ["auto-coder", "@auto-coder-old", "@Auto-Coder"]

    def test_filter_strips_exact_legacy_label_from_raw_dicts(self):
        """Raw GitHub API/webhook label dicts must be filtered the same as name strings."""
        from src.auto_coder.label_manager import filter_legacy_auto_coder_label

        raw_labels = [{"name": "bug"}, {"name": "@auto-coder"}, {"name": "urgent"}]
        assert filter_legacy_auto_coder_label(raw_labels) == ["bug", "urgent"]

    def test_filter_handles_empty_and_none(self):
        from src.auto_coder.label_manager import filter_legacy_auto_coder_label

        assert filter_legacy_auto_coder_label([]) == []
        assert filter_legacy_auto_coder_label(None) == []

    def test_get_semantic_labels_ignores_exact_legacy_label(self):
        """REQ-003/REQ-004: the retired label alone must never resolve to a semantic label."""
        label_mappings = {"bug": ["bug", "bugfix"], "urgent": ["urgent"]}

        assert get_semantic_labels_from_issue(["@auto-coder"], label_mappings) == []

    def test_get_semantic_labels_identical_with_and_without_legacy_label(self):
        """REQ-005/AS-006: adding/removing only '@auto-coder' must not change the result."""
        label_mappings = {"bug": ["bug", "bugfix"], "urgent": ["urgent", "high-priority"]}

        without_legacy = get_semantic_labels_from_issue(["bug", "urgent"], label_mappings)
        with_legacy = get_semantic_labels_from_issue(["bug", "urgent", "@auto-coder"], label_mappings)
        assert with_legacy == without_legacy

    def test_get_semantic_labels_malicious_alias_cannot_reinterpret_legacy_label(self):
        """AS-005: a configured alias mapping '@auto-coder' to a semantic label must
        never let the retired label propagate that semantic label."""
        malicious_mappings = {"urgent": ["urgent", "@auto-coder", "auto-coder"]}

        # Only the exact retired label is present -> must resolve to nothing.
        assert get_semantic_labels_from_issue(["@auto-coder"], malicious_mappings) == []
        # A near-miss alias entry ("auto-coder") is untouched by this filter and
        # may still legitimately match, since REQ-007 keeps other labels' alias
        # semantics unchanged; only the exact retired label text is excluded.
        assert get_semantic_labels_from_issue(["auto-coder"], malicious_mappings) == ["urgent"]

    def test_resolve_pr_labels_with_priority_ignores_exact_legacy_label(self):
        """REQ-003 applied to the resolve_pr_labels_with_priority entry point."""
        config = AutomationConfig()
        config.PR_LABEL_MAPPINGS = {"urgent": ["urgent"], "bug": ["bug"]}
        config.PR_LABEL_PRIORITIES = ["urgent", "bug"]

        assert resolve_pr_labels_with_priority(["@auto-coder"], config) == []

    def test_resolve_pr_labels_with_priority_identical_with_and_without_legacy_label(self):
        """REQ-005/AS-006 at the resolve_pr_labels_with_priority entry point."""
        config = AutomationConfig()
        config.PR_LABEL_MAPPINGS = {"urgent": ["urgent"], "bug": ["bug"]}
        config.PR_LABEL_PRIORITIES = ["urgent", "bug"]

        without_legacy = resolve_pr_labels_with_priority(["bug", "urgent"], config)
        with_legacy = resolve_pr_labels_with_priority(["bug", "urgent", "@auto-coder"], config)
        assert with_legacy == without_legacy

    def test_resolve_pr_labels_with_priority_malicious_alias_cannot_reinterpret_legacy_label(self):
        """AS-005 at the resolve_pr_labels_with_priority entry point used by
        _create_pr_for_issue to propagate semantic labels from issues to PRs."""
        config = AutomationConfig()
        config.PR_LABEL_MAPPINGS = {"urgent": ["urgent", "@auto-coder"]}
        config.PR_LABEL_PRIORITIES = ["urgent"]

        assert resolve_pr_labels_with_priority(["@auto-coder"], config) == []


class TestFuzzyMatching:
    """Test fuzzy matching functionality for label detection."""

    def test_fuzzy_match_normalization(self):
        """Test label normalization for fuzzy matching."""
        from src.auto_coder.label_manager import _normalize_label

        # Test basic normalization
        assert _normalize_label("BUG-FIX") == "bug-fix"
        assert _normalize_label("bug fix") == "bug-fix"
        assert _normalize_label("bug_fix") == "bug-fix"
        assert _normalize_label("bug__fix") == "bug-fix"
        assert _normalize_label("breaking_change") == "breaking-change"

        # Test special characters
        assert _normalize_label("bug!@#$fix") == "bugfix"
        assert _normalize_label("breaking-change") == "breaking-change"

        # Test duplicate hyphens
        assert _normalize_label("bug---fix") == "bug-fix"
        assert _normalize_label("--bug-fix--") == "bug-fix"

        # Test mixed case
        assert _normalize_label("BuG-FiX") == "bug-fix"
        assert _normalize_label("BREAKING-CHANGE") == "breaking-change"

    def test_fuzzy_match_exact(self):
        """Test exact matching with fuzzy matching enabled."""
        from src.auto_coder.label_manager import _is_fuzzy_match

        # Exact matches should work
        assert _is_fuzzy_match("bug", "bug") is True
        assert _is_fuzzy_match("BUG", "bug") is True
        assert _is_fuzzy_match("breaking-change", "breaking-change") is True

    def test_fuzzy_match_hyphen_variations(self):
        """Test fuzzy matching with hyphen/underscore/space variations."""
        from src.auto_coder.label_manager import _is_fuzzy_match

        # Different separators should match
        assert _is_fuzzy_match("bug-fix", "bugfix") is True
        assert _is_fuzzy_match("bugfix", "bug-fix") is True
        assert _is_fuzzy_match("bug_fix", "bug-fix") is True
        assert _is_fuzzy_match("breaking change", "breaking-change") is True

    def test_fuzzy_match_partial(self):
        """Test fuzzy matching with partial string matches."""
        from src.auto_coder.label_manager import _is_fuzzy_match

        # Partial matches should work for meaningful strings
        assert _is_fuzzy_match("bc-breaking", "breaking-change") is True
        assert _is_fuzzy_match("breaking-change", "bc-breaking") is True

    def test_fuzzy_match_levenshtein_distance(self):
        """Test fuzzy matching with Levenshtein distance (typos)."""
        from src.auto_coder.label_manager import _is_fuzzy_match

        # One character difference
        assert _is_fuzzy_match("bug", "bugs") is True
        assert _is_fuzzy_match("fix", "fiix") is True

        # Two character difference for longer strings
        assert _is_fuzzy_match("breaking", "brekaing") is True

        # Too many differences
        assert _is_fuzzy_match("bug", "feature") is False

    def test_fuzzy_match_case_variations(self):
        """Test fuzzy matching with different case variations."""
        from src.auto_coder.label_manager import _is_fuzzy_match

        assert _is_fuzzy_match("BUG", "bug") is True
        assert _is_fuzzy_match("BuG-FiX", "bugfix") is True
        assert _is_fuzzy_match("BREAKING-CHANGE", "breaking change") is True

    def test_fuzzy_match_false_positives(self):
        """Test that fuzzy matching doesn't create false positives."""
        from src.auto_coder.label_manager import _is_fuzzy_match

        # Too short strings should not match
        assert _is_fuzzy_match("b", "bug") is False
        assert _is_fuzzy_match("x", "fix") is False

        # Completely different strings
        assert _is_fuzzy_match("bug", "feature") is False
        assert _is_fuzzy_match("urgent", "documentation") is False

    def test_get_semantic_labels_with_fuzzy_matching_enabled(self):
        """Test semantic label detection with fuzzy matching enabled."""
        issue_labels = ["bc-breaking", "bugg", "docss", "feat"]
        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": ["bug", "bugfix"],
            "documentation": ["documentation", "docs"],
            "enhancement": ["enhancement", "feature"],
        }

        result = get_semantic_labels_from_issue(issue_labels, label_mappings, use_fuzzy_matching=True)
        # bc-breaking should match breaking-change, feat should match enhancement
        # bugg might match bug (typo), docss might match docs (typo)
        assert len(result) >= 2

    def test_get_semantic_labels_with_fuzzy_matching_disabled(self):
        """Test semantic label detection with fuzzy matching disabled."""
        issue_labels = ["bc-breaking", "feat"]
        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "enhancement": ["enhancement", "feature"],
        }

        result = get_semantic_labels_from_issue(issue_labels, label_mappings, use_fuzzy_matching=False)
        # Should only match if exact (case-insensitive)
        assert "enhancement" not in result  # "feat" should not match "feature" without fuzzy matching
        assert "breaking-change" not in result  # "bc-breaking" should not match "breaking-change" without fuzzy matching


class TestLabelFamilies:
    """Test detection of specific label families."""

    def test_breaking_change_family_detection(self):
        """Test breaking-change label family detection."""
        breaking_change_labels = [
            "breaking-change",
            "breaking change",
            "bc-breaking",
            "breaking",
            "incompatible",
        ]

        label_mappings = {
            "breaking-change": [
                "breaking-change",
                "breaking change",
                "bc-breaking",
                "breaking",
                "incompatible",
            ],
            "bug": ["bug", "bugfix"],
            "documentation": ["documentation", "docs"],
            "enhancement": ["enhancement", "feature"],
        }

        for label in breaking_change_labels:
            result = get_semantic_labels_from_issue([label], label_mappings)
            assert "breaking-change" in result, f"Failed to detect breaking-change label: {label}"

    def test_bug_family_detection(self):
        """Test bug label family detection."""
        bug_labels = [
            "bug",
            "bugfix",
            "fix",
            "error",
            "issue",
            "defect",
            "broken",
        ]

        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": [
                "bug",
                "bugfix",
                "fix",
                "error",
                "issue",
                "defect",
                "broken",
            ],
            "documentation": ["documentation", "docs"],
            "enhancement": ["enhancement", "feature"],
        }

        for label in bug_labels:
            result = get_semantic_labels_from_issue([label], label_mappings)
            assert "bug" in result, f"Failed to detect bug label: {label}"

    def test_documentation_family_detection(self):
        """Test documentation label family detection."""
        doc_labels = ["docs", "documentation", "doc", "readme", "guide"]

        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": ["bug", "bugfix"],
            "documentation": ["documentation", "docs", "doc", "readme", "guide"],
            "enhancement": ["enhancement", "feature"],
        }

        for label in doc_labels:
            result = get_semantic_labels_from_issue([label], label_mappings)
            assert "documentation" in result, f"Failed to detect documentation label: {label}"

    def test_enhancement_family_detection(self):
        """Test enhancement label family detection."""
        enhancement_labels = [
            "enhancement",
            "feature",
            "improvement",
            "feat",
            "request",
        ]

        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": ["bug", "bugfix"],
            "documentation": ["documentation", "docs"],
            "enhancement": [
                "enhancement",
                "feature",
                "improvement",
                "feat",
                "request",
            ],
        }

        for label in enhancement_labels:
            result = get_semantic_labels_from_issue([label], label_mappings)
            assert "enhancement" in result, f"Failed to detect enhancement label: {label}"

    def test_urgent_family_detection(self):
        """Test urgent label family detection."""
        urgent_labels = [
            "urgent",
            "high-priority",
            "critical",
            "asap",
            "priority-high",
            "blocker",
        ]

        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": ["bug", "bugfix"],
            "urgent": [
                "urgent",
                "high-priority",
                "critical",
                "asap",
                "priority-high",
                "blocker",
            ],
            "documentation": ["documentation", "docs"],
            "enhancement": ["enhancement", "feature"],
        }

        for label in urgent_labels:
            result = get_semantic_labels_from_issue([label], label_mappings)
            assert "urgent" in result, f"Failed to detect urgent label: {label}"

    def test_question_family_detection(self):
        """Test question label family detection."""
        question_labels = [
            "question",
            "help wanted",
            "support",
            "q&a",
        ]

        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": ["bug", "bugfix"],
            "documentation": ["documentation", "docs"],
            "enhancement": ["enhancement", "feature"],
            "question": ["question", "help wanted", "support", "q&a"],
        }

        for label in question_labels:
            result = get_semantic_labels_from_issue([label], label_mappings)
            assert "question" in result, f"Failed to detect question label: {label}"

    def test_fuzzy_matching_for_label_variants(self):
        """Test fuzzy matching with various label format variants."""
        label_mappings = {
            "breaking-change": ["breaking-change", "breaking"],
            "bug": ["bug", "bugfix", "fix"],
            "documentation": ["documentation", "docs"],
            "enhancement": ["enhancement", "feature"],
        }

        # Test with fuzzy label variants
        test_cases = [
            (["bc--breaking"], "breaking-change"),  # Multiple hyphens
            (["bugg"], "bug"),  # Typo
            (["doc--guide"], "documentation"),  # Multiple hyphens
        ]

        for issue_labels, expected_label in test_cases:
            result = get_semantic_labels_from_issue(issue_labels, label_mappings, use_fuzzy_matching=True)
            assert expected_label in result, f"Failed to match {issue_labels} to {expected_label}"

    def test_resolve_pr_labels_with_priority_all_labels(self):
        """Test priority-based label resolution with all semantic labels."""
        issue_labels = ["bug", "documentation", "enhancement", "urgent", "breaking-change"]
        config = AutomationConfig()
        config.PR_LABEL_MAX_COUNT = 5  # Allow all 5 labels
        # Note: config already has default priorities

        result = resolve_pr_labels_with_priority(issue_labels, config)
        # Should be sorted by priority: urgent > breaking-change > bug > enhancement > documentation
        assert result == ["urgent", "breaking-change", "bug", "enhancement", "documentation"]

    def test_resolve_pr_labels_with_priority_limited(self):
        """Test priority-based label resolution respects max count."""
        issue_labels = ["bug", "documentation", "enhancement", "urgent", "breaking-change"]
        config = AutomationConfig()
        config.PR_LABEL_MAX_COUNT = 2

        result = resolve_pr_labels_with_priority(issue_labels, config)
        assert result == ["urgent", "breaking-change"]

    def test_resolve_pr_labels_with_priority_zero_limit(self):
        """Test priority-based label resolution with zero max count."""
        issue_labels = ["bug", "urgent"]
        config = AutomationConfig()
        config.PR_LABEL_MAX_COUNT = 0

        result = resolve_pr_labels_with_priority(issue_labels, config)
        # When max is 0, should return empty list
        assert result == []

    def test_resolve_pr_labels_with_priority_unprioritized(self):
        """Test priority-based label resolution with unprioritized labels."""
        # Create issue with labels not in priority list
        issue_labels = ["custom-label", "feature", "fix"]
        config = AutomationConfig()
        # Default priorities: breaking-change, urgent, bug, enhancement, documentation

        result = resolve_pr_labels_with_priority(issue_labels, config)
        # feature -> enhancement, fix -> bug (from mappings)
        # bug has priority 2, enhancement has priority 3
        assert set(result) == {"bug", "enhancement"}
        # Should be in priority order
        assert result[0] == "bug"  # Higher priority

    def test_resolve_pr_labels_with_priority_mixed(self):
        """Test priority-based label resolution with mix of prioritized and unprioritized."""
        issue_labels = ["bug", "custom-label", "feature"]
        config = AutomationConfig()
        config.PR_LABEL_MAX_COUNT = 3

        result = resolve_pr_labels_with_priority(issue_labels, config)
        # bug is prioritized (priority 2), feature -> enhancement (priority 3)
        # Both should be included since we have space
        assert set(result) == {"bug", "enhancement"}

    def test_resolve_pr_labels_with_priority_empty(self):
        """Test priority-based label resolution with no semantic labels."""
        issue_labels = ["random-label"]
        config = AutomationConfig()

        result = resolve_pr_labels_with_priority(issue_labels, config)
        assert result == []

    def test_resolve_pr_labels_with_custom_priorities(self):
        """Test priority-based label resolution with custom priority order."""
        issue_labels = ["bug", "urgent", "enhancement", "documentation"]
        config = AutomationConfig()
        # Custom priority: enhancement > bug > documentation > urgent
        config.PR_LABEL_PRIORITIES = ["enhancement", "bug", "documentation", "urgent"]
        config.PR_LABEL_MAX_COUNT = 4

        result = resolve_pr_labels_with_priority(issue_labels, config)
        # Should follow custom priority order
        assert result == ["enhancement", "bug", "documentation", "urgent"]

    def test_resolve_pr_labels_with_custom_mappings(self):
        """Test priority-based label resolution with custom label mappings."""
        issue_labels = ["type-bug", "type-feature"]
        config = AutomationConfig()
        # Custom mappings
        config.PR_LABEL_MAPPINGS = {
            "bug": ["type-bug", "error"],
            "enhancement": ["type-feature", "improvement"],
            "urgent": ["urgent"],
        }

        result = resolve_pr_labels_with_priority(issue_labels, config)
        assert set(result) == {"bug", "enhancement"}

    def test_automation_config_validate_pr_label_config_valid(self):
        """Test configuration validation with valid values."""
        config = AutomationConfig()

        # Should not raise any exception
        config.validate_pr_label_config()

    def test_automation_config_validate_pr_label_config_invalid_max_count(self):
        """Test configuration validation rejects invalid max count."""
        config = AutomationConfig()
        config.PR_LABEL_MAX_COUNT = 15  # Too high

        with pytest.raises(ValueError, match="PR_LABEL_MAX_COUNT must be between 0 and 10"):
            config.validate_pr_label_config()

    def test_automation_config_validate_pr_label_config_negative_count(self):
        """Test configuration validation rejects negative max count."""
        config = AutomationConfig()
        config.PR_LABEL_MAX_COUNT = -1

        with pytest.raises(ValueError, match="PR_LABEL_MAX_COUNT must be between 0 and 10"):
            config.validate_pr_label_config()
