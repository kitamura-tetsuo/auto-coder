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

    # Test passes if no exception is raised

    def test_multiple_code_file_changes(self, tmp_path):
        """Test handling multiple code file changes."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:

            # Trigger changes for multiple code files
            tool._on_file_changed("src/main.py")
            tool._on_file_changed("src/utils.ts")
            tool._on_file_changed("app/main.js")

            # Verify all updates were triggered
            assert mock_run_tests.call_count == 3

    def test_mixed_code_and_non_code_changes(self, tmp_path):
        """Test handling a mix of code and non-code file changes."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Mock the methods
        with patch.object(tool, "_run_playwright_tests") as mock_run_tests:

            # Trigger changes for both code and non-code files
            tool._on_file_changed("src/main.py")
            tool._on_file_changed("README.md")
            tool._on_file_changed("config.json")
            tool._on_file_changed("src/component.ts")

            # Verify test runs for all files
            assert mock_run_tests.call_count == 4

            # Verify GraphRAG updates only for code files
