"""Tests for CLI UI helpers."""

import sys
from unittest.mock import MagicMock, call, patch

from src.auto_coder import cli_ui


@patch("time.time")
@patch("time.sleep")
def test_sleep_with_countdown_execution(mock_sleep, mock_time):
    """Test that sleep_with_countdown executes correctly."""
    # Create a mock stream
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    # Setup time.time to simulate passage of time
    # Start at 0, then increment by 0.1 for a few steps, then jump to end
    start_time = 1000.0
    mock_time.side_effect = [start_time, start_time + 0.0, start_time + 0.1, start_time + 2.1]  # init start_time  # first loop check  # second loop check  # end loop check (past 2 seconds)

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called
    assert mock_sleep.called
    # It calls sleep(0.1) inside the loop
    mock_sleep.assert_has_calls([call(0.1)])

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    assert any("Sleeping..." in w for w in writes)


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
