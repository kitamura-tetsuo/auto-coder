"""
Tests for Shared Watcher: Integration between Test Watcher and GraphRAG.
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


class TestSharedWatcherIntegration:
    """Test shared watcher integration between Test Watcher and GraphRAG."""

    def test_test_watcher_unchanged_behavior(self, tmp_path):
        """Verify Test Watcher still works as before."""
        # Setup test environment
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Create a code file
        code_file = tmp_path / "test.py"
        code_file.write_text("def hello(): pass")

        with patch.object(watcher, "_run_playwright_tests") as mock_run_tests:
            # Start watching
            watcher.start_watching()

            # Simulate file change
            watcher._on_file_changed(str(code_file))

            # Wait for async processing
            time.sleep(0.1)

            # Verify E2E tests are triggered
            mock_run_tests.assert_called()

            watcher.stop_watching()

    def test_graphrag_update_triggered(self, tmp_path):
        """Verify GraphRAG updates are triggered for code files."""
        # Mock GraphRAG manager
        mock_manager = MagicMock()
        mock_manager.update_index.return_value = True

        # MagicMock has all attributes by default, so hasattr will return True
        # We need to set up smart_update_trigger to return True as well
        mock_manager.smart_update_trigger.return_value = True

        with patch(
            "auto_coder.graphrag_index_manager.GraphRAGIndexManager",
            return_value=mock_manager,
        ):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create a Python file
            code_file = tmp_path / "test.py"
            code_file.write_text("def hello(): pass")

            # Trigger file change
            watcher._trigger_graphrag_update(str(code_file))

            # Verify GraphRAG update was called (either smart_update_trigger or update_index)
            # The enhanced version will call smart_update_trigger if available
            assert mock_manager.smart_update_trigger.called or mock_manager.update_index.called

    def test_file_type_filtering(self, tmp_path):
        """Verify GraphRAG updates are triggered for all file types when explicitly called."""
        # Use Mock spec to prevent hasattr from always returning True
        from unittest.mock import Mock

        mock_manager = Mock(spec=["update_index"])
        mock_manager.update_index.return_value = True

        with patch(
            "auto_coder.graphrag_index_manager.GraphRAGIndexManager",
            return_value=mock_manager,
        ):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create non-code file
            readme_file = tmp_path / "README.md"
            readme_file.write_text("# Test Project")

            # Create code file
            code_file = tmp_path / "test.py"
            code_file.write_text("def hello(): pass")

            # Trigger updates for both files
            watcher._trigger_graphrag_update(str(readme_file))
            watcher._trigger_graphrag_update(str(code_file))

            # Verify both triggered GraphRAG updates
            # Note: _trigger_graphrag_update doesn't filter by file type
            # The filtering happens in _on_file_changed
            assert mock_manager.update_index.call_count == 2

    def test_file_type_filtering_in_on_file_changed(self, tmp_path):
        """Verify that _on_file_changed filters non-code files for GraphRAG updates."""
        mock_manager = MagicMock()

        with patch(
            "auto_coder.graphrag_index_manager.GraphRAGIndexManager",
            return_value=mock_manager,
        ):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            with patch.object(watcher, "_run_playwright_tests") as mock_test:
                with patch.object(watcher, "_trigger_graphrag_update") as mock_graphrag:
                    # Create non-code file
                    readme_file = tmp_path / "README.md"
                    readme_file.write_text("# Test Project")

                    # Trigger file change
                    watcher._on_file_changed(str(readme_file))

                    # Test run should be called
                    mock_test.assert_called()

                    # GraphRAG update should NOT be called for non-code files
                    mock_graphrag.assert_not_called()

                    # Create code file
                    code_file = tmp_path / "test.py"
                    code_file.write_text("def hello(): pass")

                    # Reset mocks
                    mock_test.reset_mock()
                    mock_graphrag.reset_mock()

                    # Trigger file change
                    watcher._on_file_changed(str(code_file))

                    # Both should be called for code files
                    mock_test.assert_called()
                    mock_graphrag.assert_called()

    def test_smart_update_integration(self, tmp_path):
        """Test that smart update logic is used when available."""
        # Use Mock spec to prevent hasattr from always returning True
        from unittest.mock import Mock

        mock_manager = Mock(spec=["update_index"])
        mock_manager.update_index.return_value = True

        with patch(
            "auto_coder.graphrag_index_manager.GraphRAGIndexManager",
            return_value=mock_manager,
        ):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create a code file
            code_file = tmp_path / "test.py"
            code_file.write_text("def hello(): pass")

            # Trigger file change
            watcher._trigger_graphrag_update(str(code_file))

            # Verify that update_index was called (the fallback when smart_update_trigger doesn't exist)
            mock_manager.update_index.assert_called_once()

    def test_smart_update_integration_with_smart_trigger(self, tmp_path):
        """Test that smart update logic is used when available on the manager."""
        mock_manager = MagicMock()
        # Make the manager appear to have smart_update_trigger
        mock_manager.smart_update_trigger = MagicMock(return_value=True)
        mock_manager.update_index = MagicMock(return_value=True)

        with patch(
            "auto_coder.graphrag_index_manager.GraphRAGIndexManager",
            return_value=mock_manager,
        ):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create a code file
            code_file = tmp_path / "test.py"
            code_file.write_text("def hello(): pass")

            # Trigger file change
            watcher._trigger_graphrag_update(str(code_file))

            # Verify smart_update_trigger was called when available
            mock_manager.smart_update_trigger.assert_called_once()

    def test_code_file_detection_python(self, tmp_path):
        """Test that Python files are correctly identified as code files."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        assert watcher._is_code_file("test.py")
        assert watcher._is_code_file("src/main.py")
        assert watcher._is_code_file("module/submodule/file.py")

    def test_code_file_detection_typescript(self, tmp_path):
        """Test that TypeScript files are correctly identified as code files."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        assert watcher._is_code_file("test.ts")
        assert watcher._is_code_file("src/component.ts")
        assert watcher._is_code_file("app/components/Button.ts")

    def test_code_file_detection_javascript(self, tmp_path):
        """Test that JavaScript files are correctly identified as code files."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        assert watcher._is_code_file("test.js")
        assert watcher._is_code_file("src/script.js")
        assert watcher._is_code_file("app/main.js")

    def test_code_file_detection_non_code(self, tmp_path):
        """Test that non-code files are correctly identified."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        assert not watcher._is_code_file("README.md")
        assert not watcher._is_code_file("config.json")
        assert not watcher._is_code_file("data.txt")
        assert not watcher._is_code_file("image.png")
        assert not watcher._is_code_file(".gitignore")
        assert not watcher._is_code_file("package-lock.json")

    def test_concurrent_test_and_graphrag_execution(self, tmp_path):
        """Test that test execution and GraphRAG updates can run concurrently."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        call_order = []

        def mock_run_tests(last_failed):
            call_order.append("test_run")

        def mock_graphrag_update(file_path):
            call_order.append("graphrag_update")

        with patch.object(watcher, "_run_playwright_tests", side_effect=mock_run_tests), patch.object(watcher, "_trigger_graphrag_update", side_effect=mock_graphrag_update):

            # Trigger file change
            watcher._on_file_changed("src/main.py")

            # Give threads time to start
            time.sleep(0.05)

            # Both methods should have been called
            assert "test_run" in call_order
            assert "graphrag_update" in call_order

    def test_multiple_code_file_changes(self, tmp_path):
        """Test handling multiple code file changes."""
        # Use Mock spec to prevent hasattr from always returning True
        from unittest.mock import Mock

        mock_manager = Mock(spec=["update_index"])
        mock_manager.update_index.return_value = True

        with patch(
            "auto_coder.graphrag_index_manager.GraphRAGIndexManager",
            return_value=mock_manager,
        ):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create multiple code files
            files = [
                tmp_path / "src/main.py",
                tmp_path / "src/utils.ts",
                tmp_path / "app/main.js",
            ]
            for f in files:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("test")

            # Trigger changes for multiple code files
            for file_path in files:
                watcher._trigger_graphrag_update(str(file_path))

            # Verify all updates were triggered
            assert mock_manager.update_index.call_count == 3

    def test_mixed_code_and_non_code_changes(self, tmp_path):
        """Test handling a mix of code and non-code file changes."""
        # Use Mock spec to prevent hasattr from always returning True
        from unittest.mock import Mock

        mock_manager = Mock(spec=["update_index"])
        mock_manager.update_index.return_value = True

        with patch(
            "auto_coder.graphrag_index_manager.GraphRAGIndexManager",
            return_value=mock_manager,
        ):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            # Create test files
            py_file = tmp_path / "src/main.py"
            py_file.parent.mkdir(parents=True, exist_ok=True)
            py_file.write_text("test")

            readme_file = tmp_path / "README.md"
            readme_file.write_text("# Test")

            config_file = tmp_path / "config.json"
            config_file.write_text("{}")

            ts_file = tmp_path / "src/component.ts"
            ts_file.parent.mkdir(parents=True, exist_ok=True)
            ts_file.write_text("test")

            # Trigger updates for all files
            watcher._trigger_graphrag_update(str(py_file))
            watcher._trigger_graphrag_update(str(readme_file))
            watcher._trigger_graphrag_update(str(config_file))
            watcher._trigger_graphrag_update(str(ts_file))

            # Verify GraphRAG updates were triggered for all files
            # (The _trigger_graphrag_update method doesn't filter by file type)
            assert mock_manager.update_index.call_count == 4

    def test_mixed_code_and_non_code_changes_filtered(self, tmp_path):
        """Test handling a mix of code and non-code file changes with _on_file_changed."""
        mock_manager = MagicMock()

        with patch(
            "auto_coder.graphrag_index_manager.GraphRAGIndexManager",
            return_value=mock_manager,
        ):
            watcher = TestWatcherTool(project_root=str(tmp_path))

            with patch.object(watcher, "_run_playwright_tests") as mock_test:
                with patch.object(watcher, "_trigger_graphrag_update") as mock_graphrag:
                    # Create test files
                    py_file = tmp_path / "src/main.py"
                    py_file.parent.mkdir(parents=True, exist_ok=True)
                    py_file.write_text("test")

                    readme_file = tmp_path / "README.md"
                    readme_file.write_text("# Test")

                    config_file = tmp_path / "config.json"
                    config_file.write_text("{}")

                    ts_file = tmp_path / "src/component.ts"
                    ts_file.parent.mkdir(parents=True, exist_ok=True)
                    ts_file.write_text("test")

                    # Trigger file changes for all files (this uses _on_file_changed)
                    watcher._on_file_changed(str(py_file))
                    watcher._on_file_changed(str(readme_file))
                    watcher._on_file_changed(str(config_file))
                    watcher._on_file_changed(str(ts_file))

                    # Verify test runs for all files
                    assert mock_test.call_count == 4

                    # Verify GraphRAG updates only for code files (py and ts)
                    assert mock_graphrag.call_count == 2

    def test_enhanced_debounce_integration(self, tmp_path):
        """Test that enhanced debouncing reduces redundant GraphRAG updates."""
        watcher = TestWatcherTool(project_root=str(tmp_path))

        # Check if enhanced debouncing is available
        if not hasattr(watcher, "_enhanced_debounce_files"):
            # Skip test if method is not available in this version
            pytest.skip("Enhanced debouncing not available in this version")

        # Create test files in different directories
        files = [
            "src/main.py",
            "src/utils.py",
            "src/components/Button.ts",
            "tests/test_main.py",
            "tests/test_utils.py",
        ]

        # Apply enhanced debouncing
        debounced = watcher._enhanced_debounce_files(files)

        # Should reduce the number of files by grouping
        # At most one file per directory
        assert len(debounced) <= len({Path(f).parent for f in files})

        # Should still include representatives from key directories
        dir_names = {Path(f).parts[0] for f in debounced}
        assert "src" in dir_names
        assert "tests" in dir_names
