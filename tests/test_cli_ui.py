"""Tests for CLI UI helpers."""

import sys
from unittest.mock import MagicMock, patch, call

from src.auto_coder import cli_ui


@patch("time.sleep")
@patch("time.time")
def test_sleep_with_countdown_execution(mock_time, mock_sleep):
    """Test that sleep_with_countdown executes correctly."""
    # Create a mock stream
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    # Setup time sequence:
    # start, start+0.1, start+0.2, ... start+2.0
    start_time = 1000.0
    duration = 2.0
    interval = 0.1
    steps = int(duration / interval)

    # We need to provide enough return values for time.time()
    # It's called once for start_time
    # Then inside the loop: current_time = time.time()
    # If using side_effect with a generator or iterator
    times = [start_time] + [start_time + (i * interval) for i in range(steps + 5)]
    mock_time.side_effect = times

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called multiple times (approx 20 times for 2 seconds with 0.1 interval)
    assert mock_sleep.call_count >= 20

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    assert any("Sleeping..." in w for w in writes)

    # Check that spinner characters are rotating
    # The spinner chars might be colored (control codes), so we check basic presence or just multiple writes
    assert len(writes) >= 20

    # Verify spinner rotation (heuristic)
    # Check if we see different spinner chars if we strip ANSI codes (simplification)
    # Or just check if the strings change
    unique_writes = set(writes)
    # Even if time string "2s" is same, spinner should change
    assert len(unique_writes) > 1


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
