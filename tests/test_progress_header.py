"""Tests for progress header display functionality."""

import io
import sys
from unittest.mock import patch

import pytest

from auto_coder.progress_header import (
    ProgressContext,
    ProgressHeader,
    ProgressStage,
    clear_progress,
    get_progress_header,
    newline_progress,
    update_progress,
)


def test_progress_header_format():
    """Test that progress header formats correctly."""
    header = ProgressHeader()
    
    # Test formatting
    formatted = header._format_header("PR", 123, "Running tests")
    
    # Should contain PR number and stage
    assert "PR" in formatted
    assert "123" in formatted
    assert "Running tests" in formatted


def test_progress_header_update_with_tty(monkeypatch):
    """Test progress header update when stream is a TTY."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Create a new header instance with mocked stream
    header = ProgressHeader(stream=mock_stream)
    header.update("PR", 123, "Testing")

    # Get the output
    output = mock_stream.getvalue()

    # Should contain the header
    assert "PR" in output
    assert "123" in output
    assert "Testing" in output


def test_progress_header_update_without_tty(monkeypatch):
    """Test progress header update when stream is not a TTY."""
    # Mock stream as not a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: False

    # Create a new header instance with mocked stream
    header = ProgressHeader(stream=mock_stream)
    header.update("Issue", 456, "Processing")

    # Get the output
    output = mock_stream.getvalue()

    # When not a TTY, header is not printed (only logged)
    # So output should be empty
    assert output == ""


def test_progress_header_clear():
    """Test clearing the progress header."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Create a new header instance with mocked stream
    header = ProgressHeader(stream=mock_stream)
    header.update("PR", 123, "Testing")
    header.clear()

    # Should have cleared the line
    output = mock_stream.getvalue()
    # Clear uses cursor save/restore sequences
    assert "\0337" in output or "\033[H" in output


def test_progress_header_newline():
    """Test adding a newline after the header."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Mock sys.stdout for newline() which writes to stdout
    mock_stdout = io.StringIO()

    with patch('sys.stdout', mock_stdout):
        # Create a new header instance with mocked stream
        header = ProgressHeader(stream=mock_stream)
        header.update("PR", 123, "Testing")
        header.newline()

        # Should have added a newline to stdout
        output = mock_stdout.getvalue()
        assert "\n" in output


def test_global_progress_header():
    """Test global progress header functions."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Reset global instance
    import auto_coder.progress_header as ph
    ph._global_header = None

    # Create global header with mocked stream
    ph._global_header = ProgressHeader(stream=mock_stream)

    # Test update
    update_progress("PR", 789, "Global test")

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
        import auto_coder.progress_header as ph
        ph._global_header = None

        # Create global header with mocked stream
        ph._global_header = ProgressHeader(stream=mock_stream)

        # Use context manager
        with ProgressContext("Issue", 999, "Context test") as ctx:
            # Should have displayed the header
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


def test_progress_header_thread_safety():
    """Test that progress header is thread-safe."""
    import threading
    
    header = ProgressHeader()
    errors = []
    
    def update_header(item_type, number, stage):
        try:
            for _ in range(10):
                header.update(item_type, number, stage)
        except Exception as e:
            errors.append(e)
    
    # Create multiple threads
    threads = []
    for i in range(5):
        t = threading.Thread(target=update_header, args=("PR", i, f"Stage {i}"))
        threads.append(t)
        t.start()
    
    # Wait for all threads
    for t in threads:
        t.join()
    
    # Should not have any errors
    assert len(errors) == 0


def test_progress_header_multiple_updates():
    """Test multiple updates to the progress header."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    header = ProgressHeader(stream=mock_stream)

    # Multiple updates
    header.update("PR", 100, "Stage 1")
    header.update("PR", 100, "Stage 2")
    header.update("PR", 100, "Stage 3")

    # Should have all stages in output
    output = mock_stream.getvalue()
    assert "Stage 1" in output or "Stage 2" in output or "Stage 3" in output


def test_progress_header_different_items():
    """Test progress header with different item types."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    header = ProgressHeader(stream=mock_stream)

    # Update with PR
    header.update("PR", 123, "PR stage")

    # Update with Issue
    header.update("Issue", 456, "Issue stage")

    # Should have both in output
    output = mock_stream.getvalue()
    assert ("PR" in output or "Issue" in output)
    assert ("123" in output or "456" in output)


