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

    # Setup time sequence: start, then increments
    start_time = 1000.0
    # Simulate time passing in 0.1s increments for 2 seconds = 20 iterations
    # But wait, logic is: remaining = end_time - time.time()
    # end_time = 1000 + 2 = 1002
    # loops:
    # 1. time=1000.0 -> rem=2.0 -> sleep(0.1)
    # 2. time=1000.1 -> rem=1.9 -> sleep(0.1)
    # ...
    # 21. time=1002.0 -> rem=0 -> break

    # We need to provide side_effects for time.time()
    # Initial call is end_time = time.time() + seconds
    # Then inside loop: time.time()

    # So: call 1 (start): 1000
    # Loop 1: 1000 (remaining=2)
    # Loop 2: 1000.1
    # ...

    ticks = [start_time + (i * 0.1) for i in range(30)]  # Provide enough ticks
    mock_time.side_effect = ticks

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called approx 20 times (2s / 0.1s)
    # Allow small margin of error due to loop logic
    assert 18 <= mock_sleep.call_count <= 22

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    # Check for spinner chars and text
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
