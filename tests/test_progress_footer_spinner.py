"""Tests for progress footer spinner functionality."""

import io

from auto_coder.progress_footer import ProgressFooter


def test_progress_footer_spinner_tick():
    """Test that tick updates the spinner index."""
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True
    footer = ProgressFooter(stream=mock_stream)

    # Activate footer (needed for tick to work)
    footer.set_item("PR", 123)

    initial_idx = footer._spinner_idx
    assert initial_idx == 0

    footer.tick()
    assert footer._spinner_idx == 1

    footer.tick()
    assert footer._spinner_idx == 2


def test_progress_footer_spinner_render():
    """Test that spinner is included in formatted output."""
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True
    footer = ProgressFooter(stream=mock_stream)

    # Activate footer (needed for tick to work)
    footer.set_item("PR", 123)

    # Force color mode (default usually, but explicit check)
    footer._no_color = False
    footer._spinner_frames = ["A", "B", "C"]
    footer._spinner_idx = 0

    formatted = footer._format_footer("PR", 123)
    assert "A" in formatted

    footer.tick()  # idx becomes 1 -> B
    formatted = footer._format_footer("PR", 123)
    assert "B" in formatted


def test_progress_footer_spinner_no_color():
    """Test spinner in no-color mode."""
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Create footer and force no_color
    footer = ProgressFooter(stream=mock_stream)
    footer._no_color = True
    # Re-init spinner frames for no-color (normally done in __init__)
    footer._spinner_frames = ["|", "/", "-", "\\"]

    formatted = footer._format_footer("PR", 123)
    assert "|" in formatted

    footer.set_item("PR", 123)  # Activate
    footer.tick()

    formatted = footer._format_footer("PR", 123)
    assert "/" in formatted
