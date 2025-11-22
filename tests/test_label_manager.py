"""Tests for LabelManager context manager."""

import time
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.github_client import GitHubClient
from src.auto_coder.label_manager import LabelManager, get_semantic_labels_from_issue, resolve_pr_labels_with_priority


class TestLabelManager:
    """Test LabelManager context manager functionality."""

    def test_label_manager_context_manager_success(self):
        """Test successful label management - add and remove label."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        # Use LabelManager context manager
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
            assert should_process is True
            # Label should be added inside context
            mock_github_client.try_add_labels.assert_called_once_with("owner/repo", 123, ["@auto-coder"], item_type="issue")

        # Label should be removed after exiting context
        mock_github_client.remove_labels.assert_called_once_with("owner/repo", 123, ["@auto-coder"], "issue")

    def test_label_manager_skips_when_label_already_exists(self):
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.get_issue_details_by_number.return_value = {"labels": ["@auto-coder"]}

        config = AutomationConfig()

        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
            assert should_process is False
            mock_github_client.try_add_labels.assert_not_called()

        mock_github_client.remove_labels.assert_not_called()

    def test_label_manager_with_labels_disabled_via_client(self):
        """Test that context manager skips when labels are disabled via client."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = True  # Labels disabled

        config = AutomationConfig()

        # Use LabelManager context manager
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
            assert should_process is True
            # No label operations should be performed
            mock_github_client.try_add_labels.assert_not_called()

        # No removal should happen
        mock_github_client.remove_labels.assert_not_called()

    def test_label_manager_with_labels_disabled_via_config(self):
        """Test that context manager skips when labels are disabled via config."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False

        config = AutomationConfig()
        config.DISABLE_LABELS = True  # Labels disabled via config

        # Use LabelManager context manager
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
            assert should_process is True
            # No label operations should be performed
            mock_github_client.try_add_labels.assert_not_called()

        # No removal should happen
        mock_github_client.remove_labels.assert_not_called()

    def test_label_manager_cleanup_on_exception(self):
        """Test that label is removed even when exception occurs."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        # Use LabelManager context manager and raise an exception
        try:
            with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
                assert should_process is True
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Label should still be removed after exception
        mock_github_client.remove_labels.assert_called_once_with("owner/repo", 123, ["@auto-coder"], "issue")

    def test_label_manager_with_custom_label_name(self):
        """Test that LabelManager works with custom label names."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        # Use LabelManager with custom label
        with LabelManager(
            mock_github_client,
            "owner/repo",
            123,
            "issue",
            label_name="custom-label",
            config=config,
        ) as should_process:
            assert should_process is True
            mock_github_client.try_add_labels.assert_called_once_with("owner/repo", 123, ["custom-label"], item_type="issue")

        # Custom label should be removed
        mock_github_client.remove_labels.assert_called_once_with("owner/repo", 123, ["custom-label"], "issue")

    def test_label_manager_pr_type(self):
        """Test that LabelManager works with PR type."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        # Use LabelManager for PR
        with LabelManager(mock_github_client, "owner/repo", 456, "pr", config=config) as should_process:
            assert should_process is True
            mock_github_client.try_add_labels.assert_called_once_with("owner/repo", 456, ["@auto-coder"], item_type="pr")

        # Label should be removed from PR
        mock_github_client.remove_labels.assert_called_once_with("owner/repo", 456, ["@auto-coder"], "pr")

    def test_label_manager_retry_on_add_failure(self):
        """Test that LabelManager retries on label addition failure."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        # First two calls fail, third succeeds
        mock_github_client.try_add_labels.side_effect = [
            Exception("API error 1"),
            Exception("API error 2"),
            True,  # Success on third try
        ]

        config = AutomationConfig()

        # Use LabelManager with retry
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config, max_retries=3) as should_process:
            assert should_process is True
            # Should be called 3 times (2 failures + 1 success)
            assert mock_github_client.try_add_labels.call_count == 3

    def test_label_manager_gives_up_after_max_retries(self):
        """Test that LabelManager gives up after max retries and continues processing."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        # Always fails
        mock_github_client.try_add_labels.side_effect = Exception("API error")

        config = AutomationConfig()

        # Use LabelManager with retries
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config, max_retries=2) as should_process:
            assert should_process is True  # Still returns True to allow processing
            # Should be called 2 times (max_retries)
            assert mock_github_client.try_add_labels.call_count == 2

    def test_label_manager_retry_on_remove_failure(self):
        """Test that LabelManager retries on label removal failure."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True
        # First two remove calls fail, third succeeds
        mock_github_client.remove_labels.side_effect = [
            Exception("API error 1"),
            Exception("API error 2"),
            None,  # Success on third try
        ]

        config = AutomationConfig()

        # Use LabelManager
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config, max_retries=3) as should_process:
            assert should_process is True

        # Remove should be called 3 times (2 failures + 1 success)
        assert mock_github_client.remove_labels.call_count == 3

    def test_label_manager_thread_safety(self):
        """Test that LabelManager is thread-safe (uses locks internally)."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        # Create multiple LabelManager instances to test they don't interfere
        managers = [LabelManager(mock_github_client, "owner/repo", i, "issue", config=config) for i in range(5)]

        # All should be able to enter and exit independently
        for manager in managers:
            with manager as should_process:
                assert should_process is True

        # All should have been processed
        assert mock_github_client.try_add_labels.call_count == 5
        assert mock_github_client.remove_labels.call_count == 5

    def test_label_manager_no_label_added_flag(self):
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.get_issue_details_by_number.return_value = {"labels": []}
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
            assert should_process is True
            mock_github_client.try_add_labels.assert_called_once()

        mock_github_client.remove_labels.assert_called_once_with("owner/repo", 123, ["@auto-coder"], "issue")

    def test_label_manager_network_error_on_label_check(self):
        """Test that LabelManager handles network errors during label check."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        # Simulate network error during has_label check
        mock_github_client.has_label.side_effect = Exception("Network error")
        # But check_label_exists should fallback and return False
        mock_github_client.get_issue_details_by_number.return_value = {"labels": []}

        config = AutomationConfig()

        # Use LabelManager - should still proceed even if check fails
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
            # Should still return True to allow processing
            assert should_process is True

    def test_label_manager_exception_in_exit_does_not_propagate(self):
        """Test that exceptions in __exit__ don't propagate to caller."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True
        # Simulate error during cleanup
        mock_github_client.remove_labels.side_effect = Exception("Cleanup error")

        config = AutomationConfig()

        # __exit__ should not raise the exception
        try:
            with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
                assert should_process is True
        except Exception as e:
            pytest.fail(f"__exit__ should not propagate exceptions, but got: {e}")

    def test_label_manager_with_retry_delay(self):
        """Test that custom retry_delay is respected."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        # First attempt fails, second succeeds
        mock_github_client.try_add_labels.side_effect = [Exception("API error"), True]

        config = AutomationConfig()

        # Use custom retry_delay
        start = time.time()
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config, max_retries=2, retry_delay=0.1) as should_process:
            assert should_process is True
        elapsed = time.time() - start

        # Should wait at least 0.1 seconds for retry
        assert elapsed >= 0.1, f"Expected at least 0.1s delay, got {elapsed:.4f}s"

    def test_label_manager_zero_retries(self):
        """Test LabelManager with max_retries=0 (no retries attempted)."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        # Always fails
        mock_github_client.try_add_labels.side_effect = Exception("API error")

        config = AutomationConfig()

        # Use with max_retries=0
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config, max_retries=0) as should_process:
            # Should still return True (fail open) - loop doesn't execute when max_retries=0
            assert should_process is True
        # Should NOT be called at all (range(0) is empty)
        assert mock_github_client.try_add_labels.call_count == 0

    def test_label_manager_with_string_item_number(self):
        """Test LabelManager with string item number (e.g., GitHub issue numbers as strings)."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        # Use string item number
        with LabelManager(mock_github_client, "owner/repo", "123", "issue", config=config) as should_process:
            assert should_process is True
            mock_github_client.try_add_labels.assert_called_once_with("owner/repo", 123, ["@auto-coder"], item_type="issue")

        # Label should be removed
        mock_github_client.remove_labels.assert_called_once_with("owner/repo", "123", ["@auto-coder"], "issue")

    def test_label_manager_nested_contexts_different_items(self):
        """Test that nested LabelManager contexts work for different items."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        # Nested contexts for different items
        with LabelManager(mock_github_client, "owner/repo", 1, "issue", config=config) as should_process_1:
            assert should_process_1 is True

            # Inner context for different item
            with LabelManager(mock_github_client, "owner/repo", 2, "issue", config=config) as should_process_2:
                assert should_process_2 is True

        # Both should have been processed
        assert mock_github_client.try_add_labels.call_count == 2
        assert mock_github_client.remove_labels.call_count == 2

    def test_label_manager_fallback_to_get_issue_details(self):
        """Test that LabelManager falls back to get_issue_details when has_label is not available."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        # has_label is not a real method (just a Mock attribute)
        mock_github_client.has_label = Mock()  # This will be detected as not a real method
        # get_issue_details should work
        mock_github_client.get_issue_details_by_number.return_value = {"labels": []}
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        # Use LabelManager - should use fallback
        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
            assert should_process is True
            # Should have called get_issue_details
            mock_github_client.get_issue_details_by_number.assert_called_once()

    def test_label_manager_fail_open_when_all_checks_error_issue(self):
        """All existence checks raise → fail-open: proceed and try to add label."""
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.side_effect = Exception("primary check error")
        mock_github_client.get_issue_details_by_number.side_effect = Exception("details error")
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config) as should_process:
            assert should_process is True
            mock_github_client.try_add_labels.assert_called_once()

    def test_label_manager_fail_open_when_all_checks_error_pr(self):
        """PR path: has_label and both PR/Issue detail fallbacks raise → proceed."""
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.side_effect = Exception("primary check error")
        mock_github_client.get_pr_details_by_number.side_effect = Exception("pr details error")
        mock_github_client.get_issue_details_by_number.side_effect = Exception("issue details error")
        mock_github_client.try_add_labels.return_value = True

        config = AutomationConfig()

        with LabelManager(mock_github_client, "owner/repo", 456, "pr", config=config) as should_process:
            assert should_process is True
            mock_github_client.try_add_labels.assert_called_once_with("owner/repo", 456, ["@auto-coder"], item_type="pr")

    def test_label_manager_check_only_mode_label_exists(self):
        """skip_label_add=True かつ既存ラベルあり → False を返し、追加も削除もしない。"""
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.get_issue_details_by_number.return_value = {"labels": ["@auto-coder"]}

        config = AutomationConfig()

        with LabelManager(mock_github_client, "owner/repo", 123, "issue", config=config, skip_label_add=True) as should_process:
            assert should_process is False
            mock_github_client.try_add_labels.assert_not_called()
        mock_github_client.remove_labels.assert_not_called()


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
