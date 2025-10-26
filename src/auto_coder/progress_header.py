"""
Progress header display module for Auto-Coder.

This module provides overprint functionality to display processing status
in the terminal with PR/Issue number and processing stage.
"""

import sys
import threading
import shutil
from typing import Optional

from .logger_config import get_logger

logger = get_logger(__name__)


class ProgressHeader:
    """
    Manages progress header display with overprint functionality.

    This class provides a way to display processing status in the terminal
    that updates in place (overprint) rather than creating new lines.
    """

    def __init__(self, stream=sys.stderr):
        """Initialize the progress header."""
        self._lock = threading.Lock()
        self._current_header: Optional[str] = None
        self._is_active = False
        self._stream = stream
        # Check if stream is a TTY (supports overprint)
        self._supports_overprint = stream.isatty()
        # Stack for nested stages
        self._stage_stack: list[str] = []

    def _format_header(
        self,
        item_type: str,
        item_number: int,
        stage: str,
    ) -> str:
        """
        Format the header string with nested stages.

        Args:
            item_type: Type of item being processed ("PR" or "Issue")
            item_number: Number of the PR or Issue
            stage: Current processing stage

        Returns:
            Formatted header string
        """
        # Use ANSI color codes for better visibility
        # Cyan for item info, Yellow for stages
        # Build nested display: [PR #123] Stage1 / Stage2 / Stage3
        if self._stage_stack:
            all_stages = " / ".join(self._stage_stack + [stage])
        else:
            all_stages = stage
        return f"\033[36m[{item_type} #{item_number}]\033[0m \033[33m{all_stages}\033[0m"

    def print_header(self) -> None:
        """Print the current header at the top of the terminal."""
        if not self._current_header or not self._supports_overprint:
            return

        with self._lock:
            cols = shutil.get_terminal_size((80, 20)).columns
            padded = self._current_header[:cols].ljust(cols)
            # Save cursor position, move to top-left, clear line, print header, restore cursor
            self._stream.write("\0337")        # save cursor (alt: \033[s)
            self._stream.write("\033[H")       # move to top-left
            self._stream.write("\033[K")       # clear line
            self._stream.write(padded)
            self._stream.write("\0338")        # restore cursor (alt: \033[u)
            self._stream.flush()

    def update(
        self,
        item_type: str,
        item_number: int,
        stage: str,
    ) -> None:
        """
        Update the progress header.

        Args:
            item_type: Type of item being processed ("PR" or "Issue")
            item_number: Number of the PR or Issue
            stage: Current processing stage
        """
        with self._lock:
            header = self._format_header(item_type, item_number, stage)
            self._current_header = header
            self._is_active = True

            # Also log to logger for file logging
            logger.debug(f"Progress: {item_type} #{item_number} - {stage}")

        # Print header at the top
        self.print_header()

    def sink_wrapper(self, message):
        """
        Loguru sink wrapper that clears header before log, then re-prints after.

        Args:
            message: The log message from loguru
        """
        text = str(message)

        # Clear header before writing log (to avoid old headers in scrollback)
        if self._current_header and self._supports_overprint:
            with self._lock:
                self._stream.write("\0337")        # save cursor
                self._stream.write("\033[H")       # move to top-left
                self._stream.write("\033[K")       # clear line
                self._stream.write("\0338")        # restore cursor
                self._stream.flush()

        # Write log message
        with self._lock:
            self._stream.write(text)
            self._stream.flush()

        # Re-print header after log output
        self.print_header()

    def push_stage(self, stage: str) -> None:
        """
        Push a stage onto the stack for nested display.

        Args:
            stage: Stage to push onto the stack
        """
        with self._lock:
            self._stage_stack.append(stage)

    def pop_stage(self) -> None:
        """Pop the last stage from the stack."""
        with self._lock:
            if self._stage_stack:
                self._stage_stack.pop()

    def clear(self) -> None:
        """Clear the current progress header."""
        with self._lock:
            if self._is_active and self._supports_overprint:
                # Clear the top line
                self._stream.write("\0337")        # save cursor
                self._stream.write("\033[H")       # move to top-left
                self._stream.write("\033[K")       # clear line
                self._stream.write("\0338")        # restore cursor
                self._stream.flush()

            self._current_header = None
            self._is_active = False
            self._stage_stack.clear()

    def newline(self) -> None:
        """
        Print a newline to move past the current header.
        
        This should be called before printing regular output to ensure
        the header doesn't get overwritten.
        """
        with self._lock:
            if self._is_active and self._supports_overprint:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._is_active = False


