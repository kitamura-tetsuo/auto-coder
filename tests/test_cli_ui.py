"""Tests for CLI UI helpers."""

import sys
from unittest.mock import MagicMock, patch

from src.auto_coder import cli_ui


@patch("time.sleep")
@patch("sys.stdout")
def test_sleep_with_countdown_execution(mock_stdout, mock_sleep):
    """Test that sleep_with_countdown executes correctly."""
    # Mock isatty to return True so we test the countdown logic
    mock_stdout.isatty.return_value = True

    cli_ui.sleep_with_countdown(2)

    # Check that time.sleep was called 2 times
    assert mock_sleep.call_count == 2

    # Check that stdout.write was called (for printing countdown and clearing)
    # 2 seconds = 2 updates + 2 clears/initial writes
    assert mock_stdout.write.called

    # Check output format
    # We expect writes to contain "Sleeping..."
    writes = [args[0] for args, _ in mock_stdout.write.call_args_list]
    assert any("Sleeping..." in w for w in writes)


@patch("time.sleep")
@patch("sys.stdout")
def test_sleep_with_countdown_non_interactive(mock_stdout, mock_sleep):
    """Test that sleep_with_countdown falls back to regular sleep in non-interactive mode."""
    # Mock isatty to return False
    mock_stdout.isatty.return_value = False

    cli_ui.sleep_with_countdown(5)

    # Should call time.sleep once with full duration
    mock_sleep.assert_called_once_with(5)

    # Should not write to stdout
    mock_stdout.write.assert_not_called()


@patch("time.sleep")
@patch("sys.stdout")
def test_sleep_with_countdown_zero_seconds(mock_stdout, mock_sleep):
    """Test that sleep_with_countdown returns immediately for 0 or negative seconds."""
    cli_ui.sleep_with_countdown(0)
    mock_sleep.assert_not_called()
    mock_stdout.write.assert_not_called()

    cli_ui.sleep_with_countdown(-5)
    mock_sleep.assert_not_called()
    mock_stdout.write.assert_not_called()
