"""Tests for CLI UI helpers."""

import sys
from unittest.mock import MagicMock, patch

from src.auto_coder import cli_ui


@patch("time.time")
@patch("time.sleep")
def test_sleep_with_countdown_execution(mock_sleep, mock_time):
    """Test that sleep_with_countdown executes correctly."""
    # Create a mock stream
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    # Setup time sequence
    # Initial time is 1000.0
    # sleep_with_countdown(2) called
    # end_time = 1002.0
    # Loop needs to run a few times
    # We simulate time passing by side_effect on time.time()

    # Sequence of time.time() calls:
    # 1. end_time = time.time() + seconds -> returns 1000.0
    # 2. while True: current_time = time.time() -> returns 1000.0
    # 3. while True: current_time = time.time() -> returns 1000.1
    # ...
    # N. while True: current_time = time.time() -> returns 1002.0 (break)

    start_time = 1000.0
    duration = 2

    # Generate side effects for time.time()
    # 1st call: start setup
    # Loop iterations...

    time_values = [start_time]  # for setup

    current = start_time
    while current <= start_time + duration:
        time_values.append(current)
        current += 0.1

    # Ensure the last value triggers break
    time_values.append(start_time + duration + 0.1)

    mock_time.side_effect = time_values

    cli_ui.sleep_with_countdown(duration, stream=mock_stream)

    # Check that time.sleep was called multiple times
    # It should be called roughly duration / 0.1 times
    assert mock_sleep.call_count >= duration * 10

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    assert any("Sleeping..." in w for w in writes)

    # Check that spinner was included (assuming default spinner)
    # The spinner uses unicode by default if NO_COLOR is not set
    # Mocking os.environ is tricky if not done via patch.dict, but let's check for spinner chars
    # SPINNER_FRAMES_UNICODE = ["⠋", "⠙", ...]
    # Just check if we see something that looks like a spinner or text
    assert any("⠋" in w or "|" in w for w in writes)


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
