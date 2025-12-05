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

    def test_dot_positioning_with_ansi_escape_sequences_tty(self, monkeypatch, _use_real_commands):
        """Test that dots are printed with ANSI escape sequences when stderr is a TTY."""
        # Ensure verbose mode is disabled
        monkeypatch.delenv("AUTOCODER_VERBOSE", raising=False)

        # Create a command that produces some output
        command = [
            sys.executable,
            "-c",
            "import sys; print('line1'); print('line2'); print('line3')",
        ]

        # Create a mock stderr that simulates a TTY
        original_stderr = sys.stderr
        captured_stderr = StringIO()

        # Mock isatty to return True
        captured_stderr.isatty = Mock(return_value=True)

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

        # Verify ANSI escape sequences are present in the output
        stderr_content = captured_stderr.getvalue()

        # Check for cursor save/restore and move up sequences
        assert "\033[s" in stderr_content, "Expected cursor save sequence \\033[s"
        assert "\033[1A" in stderr_content, "Expected cursor move up sequence \\033[1A"
        assert "\033[u" in stderr_content, "Expected cursor restore sequence \\033[u"

        # Verify dots are printed
        assert "." in stderr_content, "Expected dots to be printed"

    def test_dot_positioning_non_tty_fallback(self, monkeypatch, _use_real_commands):
        """Test graceful fallback when stderr is not a TTY."""
        # Ensure verbose mode is disabled
        monkeypatch.delenv("AUTOCODER_VERBOSE", raising=False)

        # Create a command that produces some output
        command = [
            sys.executable,
            "-c",
            "import sys; print('line1'); print('line2')",
        ]

        # Create a mock stderr that is NOT a TTY
        original_stderr = sys.stderr
        captured_stderr = StringIO()

        # Mock isatty to return False
        captured_stderr.isatty = Mock(return_value=False)

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

        # Verify basic dot printing still works (without ANSI sequences)
        stderr_content = captured_stderr.getvalue()
        assert "." in stderr_content, "Expected dots to be printed"

        # Verify ANSI escape sequences are NOT present in non-TTY mode
        assert "\033[s" not in stderr_content, "Did not expect cursor save sequence in non-TTY mode"
        assert "\033[1A" not in stderr_content, "Did not expect cursor move up sequence in non-TTY mode"

    def test_dot_positioning_with_progress_stage_footer(self, monkeypatch, _use_real_commands):
        """Test that dots interact correctly with ProgressStage footer."""
        # Ensure verbose mode is disabled
        monkeypatch.delenv("AUTOCODER_VERBOSE", raising=False)

        # Import ProgressStage for testing interaction
        from src.auto_coder.progress_footer import ProgressStage, clear_progress

        # Create a command that produces some output
        command = [
            sys.executable,
            "-c",
            "import sys; print('line1'); print('line2')",
        ]

        # Create a mock stderr that simulates a TTY
        original_stderr = sys.stderr
        captured_stderr = StringIO()
        captured_stderr.isatty = Mock(return_value=True)

        try:
            # Set up a progress stage
            with ProgressStage("Issue", 1119, "Testing dots"):
                # Temporarily replace stderr to capture dots
                sys.stderr = captured_stderr

                # Run command with dot_format=True
                result = utils.CommandExecutor.run_command(
                    command,
                    timeout=5,
                    stream_output=True,
                    dot_format=True,
                )

                # Restore stderr before exiting context
                sys.stderr = original_stderr
        finally:
            sys.stderr = original_stderr
            clear_progress()

        # Verify command succeeded
        assert result.success is True

        # Verify that dots were printed with ANSI sequences
        stderr_content = captured_stderr.getvalue()
        assert "." in stderr_content, "Expected dots to be printed"

        # Verify ANSI escape sequences are present (cursor positioning)
        assert "\033[s" in stderr_content or "\033[1A" in stderr_content, "Expected ANSI sequences for cursor positioning"
