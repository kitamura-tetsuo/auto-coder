"""
Progress footer display module for Auto-Coder.

This module provides overprint functionality to display processing status
in the terminal footer with PR/Issue number and processing stage.
"""

import sys
import threading
import shutil
from typing import Optional

from .logger_config import get_logger

logger = get_logger(__name__)


class ProgressFooter:
    """
    Manages progress footer display with overprint functionality.

    This class provides a way to display processing status in the terminal footer
    that updates in place (overprint) rather than creating new lines.
    """

    def __init__(self, stream=sys.stderr):
        """Initialize the progress footer."""
        self._lock = threading.Lock()
        self._current_footer: Optional[str] = None
        self._is_active = False
        self._stream = stream
        # Check if stream is a TTY (supports overprint)
        self._supports_overprint = stream.isatty()
        # Stack for nested stages
        self._stage_stack: list[str] = []
        # Store current item info for re-rendering
        self._current_item_type: Optional[str] = None
        self._current_item_number: Optional[int] = None

    def _format_footer(
        self,
        item_type: str,
        item_number: int,
    ) -> str:
        """
        Format the footer string with nested stages from stack.

        Args:
            item_type: Type of item being processed ("PR" or "Issue")
            item_number: Number of the PR or Issue

        Returns:
            Formatted footer string
        """
        # Use ANSI color codes for better visibility
        # Bright Cyan for item info, Bright Yellow for stages
        # Build nested display: [PR #123] Stage1 / Stage2 / Stage3
        if self._stage_stack:
            all_stages = " / ".join(self._stage_stack)
            return f"\033[96m[{item_type} #{item_number}]\033[0m \033[93m{all_stages}\033[0m"
        else:
            # No stages, just show item info
            return f"\033[96m[{item_type} #{item_number}]\033[0m"

    def print_footer(self) -> None:
        """Print the current footer at the bottom of the terminal."""
        if not self._current_footer or not self._supports_overprint:
            return

        with self._lock:
            cols = shutil.get_terminal_size((80, 20)).columns
            rows = shutil.get_terminal_size((80, 20)).lines
            padded = self._current_footer[:cols].ljust(cols)
            # Save cursor position, move to bottom line, clear line, print footer, restore cursor
            self._stream.write("\0337")                    # save cursor (alt: \033[s)
            self._stream.write(f"\033[{rows};1H")          # move to bottom-left
            self._stream.write("\033[K")                   # clear line
            self._stream.write(padded)
            self._stream.write("\0338")                    # restore cursor (alt: \033[u)
            self._stream.flush()

    def set_item(
        self,
        item_type: str,
        item_number: int,
    ) -> None:
        """
        Set the current item being processed.

        Args:
            item_type: Type of item being processed ("PR" or "Issue")
            item_number: Number of the PR or Issue
        """
        with self._lock:
            self._current_item_type = item_type
            self._current_item_number = item_number
            self._is_active = True
            self._render_footer()

        # Print footer at the bottom
        self.print_footer()

    def _render_footer(self) -> None:
        """Render the footer from current state (must be called with lock held)."""
        if self._current_item_type and self._current_item_number:
            self._current_footer = self._format_footer(
                self._current_item_type, self._current_item_number
            )

    def sink_wrapper(self, message):
        """
        Loguru sink wrapper that clears footer before log, then re-prints after.

        Args:
            message: The log message from loguru
        """
        text = str(message)

        # Clear footer before writing log (to avoid old footers in scrollback)
        if self._current_footer and self._supports_overprint:
            with self._lock:
                cols = shutil.get_terminal_size((80, 20)).columns
                rows = shutil.get_terminal_size((80, 20)).lines
                self._stream.write("\0337")                    # save cursor
                self._stream.write(f"\033[{rows};1H")          # move to bottom-left
                self._stream.write("\033[K")                   # clear line
                self._stream.write("\0338")                    # restore cursor
                self._stream.flush()

        # Write log message
        with self._lock:
            self._stream.write(text)
            self._stream.flush()

        # Re-print footer after log output
        self.print_footer()

    def push_stage(self, stage: str) -> None:
        """
        Push a stage onto the stack for nested display and re-render the footer.

        Args:
            stage: Stage to push onto the stack
        """
        with self._lock:
            self._stage_stack.append(stage)
            # Log for file logging
            if self._current_item_type and self._current_item_number:
                logger.debug(
                    f"Progress: {self._current_item_type} #{self._current_item_number} - {' / '.join(self._stage_stack)}"
                )
            self._render_footer()

        # Print footer outside the lock
        self.print_footer()

    def pop_stage(self) -> None:
        """Pop the last stage from the stack and re-render the footer."""
        with self._lock:
            if self._stage_stack:
                self._stage_stack.pop()
            self._render_footer()

        # Print footer outside the lock
        self.print_footer()

    def clear(self) -> None:
        """Clear the current progress footer."""
        with self._lock:
            if self._is_active and self._supports_overprint:
                # Clear the bottom line
                cols = shutil.get_terminal_size((80, 20)).columns
                rows = shutil.get_terminal_size((80, 20)).lines
                self._stream.write("\0337")                    # save cursor
                self._stream.write(f"\033[{rows};1H")          # move to bottom-left
                self._stream.write("\033[K")                   # clear line
                self._stream.write("\0338")                    # restore cursor
                self._stream.flush()

            self._current_footer = None
            self._is_active = False
            self._stage_stack.clear()

    def newline(self) -> None:
        """
        Print a newline to move past the current footer.

        This should be called before printing regular output to ensure
        the footer doesn't get overwritten.
        """
        with self._lock:
            if self._is_active and self._supports_overprint:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._is_active = False


