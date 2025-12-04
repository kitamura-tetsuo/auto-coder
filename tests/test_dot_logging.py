"""
Tests for dot format logging in CommandExecutor.

This module tests the dot format logging feature where dots are printed
instead of full log lines when dot_format=True and verbose mode is disabled.
"""

import sys
from io import StringIO
from unittest.mock import Mock, call, patch

import pytest

from src.auto_coder import utils


class TestDotLogging:
    """Test cases for dot format logging in CommandExecutor."""

    @pytest.fixture
    def _use_real_commands(self):
        """Marker fixture to indicate test needs real command execution."""
        pass

    @pytest.fixture
    def mock_logger_opt(self):
        """Mock the logger.opt().info() chain."""
        with patch("src.auto_coder.utils.logger") as mock_logger:
            mock_opt = Mock()
            mock_info = Mock()
            mock_opt.info = mock_info
            mock_logger.opt = Mock(return_value=mock_opt)
            yield mock_logger

    def test_dot_format_true_verbose_false(self, monkeypatch, _use_real_commands, mock_logger_opt):
        """Test CommandExecutor with dot_format=True and verbose=False prints dots."""
        # Ensure verbose mode is disabled
        monkeypatch.delenv("AUTOCODER_VERBOSE", raising=False)

        # Create a command that produces some output
        command = [
            sys.executable,
            "-c",
            "import sys; print('line1'); print('line2'); print('line3')",
        ]

        # Create a StringIO to capture direct print statements to stderr
        original_stderr = sys.stderr
        captured_stderr = StringIO()

        try:
            # Temporarily replace stderr to capture dots
            sys.stderr = captured_stderr
            result = utils.CommandExecutor.run_command(
                command,
                timeout=5,
                stream_output=True,
                dot_format=True,
            )
        finally:
            sys.stderr = original_stderr

        # Verify command succeeded
        assert result.success is True
        assert "line1" in result.stdout
        assert "line2" in result.stdout
        assert "line3" in result.stdout

        # Verify dots were printed (should have 3 dots for 3 lines)
        stderr_content = captured_stderr.getvalue()
        # Count dots - should be 3 (one for each line)
        assert stderr_content.count(".") == 3, f"Expected 3 dots, got: {repr(stderr_content)}"

        # Verify logger.opt was NOT called for the actual output lines
        # (It might be called for other debug/info messages, but that's not the focus of this test)
        # The key is that dots were printed to stderr instead of logging the full lines

    def test_dot_format_true_verbose_true(self, monkeypatch, _use_real_commands, mock_logger_opt):
        """Test CommandExecutor with dot_format=True and verbose=True logs full lines."""
        # Enable verbose mode
        monkeypatch.setenv("AUTOCODER_VERBOSE", "1")

        # Create a command that produces some output
        command = [
            sys.executable,
            "-c",
            "import sys; print('line1'); print('line2'); print('line3')",
        ]

        # Run with dot_format=True and verbose=True
        result = utils.CommandExecutor.run_command(
            command,
            timeout=5,
            stream_output=True,
            dot_format=True,
        )

        # Verify command succeeded
        assert result.success is True
        assert "line1" in result.stdout
        assert "line2" in result.stdout
        assert "line3" in result.stdout

        # Verify logger.opt().info() was called with the actual log lines (not dots)
        # The logger.opt should be called with depth=2
        # We expect multiple calls because each line triggers a log call
        assert mock_logger_opt.opt.call_count >= 3  # At least 3 calls for 3 lines

        # Verify that logger.opt was called with depth=2 for each line
        calls = mock_logger_opt.opt.call_args_list
        for call_args in calls:
            # Each call should have depth=2
            assert "depth=2" in str(call_args)

    def test_dot_format_false(self, monkeypatch, _use_real_commands, mock_logger_opt):
        """Test CommandExecutor with dot_format=False logs full lines."""
        # Ensure verbose mode is disabled
        monkeypatch.delenv("AUTOCODER_VERBOSE", raising=False)

        # Create a command that produces some output
        command = [
            sys.executable,
            "-c",
            "import sys; print('line1'); print('line2'); print('line3')",
        ]

        # Run with dot_format=False
        result = utils.CommandExecutor.run_command(
            command,
            timeout=5,
            stream_output=True,
            dot_format=False,
        )

        # Verify command succeeded
        assert result.success is True
        assert "line1" in result.stdout
        assert "line2" in result.stdout
        assert "line3" in result.stdout

        # Verify logger.opt().info() was called with the actual log lines (not dots)
        # The logger.opt should be called with depth=2
        assert mock_logger_opt.opt.call_count >= 3  # At least 3 calls for 3 lines

        # Verify that logger.opt was called with depth=2 for each line
        calls = mock_logger_opt.opt.call_args_list
        for call_args in calls:
            # Each call should have depth=2
            assert "depth=2" in str(call_args)
