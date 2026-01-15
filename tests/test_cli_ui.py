"""Tests for CLI UI helpers."""

import sys
from unittest.mock import MagicMock, patch

from src.auto_coder import cli_ui


@patch("time.sleep")
@patch("time.time")
def test_sleep_with_countdown_execution(mock_time, mock_sleep):
    """Test that sleep_with_countdown executes correctly."""
    # Create a mock stream
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    # Setup time.time to simulate progression
    # Initial time, then increments for each loop check
    start_time = 1000.0
    # The loop checks time.time() < end_time
    # We want it to run for a few iterations
    # Iterations: 0s, 0.1s, 0.2s ... 2.0s
    # We need to return enough values for the while loop

    # Logic:
    # 1. end_time = time.time() + seconds (1000 + 2 = 1002)
    # 2. while True:
    # 3. current_time = time.time() (1000.1) -> check < 1002
    # 4. ... sleep(0.1) ...

    # We can just make time.time return a sequence that eventually exceeds end_time
    # We need to control the loop carefully

    # Let's say we want 3 iterations:
    # 1. Setup: 1000.0
    # 2. Loop check 1: 1000.0
    # 3. Loop check 2: 1001.0
    # 4. Loop check 3: 1002.0 (break condition)

    mock_time.side_effect = [1000.0, 1000.0, 1001.0, 1002.0]

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called (at least once with 0.1)
    # With our side_effect, it should run 2 loop iterations
    # 1. 1000.0 < 1002.0 -> sleep(0.1)
    # 2. 1001.0 < 1002.0 -> sleep(0.1)
    # 3. 1002.0 >= 1002.0 -> break
    assert mock_sleep.call_count >= 1
    mock_sleep.assert_called_with(0.1)

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    # Should see the spinner char and message
    assert any("Sleeping..." in w for w in writes)
    assert any("remaining" in w for w in writes)


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


@patch("time.sleep")
@patch("time.time")
def test_sleep_with_countdown_custom_message(mock_time, mock_sleep):
    """Test that sleep_with_countdown accepts a custom message."""
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    # Run for 1 iteration
    mock_time.side_effect = [1000.0, 1000.0, 1002.0]

    cli_ui.sleep_with_countdown(1, stream=mock_stream, message="Waiting for API")

    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    assert any("Waiting for API..." in w for w in writes)
