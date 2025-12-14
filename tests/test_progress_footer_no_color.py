"""Tests for progress footer NO_COLOR support."""

import os
from unittest.mock import patch
import pytest
from auto_coder.progress_footer import ProgressFooter

def test_progress_footer_no_color():
    """Test that NO_COLOR environment variable disables colors."""
    with patch.dict(os.environ, {"NO_COLOR": "1"}):
        footer = ProgressFooter()
        assert footer._no_color is True

        # Test PR formatting (cyan usually)
        formatted = footer._format_footer("PR", 123)
        assert "\033[96m" not in formatted
        assert "[PR #123]" in formatted

        # Test Issue formatting (magenta usually)
        formatted = footer._format_footer("Issue", 456)
        assert "\033[95m" not in formatted
        assert "[Issue #456]" in formatted

        # Test branch name (red usually)
        footer._branch_name = "feature-x"
        formatted = footer._format_footer("PR", 123)
        assert "\033[91m" not in formatted
        assert "/feature-x" in formatted

        # Test stages (yellow usually)
        footer._stage_stack = ["Stage 1"]
        formatted = footer._format_footer("PR", 123)
        assert "\033[93m" not in formatted
        assert "Stage 1" in formatted

def test_progress_footer_no_color_empty_string():
    """Test that NO_COLOR with empty string does NOT disable colors."""
    # NO_COLOR spec says "when set and not empty" (actually usually just presence,
    # but some implementations check for value).
    # The code implementation: bool(os.environ.get("NO_COLOR"))
    # get return None if missing -> bool(None) is False.
    # if set to "", get returns "" -> bool("") is False.
    # if set to "0", get returns "0" -> bool("0") is True in Python!

    # Wait, strict NO_COLOR standard says "when present and not empty".
    # https://no-color.org/ : "User-level configuration files and per-instance command-line arguments should override NO_COLOR. A blank string should be considered true?"
    # No: "NO_COLOR... to any string (not empty)..."
    # "All command-line software which outputs text with ANSI color should check for the presence of a NO_COLOR environment variable that, when present (and not an empty string), prevents the addition of ANSI color."

    # My implementation: bool(os.environ.get("NO_COLOR"))
    # os.environ.get("NO_COLOR") returns None if not present.
    # If present as "1", returns "1".
    # If present as "", returns "".
    # bool("") is False. So empty string = Colors Enabled. This matches the spec ("not an empty string").

    with patch.dict(os.environ, {"NO_COLOR": ""}):
        footer = ProgressFooter()
        assert footer._no_color is False

        formatted = footer._format_footer("PR", 123)
        assert "\033[96m" in formatted

def test_progress_footer_color_enabled_by_default():
    """Test that colors are enabled when NO_COLOR is missing."""
    # Ensure NO_COLOR is not in env
    with patch.dict(os.environ):
        if "NO_COLOR" in os.environ:
            del os.environ["NO_COLOR"]

        footer = ProgressFooter()
        assert footer._no_color is False

        formatted = footer._format_footer("PR", 123)
        assert "\033[96m" in formatted
