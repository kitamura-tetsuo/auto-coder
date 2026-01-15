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
    start_time = 1000.0
    duration = 2

    # The loop calls time.time() once per iteration.
    # We want to increment time by roughly 0.1 each iteration to match sleep(0.1)

    def time_generator():
        current = start_time
        yield current  # for end_time calc
        while True:
            yield current  # for current_time = time.time()
            current += 0.100001

    mock_time.side_effect = time_generator()

    cli_ui.sleep_with_countdown(duration, stream=mock_stream)

    # Check that time.sleep was called roughly duration * 10 times
    # We sleep 0.1s each time.
    expected_sleeps = int(duration * 10)
    # Allow for off-by-one errors due to float precision
    # With 2 seconds, expected is 20.
    # The loop runs until current >= end_time.
    # 0, 0.1, ..., 1.9 -> 20 iterations. 2.0 -> breaks.
    assert abs(mock_sleep.call_count - expected_sleeps) <= 2

    # Verify sleep amount is 0.1
    mock_sleep.assert_called_with(0.1)

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    assert any("Sleeping..." in w for w in writes)

    # Check that spinner frames are used (checking for at least one unicode frame)
    assert any(any(frame in w for frame in cli_ui.SPINNER_FRAMES_UNICODE) for w in writes)


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
