"""
Tests for Shared Watcher: Core Integration of Test Watcher and GraphRAG.
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


class TestSharedWatcherCore:
    """Test the core integration between Test Watcher and GraphRAG."""

    def test_is_code_file_python(self, tmp_path):
        """Test that Python files are correctly identified as code files."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        assert tool._is_code_file("test.py")
        assert tool._is_code_file("src/main.py")
        assert tool._is_code_file("module/submodule/file.py")

    def test_is_code_file_typescript(self, tmp_path):
        """Test that TypeScript files are correctly identified as code files."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        assert tool._is_code_file("test.ts")
        assert tool._is_code_file("src/component.ts")
        assert tool._is_code_file("app/components/Button.ts")

    def test_is_code_file_javascript(self, tmp_path):
        """Test that JavaScript files are correctly identified as code files."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        assert tool._is_code_file("test.js")
        assert tool._is_code_file("src/script.js")
        assert tool._is_code_file("app/main.js")

    def test_is_code_file_non_code(self, tmp_path):
        """Test that non-code files are correctly identified."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        assert not tool._is_code_file("README.md")
        assert not tool._is_code_file("config.json")
        assert not tool._is_code_file("data.txt")
        assert not tool._is_code_file("image.png")
        assert not tool._is_code_file(".gitignore")
        assert not tool._is_code_file("package-lock.json")

    def test_on_file_changed_code_file(self, tmp_path):
        """Test that code file changes trigger both test run and GraphRAG update."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods to avoid actually running them
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests, patch.object(tool, "_trigger_graphrag_update") as mock_graphrag:

            # Trigger file change for a Python file
            tool._on_file_changed("src/main.py")

            # Verify both methods were called
            mock_run_tests.assert_called_once_with(True)
            mock_graphrag.assert_called_once_with("src/main.py")

    def test_on_file_changed_non_code_file(self, tmp_path):
        """Test that non-code file changes only trigger test run."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods to avoid actually running them
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests, patch.object(tool, "_trigger_graphrag_update") as mock_graphrag:

            # Trigger file change for a non-code file
            tool._on_file_changed("README.md")

            # Verify only test run was called, not GraphRAG
            mock_run_tests.assert_called_once_with(True)
            mock_graphrag.assert_not_called()

    def test_trigger_graphrag_update_success(self, tmp_path):
        """Test that GraphRAG update is triggered successfully."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock GraphRAGIndexManager - use Mock spec to prevent hasattr from always returning True
        from unittest.mock import Mock

        mock_manager = Mock(spec=["update_index"])
        mock_manager.update_index.return_value = True

        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", return_value=mock_manager):
            # Trigger GraphRAG update
            tool._trigger_graphrag_update("src/main.py")

            # Verify the update was called
            mock_manager.update_index.assert_called_once()

    def test_trigger_graphrag_update_failure(self, tmp_path):
        """Test that GraphRAG update failures are handled gracefully."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock GraphRAGIndexManager to raise an exception
        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", side_effect=Exception("Test error")):
            # Trigger GraphRAG update - should not raise an exception
            tool._trigger_graphrag_update("src/main.py")

            # Test passes if no exception is raised

    def test_trigger_graphrag_update_returns_false(self, tmp_path):
        """Test that GraphRAG update handles False return value gracefully."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock GraphRAGIndexManager - use Mock spec to prevent hasattr from always returning True
        from unittest.mock import Mock

        mock_manager = Mock(spec=["update_index"])
        mock_manager.update_index.return_value = False

        with patch("auto_coder.graphrag_index_manager.GraphRAGIndexManager", return_value=mock_manager):
            # Trigger GraphRAG update
            tool._trigger_graphrag_update("src/main.py")

            # Verify the update was called even if it returns False
            mock_manager.update_index.assert_called_once()

    def test_on_file_changed_threading_non_blocking(self, tmp_path):
        """Test that file changes are handled asynchronously and don't block."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        call_order = []

        # Mock methods to track call order
        def mock_run_tests(last_failed):
            call_order.append("test_run")
            time.sleep(0.1)  # Simulate test execution time

        def mock_graphrag_update(file_path):
            call_order.append("graphrag_update")
            time.sleep(0.1)  # Simulate update time

        with patch.object(tool, "_run_playwright_tests", side_effect=mock_run_tests), patch.object(tool, "_trigger_graphrag_update", side_effect=mock_graphrag_update):

            # Trigger file change
            tool._on_file_changed("src/main.py")

            # Give threads time to start
            time.sleep(0.05)

            # Both methods should have been called (or at least started)
            # Note: We can't guarantee the exact order due to thread scheduling,
            # but both should be in the call order
            assert "test_run" in call_order
            assert "graphrag_update" in call_order

    def test_multiple_code_file_changes(self, tmp_path):
        """Test handling multiple code file changes."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests, patch.object(tool, "_trigger_graphrag_update") as mock_graphrag:

            # Trigger changes for multiple code files
            tool._on_file_changed("src/main.py")
            tool._on_file_changed("src/utils.ts")
            tool._on_file_changed("app/main.js")

            # Verify all updates were triggered
            assert mock_run_tests.call_count == 3
            assert mock_graphrag.call_count == 3
            mock_graphrag.assert_any_call("src/main.py")
            mock_graphrag.assert_any_call("src/utils.ts")
            mock_graphrag.assert_any_call("app/main.js")

    def test_mixed_code_and_non_code_changes(self, tmp_path):
        """Test handling a mix of code and non-code file changes."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests, patch.object(tool, "_trigger_graphrag_update") as mock_graphrag:

            # Trigger changes for both code and non-code files
            tool._on_file_changed("src/main.py")
            tool._on_file_changed("README.md")
            tool._on_file_changed("config.json")
            tool._on_file_changed("src/component.ts")

            # Verify test runs for all files
            assert mock_run_tests.call_count == 4

            # Verify GraphRAG updates only for code files
            assert mock_graphrag.call_count == 2
            mock_graphrag.assert_any_call("src/main.py")
            mock_graphrag.assert_any_call("src/component.ts")


class TestLightweightUpdateCheck:
    """Test the lightweight update check functionality."""

    def test_lightweight_update_check_has_code_changes(self, tmp_path):
        """Test that lightweight check detects code changes."""
        from auto_coder.graphrag_index_manager import GraphRAGIndexManager

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Mock subprocess.run to simulate git ls-files returning code files
        with patch("auto_coder.graphrag_index_manager.subprocess.run") as mock_run:
            # Simulate git ls-files returning Python files
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "src/main.py\nsrc/utils.py\ntests/test.py\nREADME.md"
            mock_run.return_value = mock_result

            # Should detect code changes
            result = manager._has_recent_code_changes()
            assert result is True

    def test_lightweight_update_check_no_code_changes(self, tmp_path):
        """Test that lightweight check returns False when only non-code files exist."""
        from auto_coder.graphrag_index_manager import GraphRAGIndexManager

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Create only non-code files
        readme = tmp_path / "README.md"
        readme.write_text("# Test Project")

        config = tmp_path / "config.json"
        config.write_text("{}")

        # Initialize git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "README.md", "config.json"], cwd=tmp_path, capture_output=True)

        # Should not detect code changes
        assert manager._has_recent_code_changes() is False

    def test_lightweight_update_check_git_failure(self, tmp_path):
        """Test that lightweight check handles git failures gracefully."""
        from auto_coder.graphrag_index_manager import GraphRAGIndexManager

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Mock subprocess.run to simulate git command failure
        with patch("auto_coder.graphrag_index_manager.subprocess.run") as mock_run:
            mock_run.side_effect = Exception("git: command not found")

            # Should return True (assume there are code changes when git fails)
            result = manager._has_recent_code_changes()
            assert result is True

    @patch("auto_coder.graphrag_index_manager.subprocess.run")
    def test_lightweight_update_check_subprocess_exception(self, mock_run, tmp_path):
        """Test that lightweight check handles subprocess exceptions."""
        from auto_coder.graphrag_index_manager import GraphRAGIndexManager

        manager = GraphRAGIndexManager(repo_path=str(tmp_path))

        # Mock subprocess to raise an exception
        mock_run.side_effect = Exception("Git not found")

        # Should return True (assume there are code changes)
        assert manager._has_recent_code_changes() is True
