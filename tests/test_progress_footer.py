"""Tests for progress footer display functionality."""

import io
import sys
from unittest.mock import patch

import pytest

from auto_coder.progress_footer import (
    ProgressContext,
    ProgressFooter,
    ProgressStage,
    clear_progress,
    get_progress_footer,
    newline_progress,
    set_progress_item,
    push_progress_stage,
    pop_progress_stage,
)


def test_progress_footer_format():
    """Test that progress footer formats correctly."""
    footer = ProgressFooter()

    # Test formatting with stages
    footer._stage_stack = ["Running tests"]
    formatted = footer._format_footer("PR", 123)

    # Should contain PR number and stage
    assert "PR" in formatted
    assert "123" in formatted
    assert "Running tests" in formatted


def test_progress_footer_update_with_tty(monkeypatch):
    """Test progress footer set_item and push_stage when stream is a TTY."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Create a new footer instance with mocked stream
    footer = ProgressFooter(stream=mock_stream)
    footer.set_item("PR", 123)
    footer.push_stage("Testing")

    # Get the output
    output = mock_stream.getvalue()

    # Should contain the footer
    assert "PR" in output
    assert "123" in output
    assert "Testing" in output


def test_progress_footer_update_without_tty(monkeypatch):
    """Test progress footer set_item and push_stage when stream is not a TTY."""
    # Mock stream as not a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: False

    # Create a new footer instance with mocked stream
    footer = ProgressFooter(stream=mock_stream)
    footer.set_item("Issue", 456)
    footer.push_stage("Processing")

    # Get the output
    output = mock_stream.getvalue()

    # When not a TTY, footer is not printed (only logged)
    # So output should be empty
    assert output == ""


def test_progress_footer_clear():
    """Test clearing the progress footer."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Create a new footer instance with mocked stream
    footer = ProgressFooter(stream=mock_stream)
    footer.set_item("PR", 123)
    footer.push_stage("Testing")
    footer.clear()

    # Should have cleared the line
    output = mock_stream.getvalue()
    # Clear uses cursor save/restore sequences
    assert "\0337" in output or "\033[H" in output


def test_progress_footer_newline():
    """Test adding a newline after the footer."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Mock sys.stdout for newline() which writes to stdout
    mock_stdout = io.StringIO()

    with patch('sys.stdout', mock_stdout):
        # Create a new footer instance with mocked stream
        footer = ProgressFooter(stream=mock_stream)
        footer.set_item("PR", 123)
        footer.push_stage("Testing")
        footer.newline()

        # Should have added a newline to stdout
        output = mock_stdout.getvalue()
        assert "\n" in output


def test_global_progress_footer():
    """Test global progress footer functions."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Reset global instance
    import auto_coder.progress_footer as ph
    ph._global_footer = None

    # Create global footer with mocked stream
    ph._global_footer = ProgressFooter(stream=mock_stream)

    # Test set_item and push_stage
    set_progress_item("PR", 789)
    push_progress_stage("Global test")

    # Get the output
    output = mock_stream.getvalue()
    assert "PR" in output
    assert "789" in output
    assert "Global test" in output