# Global instance for use across the application
_global_header: Optional[ProgressHeader] = None
_global_header_lock = threading.Lock()


def get_progress_header() -> ProgressHeader:
    """
    Get the global progress header instance.

    Returns:
        The global ProgressHeader instance
    """
    global _global_header

    with _global_header_lock:
        if _global_header is None:
            _global_header = ProgressHeader()
        return _global_header


def setup_progress_header_logging() -> None:
    """
    Setup loguru to use progress header sink wrapper.

    This should be called early in the application startup to ensure
    all log messages are properly wrapped with header re-printing.
    """
    from .logger_config import setup_logger

    header = get_progress_header()
    # Re-setup logger with progress header sink
    setup_logger(progress_header=header)


def update_progress(item_type: str, item_number: int, stage: str) -> None:
    """
    Update the global progress header.

    Args:
        item_type: Type of item being processed ("PR" or "Issue")
        item_number: Number of the PR or Issue
        stage: Current processing stage
    """
    header = get_progress_header()
    header.update(item_type, item_number, stage)


def push_progress_stage(stage: str) -> None:
    """
    Push a stage onto the global progress header stack.

    Args:
        stage: Stage to push onto the stack
    """
    header = get_progress_header()
    header.push_stage(stage)


def pop_progress_stage() -> None:
    """Pop the last stage from the global progress header stack."""
    header = get_progress_header()
    header.pop_stage()


def clear_progress() -> None:
    """Clear the global progress header."""
    header = get_progress_header()
    header.clear()


def newline_progress() -> None:
    """Print a newline to move past the current header."""
    header = get_progress_header()
    header.newline()


# Context manager for automatic header management
class ProgressContext:
    """
    Context manager for automatic progress header management.
    
    Usage:
        with ProgressContext("PR", 123, "Analyzing"):
            # Do work
            pass
        # Header is automatically cleared on exit
    """

    def __init__(self, item_type: str, item_number: int, initial_stage: str):
        """
        Initialize the progress context.
        
        Args:
            item_type: Type of item being processed ("PR" or "Issue")
            item_number: Number of the PR or Issue
            initial_stage: Initial processing stage
        """
        self.item_type = item_type
        self.item_number = item_number
        self.initial_stage = initial_stage

    def __enter__(self):
        """Enter the context and display the initial header."""
        update_progress(self.item_type, self.item_number, self.initial_stage)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context and clear the header."""
        newline_progress()
        clear_progress()
        return False

    def update_stage(self, stage: str) -> None:
        """
        Update the processing stage.

        Args:
            stage: New processing stage
        """
        update_progress(self.item_type, self.item_number, stage)


class ProgressStage:
    """
    Context manager for automatic stage push/pop management.

    Usage:
        with ProgressStage("First pass"):
            # Do work with "First pass" stage
            with ProgressStage("Running LLM"):
                # Do work with "First pass / Running LLM" stage
                pass
            # Back to "First pass" stage
        # Stage is automatically popped on exit
    """

    def __init__(self, stage: str):
        """
        Initialize the progress stage context.

        Args:
            stage: Stage to push onto the stack
        """
        self.stage = stage

    def __enter__(self):
        """Enter the context and push the stage."""
        push_progress_stage(self.stage)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context and pop the stage."""
        pop_progress_stage()
        return False

