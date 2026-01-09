"""Tests for CLI UI helpers."""

import sys
import time
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

    # We expect the following sequence of time.time() calls:
    # 1. start_time = time.time() (Initialization)
    # 2. current_time = time.time() (Loop 1 check) -> returns start_time (0s elapsed)
    # 3. current_time = time.time() (Loop 2 check) -> returns start_time + 1.1 (1.1s elapsed)
    # 4. current_time = time.time() (Loop 3 check) -> returns start_time + 2.1 (2.1s elapsed, break)

    mock_time.side_effect = [start_time, start_time, start_time + 1.1, start_time + 2.1]  # init  # loop 1  # loop 2  # loop 3 (exit)

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called
    assert mock_sleep.called

    # Verify it slept for small intervals (0.1s)
    # It should have slept twice (once per loop iteration before the break)
    assert call(0.1) in mock_sleep.call_args_list
    assert mock_sleep.call_count == 2

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
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
