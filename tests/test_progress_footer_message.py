"""Tests for progress footer custom message functionality."""

import io
from unittest.mock import patch

import pytest

from auto_coder.progress_footer import ProgressFooter, get_progress_footer, set_progress_message


def test_progress_footer_custom_message(monkeypatch):
    """Test setting a custom message in the progress footer."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)
    footer.set_message("Next check in 10s")

    output = mock_stream.getvalue()

    # Should contain the message
    assert "Next check in 10s" in output
    # Should contain the sleep emoji (if color enabled)
    assert "💤" in output
    # Should contain cyan color
    assert "\033[96m" in output


def test_progress_footer_custom_message_no_color(monkeypatch):
    """Test custom message with NO_COLOR."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Mock NO_COLOR
    monkeypatch.setenv("NO_COLOR", "1")

    footer = ProgressFooter(stream=mock_stream)
    footer.set_message("Waiting")

    output = mock_stream.getvalue()

    # Should contain the message
    assert "Waiting" in output
    # Should NOT contain color codes
    assert "\033[96m" not in output
    # Should NOT contain emoji
    assert "💤" not in output
    # Should be bracketed
    assert "[Waiting]" in output


def test_progress_footer_message_cleared_by_item():
    """Test that setting an item clears the custom message."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)

    # Set message
    footer.set_message("Waiting")
    assert footer._custom_message == "Waiting"

    # Set item
    footer.set_item("PR", 123)

    # Message should be cleared
    assert footer._custom_message is None

    output = mock_stream.getvalue()
    assert "PR" in output
    assert "123" in output


def test_progress_footer_clear_clears_message():
    """Test that clear() clears the custom message."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)

    # Set message
    footer.set_message("Waiting")
    assert footer._custom_message == "Waiting"

    # Clear
    footer.clear()

    # Message should be cleared
    assert footer._custom_message is None


def test_global_set_progress_message():
    """Test global set_progress_message helper."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Reset global instance
    import auto_coder.progress_footer as ph
    ph._global_footer = None
    ph._global_footer = ProgressFooter(stream=mock_stream)

    set_progress_message("Global waiting")

    output = mock_stream.getvalue()
    assert "Global waiting" in output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