def test_progress_context():
    """Test progress context manager."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Mock stdout for newline
    mock_stdout = io.StringIO()

    with patch('sys.stdout', mock_stdout):
        # Reset global instance
        import auto_coder.progress_footer as ph
        ph._global_footer = None

        # Create global footer with mocked stream
        ph._global_footer = ProgressFooter(stream=mock_stream)

        # Use context manager
        with ProgressContext("Issue", 999, "Context test") as ctx:
            # Should have displayed the footer
            output = mock_stream.getvalue()
            assert "Issue" in output
            assert "999" in output
            assert "Context test" in output

            # Update stage
            ctx.update_stage("New stage")
            output = mock_stream.getvalue()
            assert "New stage" in output

        # Should have cleared after exit (newline to stdout)
        output = mock_stdout.getvalue()
        assert "\n" in output


def test_progress_footer_thread_safety():
    """Test that progress footer is thread-safe."""
    import threading

    footer = ProgressFooter()
    errors = []

    def update_footer(item_type, number, stage):
        try:
            for _ in range(10):
                footer.set_item(item_type, number)
                footer.push_stage(stage)
                footer.pop_stage()
        except Exception as e:
            errors.append(e)

    # Create multiple threads
    threads = []
    for i in range(5):
        t = threading.Thread(target=update_footer, args=("PR", i, f"Stage {i}"))
        threads.append(t)
        t.start()

    # Wait for all threads
    for t in threads:
        t.join()

    # Should not have any errors
    assert len(errors) == 0


def test_progress_footer_race_condition_print_footer():
    """Test that print_footer handles race conditions when _current_footer becomes None."""
    import threading
    import time

    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)
    errors = []

    def set_and_clear():
        try:
            for _ in range(20):
                footer.set_item("PR", 123)
                footer.push_stage("Testing")
                time.sleep(0.001)  # Small delay to increase chance of race condition
                footer.clear()
        except Exception as e:
            errors.append(e)

    def print_repeatedly():
        try:
            for _ in range(20):
                footer.print_footer()
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)

    # Create threads that set/clear and print concurrently
    threads = []
    for _ in range(3):
        t1 = threading.Thread(target=set_and_clear)
        t2 = threading.Thread(target=print_repeatedly)
        threads.extend([t1, t2])
        t1.start()
        t2.start()

    # Wait for all threads
    for t in threads:
        t.join()

    # Should not have any errors (especially TypeError from None subscript)
    assert len(errors) == 0


def test_progress_footer_race_condition_sink_wrapper():
    """Test that sink_wrapper handles race conditions when _current_footer becomes None."""
    import threading
    import time

    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)
    errors = []

    def set_and_clear():
        try:
            for _ in range(20):
                footer.set_item("PR", 456)
                footer.push_stage("Processing")
                time.sleep(0.001)
                footer.clear()
        except Exception as e:
            errors.append(e)

    def log_repeatedly():
        try:
            for i in range(20):
                footer.sink_wrapper(f"Log message {i}\n")
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)

    # Create threads that set/clear and log concurrently
    threads = []
    for _ in range(3):
        t1 = threading.Thread(target=set_and_clear)
        t2 = threading.Thread(target=log_repeatedly)
        threads.extend([t1, t2])
        t1.start()
        t2.start()

    # Wait for all threads
    for t in threads:
        t.join()

    # Should not have any errors (especially TypeError from None subscript)
    assert len(errors) == 0


def test_progress_footer_multiple_updates():
    """Test multiple push/pop stages to the progress footer."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)
    footer.set_item("PR", 100)

    # Multiple stage updates
    footer.push_stage("Stage 1")
    footer.pop_stage()
    footer.push_stage("Stage 2")
    footer.pop_stage()
    footer.push_stage("Stage 3")

    # Should have all stages in output
    output = mock_stream.getvalue()
    assert "Stage 1" in output or "Stage 2" in output or "Stage 3" in output


def test_progress_footer_different_items():
    """Test progress footer with different item types."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)

    # Set PR
    footer.set_item("PR", 123)
    footer.push_stage("PR stage")

    # Set Issue
    footer.set_item("Issue", 456)
    footer.push_stage("Issue stage")

    # Should have both in output
    output = mock_stream.getvalue()
    assert ("PR" in output or "Issue" in output)
    assert ("123" in output or "456" in output)


def test_progress_footer_special_characters():
    """Test progress footer with special characters in stage."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)

    # Set item and push stage with special characters
    footer.set_item("PR", 123)
    footer.push_stage("Running tests: 50% complete")

    # Should handle special characters
    output = mock_stream.getvalue()
    assert "Running tests" in output
    assert "50%" in output or "50" in output


