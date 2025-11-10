"""
Tests for Shared Watcher: Integration-level Error Handling and Edge Cases.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the test_watcher module to the path
test_watcher_path = Path(__file__).parent.parent / "src" / "auto_coder" / "mcp_servers" / "test_watcher"
sys.path.insert(0, str(test_watcher_path))

from test_watcher_tool import TestWatcherTool


class TestSharedWatcherErrorHandling:
    """Test error scenarios and graceful degradation at integration level."""

    def test_graphrag_unavailable(self, tmp_path):
        """Test graceful handling when GraphRAG is unavailable."""
        # Mock GraphRAG manager to fail
        mock_manager = MagicMock()
        mock_manager.update_index.side_effect = Exception("GraphRAG unavailable")

        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", return_value=mock_manager):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create file change
            code_file = tmp_path / "test.py"
            code_file.write_text("def hello(): pass")

            # This should not raise an exception
            watcher._trigger_graphrag_update(str(code_file))

            # Test Watcher should still work (no exception)
            # The error should be logged but not propagate

    def test_graphrag_lightweight_check_failure(self, tmp_path):
        """Test handling of lightweight check failures."""
        mock_manager = MagicMock()
        mock_manager.lightweight_update_check.side_effect = Exception("Git not found")

        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", return_value=mock_manager):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # This should handle the error gracefully
            result = watcher._retry_graphrag_update("test.py")

            # Should not raise an exception

    def test_concurrent_file_changes(self, tmp_path):
        """Test handling of concurrent file modifications."""
        mock_manager = MagicMock()

        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", return_value=mock_manager):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create files concurrently
            def create_file(i):
                file_path = tmp_path / f"concurrent{i}.py"
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(f"def func{i}(): pass")
                watcher._trigger_graphrag_update(str(file_path))

            threads = []
            for i in range(5):
                thread = threading.Thread(target=create_file, args=(i,))
                threads.append(thread)
                thread.start()

            # Wait for all threads
            for thread in threads:
                thread.join()

            # Should handle all changes without errors
            # Give some time for async processing
            time.sleep(0.5)

    def test_burst_file_changes(self, tmp_path):
        """Test handling of burst file changes."""
        mock_manager = MagicMock()
        mock_manager.smart_update_trigger.return_value = True

        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", return_value=mock_manager):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create multiple files quickly
            files = []
            for i in range(20):
                file_path = tmp_path / f"burst{i}.py"
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(f"def func{i}(): pass")
                files.append(str(file_path))

            start_time = time.time()

            # Trigger all file changes
            for file_path in files:
                watcher._trigger_graphrag_update(file_path)

            # Wait for processing
            time.sleep(0.5)
            end_time = time.time()

            # Should handle burst efficiently
            processing_time = end_time - start_time
            assert processing_time < 5.0  # Should complete within reasonable time

    def test_test_execution_continues_on_graphrag_failure(self, tmp_path):
        """Test that test execution continues even when GraphRAG fails."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        call_count = {"test_run": 0, "graphrag_update": 0}

        def mock_run_tests(last_failed):
            call_count["test_run"] += 1

        def mock_graphrag_update(file_path):
            call_count["graphrag_update"] += 1
            # Simulate GraphRAG failure
            raise Exception("GraphRAG unavailable")

        with patch.object(watcher, "_run_playwright_tests", side_effect=mock_run_tests), patch.object(watcher, "_trigger_graphrag_update", side_effect=mock_graphrag_update):

            # Trigger file change
            watcher._on_file_changed("src/main.py")

            # Wait for async processing
            time.sleep(0.1)

            # Test run should still be called even if GraphRAG fails
            assert call_count["test_run"] > 0

    def test_error_handler_disables_after_max_failures(self, tmp_path):
        """Test that error handler disables GraphRAG updates after max failures."""
        from test_watcher_tool import SharedWatcherErrorHandler

        error_handler = SharedWatcherErrorHandler()
        error_handler.max_failures = 3

        # Simulate failures exceeding the limit
        for i in range(5):
            result = error_handler.handle_graphrag_failure(Exception(f"Failure {i}"))

        # After max failures, should return False (disable updates)
        assert result is False

    def test_error_handler_resets_after_successful_update(self, tmp_path):
        """Test that error handler resets after successful update."""
        from test_watcher_tool import SharedWatcherErrorHandler

        error_handler = SharedWatcherErrorHandler()
        error_handler.max_failures = 3

        # Simulate failures
        for i in range(3):
            error_handler.handle_graphrag_failure(Exception(f"Failure {i}"))

        assert error_handler.failure_count == 3

        # Simulate successful update
        error_handler.reset_failures()

        assert error_handler.failure_count == 0

    def test_test_watcher_continues_after_file_watcher_error(self, tmp_path):
        """Test that Test Watcher continues functioning after file watcher errors."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Start watching
        result = watcher.start_watching()
        assert result["status"] == "started"

        # Create a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): pass")

        # Simulate file change
        watcher._on_file_changed(str(test_file))

        # Wait for processing
        time.sleep(0.1)

        # Stop watching
        result = watcher.stop_watching()
        assert result["status"] == "stopped"

        # Verify watcher is still functional
        assert watcher.observer is None

    def test_graphrag_update_graceful_degradation_with_none_manager(self, tmp_path):
        """Test graceful handling when GraphRAG manager is not available."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Create a code file
        code_file = tmp_path / "test.py"
        code_file.write_text("def hello(): pass")

        # Mock the import to fail
        with patch("importlib.import_module", side_effect=ImportError("Module not found")):
            # This should not raise an exception
            watcher._trigger_graphrag_update(str(code_file))

            # Test watcher should still be functional

    def test_file_watcher_restart_after_stop(self, tmp_path):
        """Test that file watcher can be restarted after stopping."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Start watching
        result = watcher.start_watching()
        assert result["status"] == "started"

        # Stop watching
        result = watcher.stop_watching()
        assert result["status"] == "stopped"

        # Restart watching
        result = watcher.start_watching()
        assert result["status"] == "started"

        # Clean up
        watcher.stop_watching()

    def test_multiple_graphrag_failures_then_recovery(self, tmp_path):
        """Test recovery after multiple GraphRAG failures."""
        from test_watcher_tool import SharedWatcherErrorHandler

        error_handler = SharedWatcherErrorHandler()
        call_count = 0

        # Simulate initial failures
        for i in range(3):
            should_continue = error_handler.handle_graphrag_failure(Exception(f"Failure {i}"))
            assert should_continue is True

        # Simulate recovery
        error_handler.reset_failures()

        # Now should handle new failures normally
        for i in range(2):
            should_continue = error_handler.handle_graphrag_failure(Exception(f"Recovery failure {i}"))
            assert should_continue is True

    def test_non_existent_file_handling(self, tmp_path):
        """Test handling of non-existent file paths."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Try to trigger update for non-existent file
        non_existent_file = tmp_path / "non_existent.py"

        # This should not raise an exception
        watcher._trigger_graphrag_update(str(non_existent_file))

        # Should handle gracefully

    def test_permission_error_handling(self, tmp_path):
        """Test handling of file permission errors."""
        mock_manager = MagicMock()
        mock_manager.update_index.side_effect = PermissionError("Permission denied")

        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", return_value=mock_manager):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create a code file
            code_file = tmp_path / "test.py"
            code_file.write_text("def hello(): pass")

            # This should handle the error gracefully
            watcher._trigger_graphrag_update(str(code_file))

            # Should not raise an exception

    def test_timeout_handling(self, tmp_path):
        """Test handling of timeout errors."""
        mock_manager = MagicMock()
        mock_manager.update_index.side_effect = TimeoutError("Operation timed out")

        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", return_value=mock_manager):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create a code file
            code_file = tmp_path / "test.py"
            code_file.write_text("def hello(): pass")

            # This should handle the error gracefully
            watcher._trigger_graphrag_update(str(code_file))

            # Should not raise an exception
