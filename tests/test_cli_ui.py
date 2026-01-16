"""Tests for CLI UI helpers."""

import sys
from unittest.mock import MagicMock, patch

from src.auto_coder import cli_ui


@patch("src.auto_coder.cli_ui.time")
def test_sleep_with_countdown_execution(mock_time):
    """Test that sleep_with_countdown executes correctly."""
    # Create a mock stream
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    # Setup time simulation
    current_time = [1000.0]

    def sleep_side_effect(seconds):
        current_time[0] += seconds

    mock_time.sleep.side_effect = sleep_side_effect
    mock_time.time.side_effect = lambda: current_time[0]

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called approx 20 times (2s / 0.1s)
    # Allow small margin for floating point
    assert 19 <= mock_time.sleep.call_count <= 21

    # Verify sleep duration
    mock_time.sleep.assert_called_with(0.1)

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]

    # Check for "Sleeping..."
    assert any("Sleeping..." in w for w in writes)

    # Check for spinner frame (assuming default unicode/color behavior in test env,
    # but patch might affect NO_COLOR check? NO_COLOR is os.environ check)
    # The default behavior mocks isatty=True.

    # We can check for at least one spinner frame
    spinner_frames = cli_ui.SPINNER_FRAMES_UNICODE
    assert any(any(frame in w for frame in spinner_frames) for w in writes)


@patch("src.auto_coder.cli_ui.time")
def test_sleep_with_countdown_non_interactive(mock_time):
    """Test that sleep_with_countdown falls back to regular sleep in non-interactive mode."""
    # Create a mock stream that returns False for isatty
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = False

    cli_ui.sleep_with_countdown(5, stream=mock_stream)

    # Should call time.sleep once with full duration
    mock_time.sleep.assert_called_once_with(5)

    # Should not write to stream
    mock_stream.write.assert_not_called()


@patch("src.auto_coder.cli_ui.time")
def test_sleep_with_countdown_zero_seconds(mock_time):
    """Test that sleep_with_countdown returns immediately for 0 or negative seconds."""
    mock_stream = MagicMock()

    cli_ui.sleep_with_countdown(0, stream=mock_stream)
    mock_time.sleep.assert_not_called()
    mock_stream.write.assert_not_called()

    cli_ui.sleep_with_countdown(-5, stream=mock_stream)
    mock_time.sleep.assert_not_called()
    mock_stream.write.assert_not_called()
