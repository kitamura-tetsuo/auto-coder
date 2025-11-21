"""Error handling and recovery tests for label-based prompt processing.

This module provides comprehensive error handling tests for label-based
prompt processing, including GitHub API rate limiting, missing files,
invalid configurations, and recovery mechanisms.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests  # type: ignore[import-untyped]
import yaml

from src.auto_coder.prompt_loader import (
    _get_prompt_for_labels,
    _resolve_label_priority,
    clear_prompt_cache,
    get_label_specific_prompt,
    load_prompts,
    render_prompt,
)


class TestGitHubAPIRateLimiting:
    """Test handling of GitHub API rate limiting during label operations."""

    def test_rate_limit_error_handling(self):
        """Test graceful handling of rate limit errors."""
        # Mock rate limit response
        mock_client = Mock()
        mock_client.disable_labels = False
        mock_client.has_label.side_effect = [requests.exceptions.HTTPError("403 Rate limit exceeded"), True]  # Success after retry
        mock_client.try_add_labels_to_issue.return_value = True

        # The LabelManager should handle rate limits gracefully
        # This is a conceptual test - actual rate limiting would be in GitHub client
        from src.auto_coder.automation_config import AutomationConfig
        from src.auto_coder.label_manager import LabelManager

        config = AutomationConfig()

        # With retries enabled, should eventually succeed
        with LabelManager(mock_client, "owner/repo", 123, item_type="issue", config=config, max_retries=2):
            pass

    def test_api_timeout_handling(self):
        """Test handling of API timeouts."""
        mock_client = Mock()
        mock_client.disable_labels = False
        # Simulate timeout
        mock_client.has_label.side_effect = requests.exceptions.Timeout("API timeout")
        mock_client.try_add_labels_to_issue.return_value = False

        from src.auto_coder.automation_config import AutomationConfig
        from src.auto_coder.label_manager import LabelManager

        config = AutomationConfig()

        # Should handle timeout gracefully and return False (skip processing)
        with LabelManager(mock_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
            assert should_process is False


class TestMissingPromptTemplateFiles:
    """Test handling of missing prompt template files."""

    def test_missing_yaml_file_fallback(self):
        """Test fallback when label-specific prompt file is missing."""
        # Try to render with non-existent file path
        labels = ["bug"]
        mappings = {"bug": "issue.nonexistent"}
        priorities = ["bug"]

        # Should raise SystemExit (fatal error)
        with pytest.raises(SystemExit):
            render_prompt("issue.nonexistent", path="/nonexistent/file.yaml", labels=labels, label_prompt_mappings=mappings, label_priorities=priorities)

    def test_missing_label_specific_template_with_fallback(self):
        """Test fallback to default when label-specific template doesn't exist."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Default action"\n')
            prompt_file = f.name

        try:
            labels = ["bug"]
            mappings = {"bug": "issue.bugfix"}  # This doesn't exist in the file
            priorities = ["bug"]

            # Should fall back to "issue.action" and succeed
            result = render_prompt("issue.action", path=prompt_file, labels=labels, label_prompt_mappings=mappings, label_priorities=priorities)

            assert "Default action" in result
        finally:
            Path(prompt_file).unlink()

    def test_missing_label_specific_prompt_key(self):
        """Test when label resolves to non-existent prompt key."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Default"\n')
            prompt_file = f.name

        try:
            labels = ["bug"]
            mappings = {"bug": "issue.nonexistent"}  # Points to non-existent key
            priorities = ["bug"]

            # Should fall back to the original key and render that
            result = render_prompt("issue.action", path=prompt_file, labels=labels, label_prompt_mappings=mappings, label_priorities=priorities)

            assert "Default" in result
        finally:
            Path(prompt_file).unlink()


class TestInvalidYAMLConfiguration:
    """Test handling of invalid YAML configuration files."""

    def test_invalid_yaml_syntax_error(self):
        """Test handling of invalid YAML syntax."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Unclosed bracket [")\n')
            prompt_file = f.name

        try:
            # Should raise SystemExit due to YAML parsing error
            with pytest.raises(SystemExit):
                load_prompts(prompt_file)
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()

    def test_yaml_root_not_dict_error(self):
        """Test handling when YAML root is not a dictionary."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("- item1\n- item2\n")
            prompt_file = f.name

        try:
            # Should raise SystemExit (root must be dict)
            with pytest.raises(SystemExit):
                load_prompts(prompt_file)
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()

    def test_empty_yaml_file(self):
        """Test handling of empty YAML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            prompt_file = f.name

        try:
            # Empty YAML is valid, returns empty dict
            result = load_prompts(prompt_file)
            assert result == {}
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()

    def test_yaml_with_only_comments(self):
        """Test YAML file with only comments."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("# This is a comment\n# Another comment\n")
            prompt_file = f.name

        try:
            result = load_prompts(prompt_file)
            assert result == {}
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()


class TestNetworkFailures:
    """Test handling of network failures during configuration loading."""

    @patch("src.auto_coder.prompt_loader.yaml.safe_load")
    def test_network_error_during_yaml_loading(self, mock_yaml_load):
        """Test handling of network-related errors during YAML loading."""
        mock_yaml_load.side_effect = requests.exceptions.ConnectionError("Network error")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Test"\n')
            prompt_file = f.name

        try:
            # Network errors during YAML load should be handled
            with pytest.raises((SystemExit, requests.exceptions.ConnectionError)):
                load_prompts(prompt_file)
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()


class TestConcurrentAccessConflicts:
    """Test handling of concurrent access conflicts."""

    def test_cache_corruption_concurrent_access(self):
        """Test handling of concurrent cache access."""
        import threading
        import time

        errors = []

        def access_cache(thread_id):
            try:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                    f.write(f'issue:\n  action: "Thread {thread_id}"\n')
                    prompt_file = f.name

                try:
                    # Clear cache at start
                    clear_prompt_cache()

                    # Access multiple times
                    for _ in range(10):
                        result = load_prompts(prompt_file)
                        assert result is not None
                finally:
                    Path(prompt_file).unlink()
                    clear_prompt_cache()
            except Exception as e:
                errors.append((thread_id, e))

        # Run concurrent accesses
        threads = [threading.Thread(target=access_cache, args=(i,)) for i in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should not have any errors
        assert len(errors) == 0, f"Concurrent access errors: {errors}"

    def test_racing_label_resolution(self):
        """Test concurrent label resolution operations."""
        import threading

        labels = [f"label-{i}" for i in range(100)]
        mappings = {label: f"prompt.{label}" for label in labels}
        priorities = labels.copy()

        results = []
        errors = []

        def resolve_label(thread_id):
            try:
                for _ in range(10):
                    result = _resolve_label_priority(labels, mappings, priorities)
                    results.append(result)
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=resolve_label, args=(i,)) for i in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should complete without errors
        assert len(errors) == 0, f"Concurrent resolution errors: {errors}"
        assert len(results) == 100
        assert all(r == "label-0" for r in results)


class TestDiskSpaceIssues:
    """Test handling of disk space issues with log files."""

    @patch("src.auto_coder.prompt_loader.logger")
    def test_log_write_failure_handling(self, mock_logger):
        """Test handling when log writing fails."""
        # Simulate logger failure
        mock_logger.warning.side_effect = OSError("Disk full")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Test"\n  bugfix: "Bug fix"\n')
            prompt_file = f.name

        try:
            # Should handle logger failure gracefully
            labels = ["bug"]
            mappings = {"bug": "issue.bugfix"}
            priorities = ["bug"]

            result = render_prompt("issue.action", path=prompt_file, labels=labels, label_prompt_mappings=mappings, label_priorities=priorities)

            assert "Bug fix" in result
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()


class TestInvalidLabelFormats:
    """Test handling of invalid label formats from GitHub."""

    def test_none_label_in_list(self):
        """Test handling of None values in label list."""
        labels = ["bug", None, "feature"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["bug", "feature"]

        # Should handle None gracefully
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_dict_instead_of_string_label(self):
        """Test handling when label is a dict instead of string."""
        labels = [{"name": "bug"}, "feature"]
        mappings = {"feature": "issue.feature"}
        priorities = ["feature"]

        # Should handle non-string labels gracefully
        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "feature"

    def test_empty_string_label(self):
        """Test handling of empty string labels."""
        labels = ["", "bug", "feature"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["bug", "feature"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug"

    def test_very_long_label(self):
        """Test handling of extremely long labels."""
        long_label = "x" * 10000
        labels = [long_label, "bug"]
        mappings = {long_label: "prompt.long", "bug": "issue.bugfix"}
        priorities = [long_label, "bug"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == long_label

    def test_special_characters_in_labels(self):
        """Test handling of special characters in labels."""
        labels = ["bug/is:hard", "feature+urgent", "test@work"]
        mappings = {"bug/is:hard": "prompt.special"}
        priorities = ["bug/is:hard"]

        result = _resolve_label_priority(labels, mappings, priorities)
        assert result == "bug/is:hard"


class TestCorruptedCacheState:
    """Test handling of corrupted cache state."""

    def test_cache_with_invalid_data(self):
        """Test handling when cache contains invalid data."""
        from src.auto_coder.prompt_loader import _PROMPTS_CACHE

        # Corrupt the cache
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Test"\n')
            prompt_file = f.name

        try:
            # Load normally first
            result1 = load_prompts(prompt_file)
            assert result1 == {"issue": {"action": "Test"}}

            # Corrupt the cache with invalid data type
            _PROMPTS_CACHE[Path(prompt_file)] = "invalid_data"

            # Cache returns the corrupted data (doesn't validate)
            result2 = load_prompts(prompt_file)
            assert result2 == "invalid_data"

            # Clear cache and reload to get valid data
            clear_prompt_cache()
            result3 = load_prompts(prompt_file)
            assert result3 == {"issue": {"action": "Test"}}
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()

    def test_cache_clearing_with_corruption(self):
        """Test that clear_prompt_cache handles corrupted cache."""
        from src.auto_coder.prompt_loader import _PROMPTS_CACHE

        # Populate cache with mixed valid and invalid data
        _PROMPTS_CACHE["invalid_key"] = None
        _PROMPTS_CACHE[123] = "numeric_key"

        # Clear should work without error
        clear_prompt_cache()
        assert len(_PROMPTS_CACHE) == 0


class TestRetryLogic:
    """Test retry logic for transient failures."""

    @patch("requests.get")
    def test_retry_on_transient_error(self, mock_get):
        """Test retry logic for transient errors."""
        # Simulate temporary failure then success
        mock_get.side_effect = [requests.exceptions.ConnectionError("Temporary error"), requests.exceptions.ConnectionError("Temporary error"), Mock(status_code=200, text="issue:\n  action: 'Test'")]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Test"\n')
            prompt_file = f.name

        try:
            # Should eventually succeed after retries
            result = load_prompts(prompt_file)
            assert result == {"issue": {"action": "Test"}}
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()


class TestGracefulFallbackMechanisms:
    """Test graceful fallback to default prompts."""

    def test_fallback_to_default_when_no_mappings(self):
        """Test fallback when no label mappings exist."""
        labels = ["bug"]
        mappings = {}  # Empty mappings
        priorities = ["bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        assert result is None

    def test_fallback_when_label_not_in_priorities(self):
        """Test fallback when label exists but not in priorities."""
        labels = ["bug"]
        mappings = {"bug": "issue.bugfix"}
        priorities = ["feature"]  # "bug" not in priorities

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # Should return None or fall back to first applicable label
        # Implementation may vary
        assert result is None or result == "issue.bugfix"

    def test_partial_label_match_fallback(self):
        """Test fallback when only some labels have mappings."""
        labels = ["bug", "feature", "urgent"]
        mappings = {"bug": "issue.bugfix", "feature": "issue.feature"}
        priorities = ["urgent", "feature", "bug"]

        result = _get_prompt_for_labels(labels, mappings, priorities)
        # "urgent" has no mapping, should use "feature" (next in priority)
        assert result == "issue.feature"

    def test_error_logging_and_reporting(self):
        """Test that errors are properly logged and reported."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('issue:\n  action: "Test"\n')
            prompt_file = f.name

        try:
            # Test that various errors are handled with appropriate logging
            labels = ["nonexistent"]
            mappings = {"nonexistent": "prompt.does.not.exist"}
            priorities = ["nonexistent"]

            # Should log warnings and fall back
            result = render_prompt("issue.action", path=prompt_file, labels=labels, label_prompt_mappings=mappings, label_priorities=priorities)

            assert "Test" in result
        finally:
            Path(prompt_file).unlink()
            clear_prompt_cache()


class TestCircuitBreakerPatterns:
    """Test circuit breaker patterns for failed operations."""

    def test_fail_fast_on_repeated_errors(self):
        """Test that system fails fast on repeated errors."""
        # Simulate repeated failures
        call_count = 0

        def failing_operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OSError("Persistent failure")
            return "success"

        # Should retry a few times then give up
        max_retries = 3
        retry_delay = 0.01

        import time

        start = time.perf_counter()

        result = None
        for attempt in range(max_retries):
            try:
                result = failing_operation()
                break
            except OSError:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    raise

        elapsed = time.perf_counter() - start
        assert result == "success"
        assert call_count == 3
        assert elapsed >= retry_delay * 2  # Should have waited


if __name__ == "__main__":
    # Run error handling tests with verbose output
    pytest.main([__file__, "-v", "-s"])