def test_progress_header_special_characters():
    """Test progress header with special characters in stage."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    header = ProgressHeader(stream=mock_stream)

    # Update with special characters
    header.update("PR", 123, "Running tests: 50% complete")

    # Should handle special characters
    output = mock_stream.getvalue()
    assert "Running tests" in output
    assert "50%" in output or "50" in output


def test_progress_header_nested_stages():
    """Test progress header with nested stages."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    header = ProgressHeader(stream=mock_stream)

    # Push first stage
    header.push_stage("First pass")
    header.update("PR", 123, "Checking status")

    output = mock_stream.getvalue()
    assert "First pass" in output
    assert "Checking status" in output
    assert "/" in output  # Should have separator

    # Push second stage
    header.push_stage("Running LLM")
    header.update("PR", 123, "Staging changes")

    output = mock_stream.getvalue()
    assert "First pass" in output
    assert "Running LLM" in output
    assert "Staging changes" in output

    # Pop stage
    header.pop_stage()
    header.update("PR", 123, "Committing")

    output = mock_stream.getvalue()
    assert "First pass" in output
    assert "Committing" in output
    # "Running LLM" should not be in the latest output

    # Clear should reset stack
    header.clear()

    # Clear the mock stream to start fresh
    mock_stream.truncate(0)
    mock_stream.seek(0)

    header.update("PR", 456, "New task")

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

    # Replace global header with our mock
    import auto_coder.progress_header as ph
    original_header = ph._global_header
    ph._global_header = ProgressHeader(stream=mock_stream)

    try:
        # Use ProgressStage context manager with global header
        with ProgressStage("First pass"):
            update_progress("PR", 123, "Checking status")

            output = mock_stream.getvalue()
            assert "First pass" in output
            assert "Checking status" in output
            assert "/" in output

            # Nested stage
            with ProgressStage("Running LLM"):
                update_progress("PR", 123, "Staging changes")

                output = mock_stream.getvalue()
                assert "First pass" in output
                assert "Running LLM" in output
                assert "Staging changes" in output

            # After exiting nested context, should be back to "First pass"
            update_progress("PR", 123, "Committing")

            output = mock_stream.getvalue()
            assert "First pass" in output
            assert "Committing" in output

        # After exiting context, stack should be empty
        mock_stream.truncate(0)
        mock_stream.seek(0)

        update_progress("PR", 456, "New task")

        output = mock_stream.getvalue()
        assert "456" in output
        assert "New task" in output
        assert "First pass" not in output

    finally:
        # Restore original header
        ph._global_header = original_header


def test_progress_stage_with_update_progress():
    """Test ProgressStage with global update_progress function."""
    # Mock stream as a TTY
    mock_stream = io.StringIO()
    mock_stream.isatty = lambda: True

    # Replace global header with our mock
    import auto_coder.progress_header as ph
    original_header = ph._global_header
    ph._global_header = ProgressHeader(stream=mock_stream)

    try:
        # Use ProgressStage with global update_progress
        with ProgressStage("First pass"):
            update_progress("PR", 123, "Checking status")

            output = mock_stream.getvalue()
            assert "First pass" in output
            assert "Checking status" in output

            with ProgressStage("Running LLM"):
                update_progress("PR", 123, "Staging changes")

                output = mock_stream.getvalue()
                assert "First pass" in output
                assert "Running LLM" in output
                assert "Staging changes" in output

            # After exiting nested context
            update_progress("PR", 123, "Committing")

            output = mock_stream.getvalue()
            assert "First pass" in output
            assert "Committing" in output

        # After exiting all contexts
        mock_stream.truncate(0)
        mock_stream.seek(0)

        update_progress("PR", 456, "New task")

        output = mock_stream.getvalue()
        assert "456" in output
        assert "New task" in output
        assert "First pass" not in output

    finally:
        # Restore original header
        ph._global_header = original_header


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

