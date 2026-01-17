"""Tests for CLI UI helpers."""

import sys
from unittest.mock import MagicMock, patch

from src.auto_coder import cli_ui


@patch("time.sleep")
def test_sleep_with_countdown_execution(mock_sleep):
    """Test that sleep_with_countdown executes correctly."""
    # Create a mock stream
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called 20 times (10Hz for 2 seconds)
    assert mock_sleep.call_count == 20

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    assert any("Sleeping..." in w for w in writes)

    # Check that spinner is present
    all_spinners = cli_ui.SPINNER_FRAMES_UNICODE + cli_ui.SPINNER_FRAMES_ASCII
    has_spinner = False
    for w in writes:
        for s in all_spinners:
            if s in w:
                has_spinner = True
                break
        if has_spinner:
            break
    assert has_spinner


@patch("time.sleep")
def test_sleep_with_countdown_non_interactive(mock_sleep):
    """Test that sleep_with_countdown falls back to regular sleep in non-interactive mode."""
    # Create a mock stream that returns False for isatty
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = False

    cli_ui.sleep_with_countdown(5, stream=mock_stream)

    # Should call time.sleep once with full duration
    mock_sleep.assert_called_once_with(5)

    # Should not write to stream
    mock_stream.write.assert_not_called()


@patch("time.sleep")
def test_sleep_with_countdown_zero_seconds(mock_sleep):
    """Test that sleep_with_countdown returns immediately for 0 or negative seconds."""
    mock_stream = MagicMock()

    cli_ui.sleep_with_countdown(0, stream=mock_stream)
    mock_sleep.assert_not_called()
    mock_stream.write.assert_not_called()

    cli_ui.sleep_with_countdown(-5, stream=mock_stream)
    mock_sleep.assert_not_called()
    mock_stream.write.assert_not_called()
