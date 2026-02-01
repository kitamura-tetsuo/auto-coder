import os
from unittest.mock import MagicMock, patch

from src.auto_coder import cli_ui


def test_colorize_value_extensions():
    """Test the extended colorization logic for Completed, Pending, Warning."""
    with patch("click.style") as mock_style:
        mock_style.side_effect = lambda x, **kwargs: x

        # Helper to get the color used
        def get_color(value):
            cli_ui._colorize_value(value)
            # Find the call for this value
            for call in mock_style.call_args_list:
                if call.args[0] == value:
                    return call.kwargs.get("fg")
            return None

        with patch.dict(os.environ, {}, clear=True):
            # Verify Green
            mock_style.reset_mock()
            cli_ui._colorize_value("Completed")
            assert mock_style.call_args.kwargs["fg"] == "green"

            mock_style.reset_mock()
            cli_ui._colorize_value("Success")
            assert mock_style.call_args.kwargs["fg"] == "green"

            # Verify Yellow
            mock_style.reset_mock()
            cli_ui._colorize_value("Pending")
            assert mock_style.call_args.kwargs["fg"] == "yellow"

            mock_style.reset_mock()
            cli_ui._colorize_value("Warning")
            assert mock_style.call_args.kwargs["fg"] == "yellow"

            mock_style.reset_mock()
            cli_ui._colorize_value("Skipped")
            assert mock_style.call_args.kwargs["fg"] == "yellow"

            # Verify Red
            mock_style.reset_mock()
            cli_ui._colorize_value("Error")
            assert mock_style.call_args.kwargs["fg"] == "red"
