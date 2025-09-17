import pytest
from unittest.mock import Mock, patch

from src.auto_coder.automation_engine import AutomationEngine


def test_detects_real_conflict_block():
    text = (
        "line a\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> feature-branch\n"
        "line z\n"
    )
    assert AutomationEngine._has_conflict_block_in_text(text) is True


def test_ignores_markers_not_at_line_start():
    text = (
        "some text <<<<<<< not a marker ======= and >>>>>>> not either\n"
        "another line\n"
    )
    assert AutomationEngine._has_conflict_block_in_text(text) is False


def test_ignores_isolated_markers_without_triad():
    # Has a start but no mid/end
    text1 = "<<<<<<< HEAD\njust text\nno end here\n"
    # Has mid and end but no start
    text2 = "some\n=======\n>>>>>> something\n"
    assert AutomationEngine._has_conflict_block_in_text(text1) is False
    assert AutomationEngine._has_conflict_block_in_text(text2) is False


def test_ignores_coverage_like_html():
    # Simulate coverage HTML where angle brackets are usually escaped or not at start
    text = (
        "<pre><code>&lt;&lt;&lt;&lt;&lt;&lt;&lt; HEAD\n"
        "code sample\n"
        "=======\n"
        "&gt;&gt;&gt;&gt;&gt;&gt;&gt; feature-branch</code></pre>\n"
    )
    assert AutomationEngine._has_conflict_block_in_text(text) is False


class TestConflictMarkerScanImproved:
    """Test cases for improved conflict marker scanning that only scans tracked files."""

    @patch('src.auto_coder.automation_engine.CommandExecutor.run_command')
    def test_get_unmerged_files_success(self, mock_run_command):
        """Test successful retrieval of unmerged files."""
        mock_run_command.return_value = Mock(
            success=True,
            stdout="UU file1.py\nM  file2.py\nUU file3.js\n"
        )

        engine = AutomationEngine(Mock(), Mock())
        result = engine._get_unmerged_files()

        assert result == ['file1.py', 'file3.js']
        mock_run_command.assert_called_once_with(['git', 'status', '--porcelain'])

    @patch('src.auto_coder.automation_engine.CommandExecutor.run_command')
    def test_get_unmerged_files_empty(self, mock_run_command):
        """Test when no unmerged files exist."""
        mock_run_command.return_value = Mock(
            success=True,
            stdout="M  file1.py\nA  file2.py\n"
        )

        engine = AutomationEngine(Mock(), Mock())
        result = engine._get_unmerged_files()

        assert result == []

    @patch('src.auto_coder.automation_engine.CommandExecutor.run_command')
    def test_get_tracked_files_success(self, mock_run_command):
        """Test successful retrieval of tracked files."""
        mock_run_command.return_value = Mock(
            success=True,
            stdout="src/file1.py\nsrc/file2.js\nREADME.md\n"
        )

        engine = AutomationEngine(Mock(), Mock())
        result = engine._get_tracked_files()

        assert result == ['src/file1.py', 'src/file2.js', 'README.md']
        mock_run_command.assert_called_once_with(['git', 'ls-files'])

    @patch('src.auto_coder.automation_engine.CommandExecutor.run_command')
    def test_get_tracked_files_failure(self, mock_run_command):
        """Test when git ls-files fails."""
        mock_run_command.return_value = Mock(success=False)

        engine = AutomationEngine(Mock(), Mock())
        result = engine._get_tracked_files()

        assert result == []

    @patch('builtins.open')
    @patch('src.auto_coder.automation_engine.AutomationEngine._get_unmerged_files')
    @patch('src.auto_coder.automation_engine.AutomationEngine._get_tracked_files')
    def test_scan_conflict_markers_prefers_unmerged_files(self, mock_get_tracked, mock_get_unmerged, mock_open):
        """Test that scan prefers unmerged files when available."""
        # Setup: unmerged files exist
        mock_get_unmerged.return_value = ['conflict_file.py']
        mock_get_tracked.return_value = ['conflict_file.py', 'normal_file.py']

        # Mock file content with conflict markers
        mock_open.return_value.__enter__.return_value.read.return_value = (
            "line 1\n"
            "<<<<<<< HEAD\n"
            "our change\n"
            "=======\n"
            "their change\n"
            ">>>>>>> branch\n"
            "line 2\n"
        )

        engine = AutomationEngine(Mock(), Mock())
        result = engine._scan_conflict_markers()

        # Should only scan unmerged files, not all tracked files
        assert result == ['conflict_file.py']
        mock_get_unmerged.assert_called_once()
        mock_get_tracked.assert_not_called()

    @patch('builtins.open')
    @patch('src.auto_coder.automation_engine.AutomationEngine._get_unmerged_files')
    @patch('src.auto_coder.automation_engine.AutomationEngine._get_tracked_files')
    def test_scan_conflict_markers_fallback_to_tracked(self, mock_get_tracked, mock_get_unmerged, mock_open):
        """Test fallback to tracked files when no unmerged files exist."""
        # Setup: no unmerged files, fallback to tracked files
        mock_get_unmerged.return_value = []
        mock_get_tracked.return_value = ['some_file.py']

        # Mock file content without conflict markers
        mock_open.return_value.__enter__.return_value.read.return_value = "normal content\n"

        engine = AutomationEngine(Mock(), Mock())
        result = engine._scan_conflict_markers()

        # Should fallback to scanning all tracked files
        assert result == []
        mock_get_unmerged.assert_called_once()
        mock_get_tracked.assert_called_once()

    @patch('builtins.open')
    @patch('src.auto_coder.automation_engine.AutomationEngine._get_unmerged_files')
    @patch('src.auto_coder.automation_engine.AutomationEngine._get_tracked_files')
    def test_scan_conflict_markers_skips_binary_files(self, mock_get_tracked, mock_get_unmerged, mock_open):
        """Test that binary files are skipped by extension."""
        mock_get_unmerged.return_value = []
        mock_get_tracked.return_value = ['image.png', 'document.pdf', 'code.py']

        # Mock file content for the .py file
        mock_open.return_value.__enter__.return_value.read.return_value = "print('hello')\n"

        engine = AutomationEngine(Mock(), Mock())
        result = engine._scan_conflict_markers()

        # Should only try to open the .py file, not binary files
        assert result == []
        # open should only be called once for code.py
        mock_open.assert_called_once_with('code.py', 'r', encoding='utf-8', errors='ignore')

