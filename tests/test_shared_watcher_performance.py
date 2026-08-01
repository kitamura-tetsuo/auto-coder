"""
Tests for Shared Watcher: Performance and Optimization.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the test_watcher module to the path
test_watcher_path = Path(__file__).parent.parent / "src" / "auto_coder" / "mcp_servers" / "test_watcher"
sys.path.insert(0, str(test_watcher_path))

from test_watcher_tool import TestWatcherTool


class TestEnhancedDebouncingPerformance:
    """Test the performance benefits of enhanced debouncing."""

    def test_enhanced_debounce_groups_by_directory(self):
        """Test that enhanced debouncing groups files by directory."""
        tool = TestWatcherTool()

        # Create test files in different directories
        files = [
            "src/main.py",
            "src/utils.py",
            "src/components/Button.ts",
            "tests/test_main.py",
            "tests/test_utils.py",
            "docs/readme.md",
        ]

        # Apply enhanced debouncing
        debounced = tool._enhanced_debounce_files(files)

        # Should reduce the number of files by grouping
        # At most one file per directory
        assert len(debounced) <= len(set(Path(f).parent for f in files))

        # Should still include representatives from each directory
        directories = set(Path(f).parent for f in debounced)
        assert Path("src").parent in directories or Path("src") in directories
        assert Path("tests").parent in directories or Path("tests") in directories
        assert Path("docs").parent in directories or Path("docs") in directories

    def test_enhanced_debounce_skips_recent_files(self):
        """Test that enhanced debouncing skips recently seen files."""
        tool = TestWatcherTool()

        # Add a file to recent changes
        tool._recent_file_changes["src/main.py"] = time.time()

        # Try to debounce the same file
        files = ["src/main.py", "src/utils.py"]

        # Apply enhanced debouncing
        debounced = tool._enhanced_debounce_files(files)

        # Main.py should be skipped as it was recently seen
        assert "src/main.py" not in debounced
        assert "src/utils.py" in debounced

    def test_enhanced_debounce_cleans_old_entries(self):
        """Test that enhanced debouncing cleans up old entries."""
        tool = TestWatcherTool()

        # Add old entry
        old_time = time.time() - (tool._enhancement_window + 1)
        tool._recent_file_changes["src/old.py"] = old_time

        # Add new entry
        tool._recent_file_changes["src/new.py"] = time.time()

        # Trigger cleanup
        files = ["src/old.py", "src/new.py"]
        debounced = tool._enhanced_debounce_files(files)

        # Old file should be included (cleaned up), new file should be skipped
        assert "src/old.py" in debounced
        assert "src/new.py" not in debounced

    def test_enhanced_debounce_performance_with_many_files(self):
        """Test performance with many files."""
        tool = TestWatcherTool()

        # Create many files in different directories
        files = []
        for i in range(100):
            files.append(f"src/module{i}/file.py")
            files.append(f"tests/test{i}.py")
            files.append(f"docs/doc{i}.md")

        start_time = time.time()

        # Apply enhanced debouncing
        debounced = tool._enhanced_debounce_files(files)

        elapsed = time.time() - start_time

        # Should complete quickly
        assert elapsed < 0.1  # Less than 100ms

        # Should significantly reduce the number of files
        assert len(debounced) < len(files) / 2

    def test_enhanced_debounce_preserves_at_least_one_file_per_directory(self):
        """Test that at least one file is preserved from each directory."""
        tool = TestWatcherTool()

        # Create files in different directories
        files = [
            "src/a.py",
            "src/b.py",
            "src/c.py",
            "tests/x.py",
            "tests/y.py",
        ]

        # Apply enhanced debouncing
        debounced = tool._enhanced_debounce_files(files)

        # Should have at least one file from src and one from tests
        src_files = [f for f in debounced if f.startswith("src/")]
        tests_files = [f for f in debounced if f.startswith("tests/")]

        assert len(src_files) >= 1
        assert len(tests_files) >= 1


class TestPerformanceImpact:
    """Test overall performance impact on test execution."""

    def test_file_watcher_performance_impact(self, tmp_path):
        """Test that file watcher has minimal performance impact."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods to avoid actual work
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:

            # Create test files
            test_files = [f"src/file{i}.py" for i in range(10)]

            start_time = time.time()

            # Trigger changes for all files
            for file_path in test_files:
                tool._on_file_changed(file_path)

            elapsed = time.time() - start_time

            # Should complete quickly (less than 1 second)
            assert elapsed < 1.0

            # All files should have been processed
            assert mock_run_tests.call_count == 10

    def test_enhanced_debouncing_reduces_updates(self, tmp_path):
        """Test that enhanced debouncing reduces unnecessary updates."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:

            # Create files in the same directory
            files_in_same_dir = [f"src/file{i}.py" for i in range(5)]

            start_time = time.time()

            # Trigger changes rapidly
            for file_path in files_in_same_dir:
                tool._on_file_changed(file_path)
                time.sleep(0.01)  # Small delay to allow debouncing

            elapsed = time.time() - start_time

            # Should complete quickly due to debouncing
            assert elapsed < 0.5

    def test_burst_file_changes_handled_efficiently(self, tmp_path):
        """Test that burst file changes are handled efficiently."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:

            # Simulate a burst of file changes
            burst_files = [f"src/module{i}/file.py" for i in range(20)]

            start_time = time.time()

            # Trigger all changes
            for file_path in burst_files:
                tool._on_file_changed(file_path)

            elapsed = time.time() - start_time

            # Should complete within reasonable time despite many files
            assert elapsed < 2.0

    def test_performance_with_concurrent_modifications(self, tmp_path):
        """Test performance with concurrent modifications."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:

            # Create a mix of files in different directories
            files = []
            for i in range(50):
                if i % 3 == 0:
                    files.append(f"src/module{i}/file.py")
                elif i % 3 == 1:
                    files.append(f"tests/test{i}.py")
                else:
                    files.append(f"docs/doc{i}.md")

            start_time = time.time()

            # Trigger all changes
            for file_path in files:
                tool._on_file_changed(file_path)

            elapsed = time.time() - start_time

            # Should handle efficiently
            assert elapsed < 3.0


class TestMemoryEfficiency:
    """Test that the optimizations don't cause memory issues."""

    def test_recent_file_changes_cleanup(self, tmp_path):
        """Test that recent file changes are properly cleaned up."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Add many files to recent changes
        for i in range(1000):
            if i < tool._enhancement_window * 10:  # Recent entries
                tool._recent_file_changes[f"file{i}.py"] = time.time()
            else:  # Old entries
                tool._recent_file_changes[f"file{i}.py"] = time.time() - (tool._enhancement_window + 1)

        # Verify count
        assert len(tool._recent_file_changes) > 500

        # Trigger cleanup
        files = ["new_file.py"]
        tool._enhanced_debounce_files(files)

        # Old entries should be cleaned up
        assert len(tool._recent_file_changes) < 1000
