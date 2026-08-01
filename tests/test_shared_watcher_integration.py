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

            # Verify GraphRAG updates only for code files (py and ts)