def test_progress_footer_nested_stages():
    """Test progress footer with nested stages."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    footer = ProgressFooter(stream=mock_stream)
    footer.set_item("PR", 123)

    # Push first stage
    footer.push_stage("First pass")

    output = mock_stream.getvalue()
    assert "First pass" in output

    # Push second stage
    footer.push_stage("Checking status")

    output = mock_stream.getvalue()
    assert "First pass" in output
    assert "Checking status" in output
    assert "/" in output  # Should have separator

    # Push third stage
    footer.push_stage("Running LLM")

    output = mock_stream.getvalue()
    assert "First pass" in output
    assert "Running LLM" in output

    # Pop stage
    footer.pop_stage()

    output = mock_stream.getvalue()
    assert "First pass" in output
    assert "Checking status" in output
    # "Running LLM" should not be in the latest output

    # Clear should reset stack
    footer.clear()

    # Clear the mock stream to start fresh
    mock_stream.truncate(0)
    mock_stream.seek(0)

    footer.set_item("PR", 456)
    footer.push_stage("New task")

    output = mock_stream.getvalue()
    assert "456" in output
    assert "New task" in output
    # Should not have old stages after clear
    assert "First pass" not in output


def test_progress_stage_context_manager():
    """Test ProgressStage context manager for automatic push/pop."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Replace global footer with our mock
    import auto_coder.progress_footer as ph
    original_footer = ph._global_footer
    ph._global_footer = ProgressFooter(stream=mock_stream)

    try:
        # Use ProgressStage context manager with global footer
        with ProgressStage("First pass"):
            set_progress_item("PR", 123)
            push_progress_stage("Checking status")

            output = mock_stream.getvalue()
            assert "First pass" in output
            assert "Checking status" in output
            assert "/" in output

            # Nested stage
            with ProgressStage("Running LLM"):
                output = mock_stream.getvalue()
                assert "First pass" in output
                assert "Running LLM" in output

            # After exiting nested context, should be back to "First pass"
            pop_progress_stage()  # Pop "Checking status"
            push_progress_stage("Committing")

            output = mock_stream.getvalue()
            assert "First pass" in output
            assert "Committing" in output

        # After exiting context, clear and start fresh
        clear_progress()
        mock_stream.truncate(0)
        mock_stream.seek(0)

        set_progress_item("PR", 456)
        push_progress_stage("New task")

        output = mock_stream.getvalue()
        assert "456" in output
        assert "New task" in output
        assert "First pass" not in output

    finally:
        # Restore original footer
        ph._global_footer = original_footer


def test_progress_stage_with_set_and_push():
    """Test ProgressStage with global set_progress_item and push_progress_stage."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Replace global footer with our mock
    import auto_coder.progress_footer as ph
    original_footer = ph._global_footer
    ph._global_footer = ProgressFooter(stream=mock_stream)

    try:
        # Use ProgressStage with global functions
        with ProgressStage("First pass"):
            set_progress_item("PR", 123)
            push_progress_stage("Checking status")

            output = mock_stream.getvalue()
            assert "First pass" in output
            assert "Checking status" in output

            with ProgressStage("Running LLM"):
                output = mock_stream.getvalue()
                assert "First pass" in output
                assert "Running LLM" in output

            # After exiting nested context
            pop_progress_stage()  # Pop "Checking status"
            push_progress_stage("Committing")

            output = mock_stream.getvalue()
            assert "First pass" in output
            assert "Committing" in output

        # After exiting all contexts, clear and start fresh
        clear_progress()
        mock_stream.truncate(0)
        mock_stream.seek(0)

        set_progress_item("PR", 456)
        push_progress_stage("New task")

        output = mock_stream.getvalue()
        assert "456" in output
        assert "New task" in output
        assert "First pass" not in output

    finally:
        # Restore original footer
        ph._global_footer = original_footer


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

