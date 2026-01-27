import os
from unittest.mock import MagicMock, patch

from src.auto_coder import cli_ui


def test_print_completion_message_basic():
    """Test print_completion_message formatting with colors (default)."""
    summary = {"Repository": "test/repo", "Status": "Success"}

    with patch("click.secho") as mock_secho, patch("click.echo") as mock_echo, patch("click.style") as mock_style:
        # Ensure NO_COLOR is not set
        with patch.dict(os.environ, {}, clear=True):
            cli_ui.print_completion_message("Test Title", summary)

            # Check title printed with secho (for bold/color)
            assert mock_secho.call_count > 0
            title_call = mock_secho.call_args_list[0]
            # Verify title content and color
            assert "✨ Test Title ✨" in title_call.args[0]
            assert title_call.kwargs.get("fg") == "green"

            # Check keys styled
            assert mock_style.call_count > 0
            # Extract first argument from all calls to style
            style_args = [call.args[0] for call in mock_style.call_args_list]

            # Use 'any' to check if "Repository" appears in any of the styled strings
            assert any("Repository" in s for s in style_args), f"Repository not found in styled calls: {style_args}"
            assert any("Status" in s for s in style_args)

            # Check values printed via echo
            # echo is called for title (if no color), spacer, and key:value pairs
            echo_args = [call.args[0] if call.args else "" for call in mock_echo.call_args_list]
            assert any("test/repo" in c for c in echo_args)
            assert any("Success" in c for c in echo_args)


def test_print_completion_message_no_color():
    """Test print_completion_message formatting with NO_COLOR."""
    summary = {"Repository": "test/repo", "Status": "Success"}

    with patch("click.secho") as mock_secho, patch("click.echo") as mock_echo, patch("click.style") as mock_style:
        # Set NO_COLOR
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            cli_ui.print_completion_message("Test Title", summary)

            # No secho calls
            mock_secho.assert_not_called()

            # No style calls
            mock_style.assert_not_called()

            # Check output via echo
            echo_args = [call.args[0] if call.args else "" for call in mock_echo.call_args_list]
            assert any("Test Title" in c for c in echo_args)
            assert any("Repository" in c for c in echo_args)
            assert any("test/repo" in c for c in echo_args)


def test_print_completion_message_list_values():
    """Test print_completion_message with list values."""
    summary = {"Actions": ["Action 1", "Action 2"], "Details": "Some details"}

    with patch("click.echo") as mock_echo, patch("click.style") as mock_style:
        with patch.dict(os.environ, {}, clear=True):
            cli_ui.print_completion_message("Test Title", summary)

            echo_args = [call.args[0] if call.args else "" for call in mock_echo.call_args_list]
            combined_output = "\n".join(echo_args)

            assert "Action 1" in combined_output
            assert "Action 2" in combined_output
            # Check for indentation/formatting
            assert "    - Action 1" in combined_output


def test_print_completion_message_empty():
    """Test print_completion_message with empty summary."""
    with patch("click.echo") as mock_echo, patch("click.secho") as mock_secho:
        cli_ui.print_completion_message("Test Title", {})

        mock_echo.assert_not_called()
        mock_secho.assert_not_called()


def test_print_completion_message_dict_values():
    """Test print_completion_message with dictionary values."""
    summary = {
        "Simple": "Value",
        "Dict": {"Key A": "Value A", "Key B": "Value B"},
        "Nested": {
            "SubList": ["A", "B"],
            "SubDict": {"X": 1}
        }
    }

    with patch("click.echo") as mock_echo:
        with patch.dict(os.environ, {}, clear=True):
            cli_ui.print_completion_message("Test Title", summary)

            echo_args = [call.args[0] if call.args else "" for call in mock_echo.call_args_list]
            combined_output = "\n".join(echo_args)

            # Check for Dict formatting
            assert "- Key A: Value A" in combined_output
            assert "- Key B: Value B" in combined_output

            # Check for Nested formatting
            assert "- SubList:" in combined_output
            assert "- A" in combined_output
            assert "- B" in combined_output
            assert "- SubDict:" in combined_output
            assert "- X: 1" in combined_output

            # Check indentation (approximate)
            # The exact spacing depends on my implementation, but it should be indented
            assert "    - Key A" in combined_output
