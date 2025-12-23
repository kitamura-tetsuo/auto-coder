import sys
import unittest
from unittest.mock import MagicMock, call, patch

from src.auto_coder.cli_ui import sleep_with_countdown


class TestSleepWithCountdown(unittest.TestCase):
    @patch("time.sleep")
    @patch("sys.stdout")
    def test_sleep_with_countdown_tty_color(self, mock_stdout, mock_sleep):
        """Test countdown with TTY and color enabled."""
        mock_stdout.isatty.return_value = True

        # Call function
        sleep_with_countdown(2, "Test Sleep")

        # Verify output calls
        # We expect calls to write for cursor save, countdown updates, and clear
        self.assertTrue(mock_stdout.write.called)

        # Verify sleep called
        self.assertEqual(mock_sleep.call_count, 2) # One for each second

    @patch("time.sleep")
    @patch("sys.stdout")
    @patch.dict("os.environ", {"NO_COLOR": "1"})
    def test_sleep_with_countdown_tty_no_color(self, mock_stdout, mock_sleep):
        """Test countdown with TTY and NO_COLOR."""
        mock_stdout.isatty.return_value = True

        sleep_with_countdown(1, "Test Sleep")

        # Verify output contains no ANSI codes for color (except cursor control)
        # We can check args of write calls
        calls = mock_stdout.write.call_args_list
        # Filter for the countdown string
        countdown_calls = [c for c in calls if "Test Sleep" in str(c)]
        for c in countdown_calls:
            arg = c[0][0]
            # Should not contain color codes like \x1b[36m (cyan)
            self.assertNotIn("\x1b[36m", arg)

    @patch("time.sleep")
    @patch("sys.stdout")
    def test_sleep_with_countdown_no_tty(self, mock_stdout, mock_sleep):
        """Test fallback when not TTY."""
        mock_stdout.isatty.return_value = False

        sleep_with_countdown(5, "Test Sleep")

        # Should call time.sleep(5) once
        mock_sleep.assert_called_with(5)
        # Should not write to stdout
        mock_stdout.write.assert_not_called()

    @patch("time.sleep")
    @patch("sys.stdout")
    def test_sleep_zero_seconds(self, mock_stdout, mock_sleep):
        """Test immediate return for <= 0 seconds."""
        sleep_with_countdown(0, "Test")
        mock_sleep.assert_not_called()
        mock_stdout.write.assert_not_called()
