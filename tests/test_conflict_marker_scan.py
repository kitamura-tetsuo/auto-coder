import pytest

from src.auto_coder.automation_engine import AutomationEngine


def test_detects_real_conflict_block():
    text = (
        "line a\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> feature-branch\n"
        "line z\n"
    )
    assert AutomationEngine._has_conflict_block_in_text(text) is True


def test_ignores_markers_not_at_line_start():
    text = (
        "some text <<<<<<< not a marker ======= and >>>>>>> not either\n"
        "another line\n"
    )
    assert AutomationEngine._has_conflict_block_in_text(text) is False


def test_ignores_isolated_markers_without_triad():
    # Has a start but no mid/end
    text1 = "<<<<<<< HEAD\njust text\nno end here\n"
    # Has mid and end but no start
    text2 = "some\n=======\n>>>>>> something\n"
    assert AutomationEngine._has_conflict_block_in_text(text1) is False
    assert AutomationEngine._has_conflict_block_in_text(text2) is False


def test_ignores_coverage_like_html():
    # Simulate coverage HTML where angle brackets are usually escaped or not at start
    text = (
        "<pre><code>&lt;&lt;&lt;&lt;&lt;&lt;&lt; HEAD\n"
        "code sample\n"
        "=======\n"
        "&gt;&gt;&gt;&gt;&gt;&gt;&gt; feature-branch</code></pre>\n"
    )
    assert AutomationEngine._has_conflict_block_in_text(text) is False