# Global instance for use across the application
_global_footer: Optional[ProgressFooter] = None
_global_footer_lock = threading.Lock()


def get_progress_footer() -> ProgressFooter:
    """
    Get the global progress footer instance.

    Returns:
        The global ProgressFooter instance
    """
    global _global_footer

    with _global_footer_lock:
        if _global_footer is None:
            _global_footer = ProgressFooter()
        return _global_footer


def setup_progress_footer_logging() -> None:
    """
    Setup loguru to use progress footer sink wrapper.

    This should be called early in the application startup to ensure
    all log messages are properly wrapped with footer re-printing.
    """
    from .logger_config import setup_logger

    footer = get_progress_footer()
    # Re-setup logger with progress footer sink
    setup_logger(progress_footer=footer)


def set_progress_item(item_type: str, item_number: int) -> None:
    """
    Set the current item being processed in the global progress footer.

    Args:
        item_type: Type of item being processed ("PR" or "Issue")
        item_number: Number of the PR or Issue
    """
    footer = get_progress_footer()
    footer.set_item(item_type, item_number)


def push_progress_stage(stage: str) -> None:
    """
    Push a stage onto the global progress footer stack.

    Args:
        stage: Stage to push onto the stack
    """
    footer = get_progress_footer()
    footer.push_stage(stage)


def pop_progress_stage() -> None:
    """Pop the last stage from the global progress footer stack."""
    footer = get_progress_footer()
    footer.pop_stage()


def clear_progress() -> None:
    """Clear the global progress footer."""
    footer = get_progress_footer()
    footer.clear()


def newline_progress() -> None:
    """Print a newline to move past the current footer."""
    footer = get_progress_footer()
    footer.newline()


# Context manager for automatic footer management
class ProgressContext:
    """
    Context manager for automatic progress footer management.

    Usage:
        with ProgressContext("PR", 123, "Analyzing"):
            # Do work
            pass
        # Footer is automatically cleared on exit
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
        """Enter the context and display the initial footer."""
        set_progress_item(self.item_type, self.item_number)
        push_progress_stage(self.initial_stage)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context and clear the footer."""
        newline_progress()
        clear_progress()
        return False

    def update_stage(self, stage: str) -> None:
        """
        Update the processing stage by popping old and pushing new.

        Args:
            stage: New processing stage
        """
        pop_progress_stage()
        push_progress_stage(stage)


class ProgressStage:
    """
    Context manager for automatic stage push/pop management.

    Usage:
        # With item info (sets the current item)
        with ProgressStage("PR", 123, "First pass"):
            # Footer: [PR #123] First pass
            with ProgressStage("Running LLM"):
                # Footer: [PR #123] First pass / Running LLM
                pass
            # Footer: [PR #123] First pass
        # Footer cleared

        # Without item info (just adds to the stage stack)
        with ProgressStage("First pass"):
            # Adds "First pass" to the stack
            pass
    """

    def __init__(self, *args):
        """
        Initialize the progress stage context.

        Args:
            *args: Can be:
                - (stage: str): Just push a stage onto the stack
                - (item_type: str, item_number: int, stage: str): Set item info and push stage
        """
        if len(args) == 1:
            # Just a stage
            self.item_type = None
            self.item_number = None
            self.stage = args[0]
        elif len(args) == 3:
            # Item type, number, and stage
            self.item_type = args[0]
            self.item_number = args[1]
            self.stage = args[2]
        else:
            raise ValueError(
                "ProgressStage requires either 1 argument (stage) or 3 arguments (item_type, item_number, stage)"
            )

    def __enter__(self):
        """Enter the context and push the stage."""
        if self.item_type and self.item_number:
            # Set item info and push stage
            set_progress_item(self.item_type, self.item_number)
            push_progress_stage(self.stage)
        else:
            # Just push stage
            push_progress_stage(self.stage)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context and pop the stage."""
        if self.item_type and self.item_number:
            # Pop stage and clear item info
            pop_progress_stage()
            clear_progress()
        else:
            # Just pop stage
            pop_progress_stage()
        return False

