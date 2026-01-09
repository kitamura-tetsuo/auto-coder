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

    # Setup time sequence
    # 1. end_time calculation: time.time() returns 1000
    # 2. Loop 1 check: time.time() returns 1000. Remaining: 2.
    # 3. Loop 2 check: time.time() returns 1000.1. Remaining: 1.9.
    # ... we don't want to specify 20 values.
    # Let's just simulate a few iterations to verify spinner updates.

    start = 1000.0
    # Side effect sequence:
    # 1. init: 1000.0
    # 2. loop 1: 1000.0 (rem=2.0) -> sleep(0.1)
    # 3. loop 2: 1000.1 (rem=1.9) -> sleep(0.1)
    # 4. loop 3: 1002.1 (rem=-0.1) -> break
    mock_time.side_effect = [start, start, start + 0.1, start + 2.1]

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called 2 times
    assert mock_sleep.call_count == 2

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    # Check for spinner characters (assuming color is enabled/disabled appropriately)
    # Since we didn't mock os.environ, it depends on actual env.
    # But we can look for "Sleeping..."
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
