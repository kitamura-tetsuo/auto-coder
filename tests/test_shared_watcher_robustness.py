"""
Tests for Shared Watcher: Robustness and Error Handling.
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Add the test_watcher module to the path
test_watcher_path = Path(__file__).parent.parent / "src" / "auto_coder" / "mcp_servers" / "test_watcher"
sys.path.insert(0, str(test_watcher_path))

from test_watcher_tool import TestWatcherTool


class TestConcurrentModifications:
    """Test handling of concurrent file modifications."""

    def test_concurrent_code_file_changes(self, tmp_path):
        """Test that concurrent file changes are handled correctly."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:
            # Trigger concurrent file changes
            tool._on_file_changed("src/main.py")
            tool._on_file_changed("src/utils.py")
            tool._on_file_changed("src/component.ts")

            # Verify all were processed
            assert mock_run_tests.call_count == 3

    def test_rapid_file_changes_dont_block(self, tmp_path):
        """Test that rapid file changes don't cause blocking."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:
            start_time = time.time()

            # Trigger many file changes rapidly
            for i in range(10):
                tool._on_file_changed(f"src/file{i}.py")

            elapsed = time.time() - start_time

            # Should complete quickly (less than 1 second)
            assert elapsed < 1.0

            # All should have been called
            assert mock_run_tests.call_count == 10


class TestEdgeCases:
    """Test edge cases and error scenarios."""

    def test_empty_file_path(self, tmp_path):
        """Test handling of empty file paths."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:
            # Should handle empty path gracefully
            tool._on_file_changed("")

            assert mock_run_tests.call_count == 1

    def test_special_characters_in_path(self, tmp_path):
        """Test handling of special characters in file paths."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:
            tool._on_file_changed("path/with spaces/file.py")
            tool._on_file_changed("path/with-unicode/файл.py")

            assert mock_run_tests.call_count == 2

    def test_very_long_path(self, tmp_path):
        """Test handling of very long file paths."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:
            long_path = "src/" + "a" * 200 + ".py"
            tool._on_file_changed(long_path)

            assert mock_run_tests.call_count == 1
