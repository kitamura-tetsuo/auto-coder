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
    # ⣾ is the first frame of the new spinner
    assert any("⣾" in w for w in writes)


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
        assert any("⣾" in w or "Test Loading" in w for w in writes)

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


def test_spinner_with_timer():
    """Test that Spinner displays elapsed time when show_timer is True."""
    with patch("sys.stdout") as mock_stdout, patch("time.time") as mock_time:
        mock_stdout.isatty.return_value = True

        # Simulate time progression
        start_time = 1000.0
        current_time = [start_time]

        def fake_time():
            return current_time[0]

        mock_time.side_effect = fake_time

        # Create spinner with timer enabled
        # Use short delay so thread updates quickly
        spinner = cli_ui.Spinner("Test Timer", delay=0.01, show_timer=True)

        with spinner:
            # Give time for thread to start and capture start_time
            time.sleep(0.2)
            # Advance time by 2.5 seconds (should trigger timer display which needs > 1.0s)
            current_time[0] += 2.5
            # Sleep enough to let the spinner thread iterate at least once
            # Since delay is 0.01, sleeping 0.05 is plenty
            time.sleep(0.05)

        # Verify writes
        assert mock_stdout.write.called
        writes = [args[0] for args, _ in mock_stdout.write.call_args_list]

        # Check for presence of formatted time
        # We expect " (2s)" to be part of the output string
        # Note: Depending on timing, it might print multiple frames.
        # We just need to find one occurrence.
        assert any("(2s)" in w for w in writes), f"Writes were: {writes}"


def test_spinner_custom_messages():
    """Test that Spinner uses custom success and error messages."""
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = True

        # Test success message
        with cli_ui.Spinner("Loading...", success_message="Done!", delay=0.001):
            pass

        writes = [args[0] for args, _ in mock_stdout.write.call_args_list]
        assert any("✅ Done!" in w for w in writes)

        # Reset mock
        mock_stdout.reset_mock()

        # Test error message
        try:
            with cli_ui.Spinner("Loading...", error_message="Failed!", delay=0.001):
                raise ValueError("Oops")
        except ValueError:
            pass

        writes = [args[0] for args, _ in mock_stdout.write.call_args_list]
        assert any("❌ Failed!" in w for w in writes)


@patch("sys.stdout")
@patch("time.time")
def test_spinner_final_message_with_timer(mock_time, mock_stdout):
    """Test that Spinner final message includes elapsed time when show_timer is True."""
    mock_stdout.isatty.return_value = True

    # Simulate time progression
    start_time = 1000.0
    current_time = [start_time]

    def fake_time():
        return current_time[0]

    mock_time.side_effect = fake_time

    # Create spinner with timer enabled
    spinner = cli_ui.Spinner("Test Timer", delay=0.01, show_timer=True)

    with spinner:
        # Give time for thread to start (though mock time doesn't advance automatically)
        time.sleep(0.05)

        # Advance time by 2.5 seconds
        current_time[0] += 2.5

        # We don't need to wait for the thread to spin, as we are testing __exit__ logic
        # which uses time.time() - self.start_time

    # Verify writes
    assert mock_stdout.write.called
    writes = [args[0] for args, _ in mock_stdout.write.call_args_list]

    # The final message should be something like "✅ Test Timer (2s)\n"
    # We check for the presence of the duration string in the final write

    # Filter for the final message (it ends with newline)
    final_messages = [w for w in writes if w.endswith("\n") and "Test Timer" in w]
    assert final_messages, "No final message found"

    final_msg = final_messages[-1]
    assert "(2s)" in final_msg, f"Final message did not include duration: {final_msg}"


@patch("sys.stdout")
def test_spinner_step(mock_stdout):
    """Test that Spinner step updates message and handles cleanup."""
    mock_stdout.isatty.return_value = True

    # Use a very small delay so the spinner thread loops frequently
    spinner = cli_ui.Spinner("Initial", delay=0.001)

    with spinner:
        time.sleep(0.01)  # Let it spin a bit with "Initial"
        spinner.step("Updated Long Message")
        time.sleep(0.01)  # Let it spin with longer message
        spinner.step("Short")
        time.sleep(0.01)  # Let it spin with shorter message

    writes = [args[0] for args, _ in mock_stdout.write.call_args_list]

    # Verify initial message was printed
    assert any("Initial" in w for w in writes)

    # Verify updated long message was printed
    assert any("Updated Long Message" in w for w in writes)

    # Verify short message was printed
    assert any("Short" in w for w in writes)


def test_create_terminal_link():
    """Test create_terminal_link formatting."""
    url = "https://example.com"
    text = "Link"
    expected = "\033]8;;https://example.com\033\\Link\033]8;;\033\\"

    # Test with TTY and no NO_COLOR
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = True
        with patch.dict("os.environ", {}, clear=True):
            assert cli_ui.create_terminal_link(text, url) == expected

    # Test with NO_COLOR
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = True
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            assert cli_ui.create_terminal_link(text, url) == text

    # Test with non-TTY
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        with patch.dict("os.environ", {}, clear=True):
            assert cli_ui.create_terminal_link(text, url) == text


def test_get_visual_length():
    """Test _get_visual_length helper."""
    # Plain text
    assert cli_ui._get_visual_length("Hello") == 5

    # ANSI Color
    import click

    colored = click.style("Hello", fg="red")
    assert cli_ui._get_visual_length(colored) == 5

    # OSC 8 Link
    url = "https://example.com"
    text = "Link"
    link = f"\033]8;;{url}\033\\{text}\033]8;;\033\\"
    assert cli_ui._get_visual_length(link) == 4

    # Mixed
    mixed = f"Click {link} now"
    assert cli_ui._get_visual_length(mixed) == 14  # "Click " (6) + "Link" (4) + " now" (4)
