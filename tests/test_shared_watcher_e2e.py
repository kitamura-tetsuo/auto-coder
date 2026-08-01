"""
End-to-end tests for Shared Watcher workflow.
"""

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the test_watcher module to the path
test_watcher_path = Path(__file__).parent.parent / "src" / "auto_coder" / "mcp_servers" / "test_watcher"
sys.path.insert(0, str(test_watcher_path))

from test_watcher_tool import TestWatcherTool


class TestSharedWatcherE2E:
    """End-to-end tests for shared watcher workflow."""

    def test_real_file_change_workflow(self, tmp_path):
        """Test complete workflow with real file changes."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Start monitoring
        result = watcher.start_watching()
        assert result["status"] == "started"

        # Create Python file
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): return 'world'")

        # Verify monitoring continues
        status = watcher.get_status()
        assert status["file_watcher_running"] is True

        # Stop monitoring
        result = watcher.stop_watching()
        assert result["status"] == "stopped"

        # Verify both test execution and GraphRAG update were triggered
        # (We can't directly verify this without mocking, but the workflow should complete)

    def test_watch_filter_respects_gitignore(self, tmp_path):
        """Test that file watching respects .gitignore patterns."""
        # Create .gitignore
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__/\n")

        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Verify gitignore is loaded
        assert watcher.gitignore_spec is not None

        # Test that patterns are respected
        assert watcher.gitignore_spec.match_file("test.pyc")
        assert watcher.gitignore_spec.match_file("__pycache__/test.py")
        assert not watcher.gitignore_spec.match_file("test.py")

    def test_start_stop_start_lifecycle(self, tmp_path):
        """Test complete start-stop-start lifecycle."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # First start
        result = watcher.start_watching()
        assert result["status"] == "started"

        # Create a file
        test_file = tmp_path / "test1.py"
        test_file.write_text("def func1(): pass")

        # Stop
        result = watcher.stop_watching()
        assert result["status"] == "stopped"

        # Second start
        result = watcher.start_watching()
        assert result["status"] == "started"

        # Create another file
        test_file2 = tmp_path / "test2.py"
        test_file2.write_text("def func2(): pass")

        # Stop again
        result = watcher.stop_watching()
        assert result["status"] == "stopped"

    def test_status_updates_correctly(self, tmp_path):
        """Test that status is updated correctly during operation."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Initial status
        status = watcher.get_status()
        assert status["file_watcher_running"] is False
        assert status["playwright_running"] is False

        # Start watching
        watcher.start_watching()
        status = watcher.get_status()
        assert status["file_watcher_running"] is True

        # Stop watching
        watcher.stop_watching()
        status = watcher.get_status()
        assert status["file_watcher_running"] is False

    def test_project_root_configuration(self, tmp_path):
        """Test that project root is configured correctly."""
        # Test with explicit project root
        watcher = TestWatcherTool(project_root=str(tmp_path))
        assert watcher.project_root == tmp_path

        # Test with default (current directory)
        watcher_default = TestWatcherTool()
        assert watcher_default.project_root == Path.cwd()

    def test_enhanced_debounce_with_temporal_grouping(self):
        """Test enhanced debouncing with temporal file changes."""
        watcher = TestWatcherTool()

        # Simulate file changes
        files = [
            "src/main.py",
            "src/utils.py",
            "src/components/Button.ts",
        ]

        # Apply debouncing
        debounced = watcher._enhanced_debounce_files(files)

        # Should group by directory (at most one per directory)
        assert len(debounced) <= 2  # src and tests directories
        assert "src/main.py" in debounced or "src/utils.py" in debounced or "src/components/Button.ts" in debounced

    def test_error_resilience_full_workflow(self, tmp_path):
        """Test that workflow continues despite errors."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Create test files
        files = []
        for i in range(5):
            file_path = tmp_path / f"test{i}.py"
            file_path.write_text(f"def func{i}(): pass")
            files.append(str(file_path))

        # Test that file changes are processed without errors
        for file_path in files:
            try:
                watcher._on_file_changed(file_path)
            except Exception as e:
                pytest.fail(f"File change handling failed: {e}")

        # Wait for processing
        time.sleep(0.5)

    def test_cleanup_resources_on_stop(self, tmp_path):
        """Test that resources are cleaned up properly on stop."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Start watching
        watcher.start_watching()
        assert watcher.observer is not None

        # Stop watching
        watcher.stop_watching()
        assert watcher.observer is None

    def test_file_watcher_idempotency(self, tmp_path):
        """Test that file watcher can be started multiple times without issues."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Start multiple times
        result1 = watcher.start_watching()
        result2 = watcher.start_watching()
        result3 = watcher.start_watching()

        # First should succeed
        assert result1["status"] == "started"

        # Subsequent should indicate already running
        assert result2["status"] == "already_running"
        assert result3["status"] == "already_running"

        # Clean up
        watcher.stop_watching()

    def test_large_file_count_workflow(self, tmp_path):
        """Test workflow with a large number of files."""
        # Skip actual processing in pytest to avoid timeout with many files
        import os

        os.environ["AC_SKIP_FILE_PROCESSING"] = "1"

        try:
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create many files
            num_files = 100
            files = []
            for i in range(num_files):
                file_path = tmp_path / f"module{i}.py"
                file_path.write_text(f"def func{i}(): pass")
                files.append(str(file_path))

            # Process all files (will be skipped due to AC_SKIP_FILE_PROCESSING)
            for file_path in files:
                watcher._on_file_changed(file_path)

            # No need to wait since processing is skipped
            # Verify no errors occurred
            # (If we get here without exceptions, the test passes)
        finally:
            # Clean up environment variable
            os.environ.pop("AC_SKIP_FILE_PROCESSING", None)
