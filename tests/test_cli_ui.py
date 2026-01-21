"""Tests for CLI UI helpers."""

import sys
import threading
import time
from unittest.mock import MagicMock, patch

from src.auto_coder import cli_ui


@patch("time.time")
@patch("time.sleep")
def test_sleep_with_countdown_execution(mock_sleep, mock_time):
    """Test that sleep_with_countdown executes correctly."""
    # Create a mock stream
    mock_stream = MagicMock()
    mock_stream.isatty.return_value = True

    # Setup time simulation
    current_time = [1000.0]

    def fake_time():
        return current_time[0]

    def fake_sleep(seconds):
        current_time[0] += seconds

    mock_time.side_effect = fake_time
    mock_sleep.side_effect = fake_sleep

    cli_ui.sleep_with_countdown(2, stream=mock_stream)

    # Check that time.sleep was called appropriately
    # 2 seconds total, 0.1s interval => ~20 calls
    assert mock_sleep.call_count >= 19
    mock_sleep.assert_called_with(0.1)

    # Check that stream.write was called
    assert mock_stream.write.called

    # Check output format
    writes = [args[0] for args, _ in mock_stream.write.call_args_list]
    assert any("Sleeping..." in w for w in writes)

    # Verify spinner presence (at least one frame)
    # Since we didn't set NO_COLOR, it should use unicode frames
    # ⠋ is the first frame
    assert any("⠋" in w for w in writes)


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


def test_print_lock_error_formatting():
    """Test that print_lock_error formats output correctly."""
    lock_info = MagicMock()
    lock_info.pid = 12345
    lock_info.hostname = "test-host"
    lock_info.started_at = "2023-01-01T12:00:00"

    with patch("click.secho") as mock_secho, patch("click.echo") as mock_echo, patch("click.style") as mock_style:
        # Test without NO_COLOR
        with patch.dict("os.environ", {}, clear=True):
            cli_ui.print_lock_error(lock_info, is_running=True)

            # Verify colorful calls
            assert mock_secho.call_count > 0
            # Verify "Lock Information" header is printed
            assert any("Lock Information" in str(args) for args, _ in mock_secho.call_args_list)

        # Test with NO_COLOR
        mock_secho.reset_mock()
        mock_echo.reset_mock()
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            cli_ui.print_lock_error(lock_info, is_running=False)

            # Verify no colorful calls via secho (it might still be called if implemented that way, but checking logic)
            # In our implementation we used explicit checks
            assert mock_secho.call_count == 0
            # Verify standard echo used
            assert mock_echo.call_count > 0
            # Verify status message
            assert any("stale lock" in str(args) for args, _ in mock_echo.call_args_list)


def test_spinner_execution():
    """Test that Spinner executes correctly."""
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = True

        # Test basic execution
        # Use a short delay to ensure multiple frames might be printed
        spinner = cli_ui.Spinner("Test Loading", delay=0.001)

        with spinner:
            # Wait a bit to let the thread run
            time.sleep(0.01)

        # Verify writes occurred
        assert mock_stdout.write.called
        writes = [args[0] for args, _ in mock_stdout.write.call_args_list]

        # Should verify spinner presence (at least one frame)
        # Unicode frames by default
        assert any("⠋" in w or "Test Loading" in w for w in writes)

        # Should clear line at end
        # The last write or second to last should be the clear command
        assert any(w.startswith("\r") and w.endswith("\r") for w in writes)

        # Should print final status message
        assert any("✅ Test Loading" in w for w in writes)
        assert mock_stdout.flush.called


def test_spinner_no_color():
    """Test that Spinner respects NO_COLOR."""
    with patch("sys.stdout") as mock_stdout, patch("click.style") as mock_style, patch.dict("os.environ", {"NO_COLOR": "1"}):

        mock_stdout.isatty.return_value = True

        spinner = cli_ui.Spinner("Test Loading", delay=0.001)

        with spinner:
            time.sleep(0.01)

        # Should NOT call click.style when NO_COLOR is set
        mock_style.assert_not_called()

        # Writes should still happen
        assert mock_stdout.write.called
        writes = [args[0] for args, _ in mock_stdout.write.call_args_list]
        # Should use ASCII frames (e.g. "|")
        assert any("|" in w or "/" in w for w in writes)

        # Should print final status with [OK]
        assert any("[OK] Test Loading" in w for w in writes)


def test_spinner_non_interactive():
    """Test Spinner in non-interactive mode."""
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False

        with cli_ui.Spinner("Test Loading"):
            pass

        # Should verify simple print
        mock_stdout.write.assert_called_with("Test Loading\n")
