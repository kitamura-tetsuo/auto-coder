"""
Progress footer display module for Auto-Coder.

This module provides overprint functionality to display processing status
in the terminal footer with PR/Issue number and processing stage.
"""

import os
import shutil
import sys
import threading
import time
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
        # Check for NO_COLOR environment variable
        self._no_color = bool(os.environ.get("NO_COLOR"))
        # Stack for nested stages
        self._stage_stack: list[str] = []
        # Store current item info for re-rendering
        self._current_item_type: Optional[str] = None
        self._current_item_number: Optional[int] = None
        # Store related issues and branch name
        self._related_issues: list[int] = []
        self._branch_name: Optional[str] = None

        # Timer state
        self._start_time: Optional[float] = None

        # Spinner state
        if self._no_color:
            self._spinner_frames = ["|", "/", "-", "\\"]
        else:
            self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0

    def _format_footer(
        self,
        item_type: str,
        item_number: Optional[int],
    ) -> str:
        """
        Format the footer string with nested stages from stack.

        Args:
            item_type: Type of item being processed ("PR" or "Issue")
            item_number: Number of the PR or Issue (optional)

        Returns:
            Formatted footer string
        """
        # Define colors and symbols
        if self._no_color:
            c_cyan = ""
            c_magenta = ""
            c_red = ""
            c_yellow = ""
            c_reset = ""
            sym_pr = "PR"
            sym_issue = "Issue"
            sym_sep = " / "
            sym_branch = "/"
        else:
            c_cyan = "\033[96m"
            c_magenta = "\033[95m"
            c_red = "\033[91m"
            c_yellow = "\033[93m"
            c_reset = "\033[0m"
            sym_pr = "🔀 PR"
            sym_issue = "🐛 Issue"
            sym_sep = " › "
            sym_branch = " 🌿 "

        # Build the main item display with color based on item_type
        spinner = self._spinner_frames[self._spinner_idx] + " "

        if item_number is not None:
            if item_type.upper() == "PR":
                # PR: cyan color
                main_display = f"{c_cyan}{spinner}[{sym_pr} #{item_number}"
            elif item_type.upper() == "ISSUE":
                # Issue: light purple/magenta
                main_display = f"{c_magenta}{spinner}[{sym_issue} #{item_number}"
            else:
                # Fallback: use cyan
                main_display = f"{c_cyan}{spinner}[{item_type} #{item_number}"
        else:
            # Just show item_type as label (e.g. "Waiting...")
            # Use cyan for generic system messages
            main_display = f"{c_cyan}{spinner}[{item_type}"

        # Add branch name if available (in dark red)
        if self._branch_name:
            main_display += f"{c_red}{sym_branch}{self._branch_name}{c_reset}"

        main_display += f"]{c_reset}"

        # Add related issues if available (without space before it)
        if self._related_issues:
            related_issues_str = ", ".join([f"#{issue}" for issue in self._related_issues])
            main_display += f"{c_magenta}[{sym_issue} {related_issues_str}]{c_reset}"

        # Add stages if available
        if self._stage_stack:
            all_stages = sym_sep.join(self._stage_stack)
            formatted = f"{main_display}{sym_sep}{c_yellow}{all_stages}{c_reset}"
        else:
            # No stages, just show main info
            formatted = main_display

        # Add elapsed time if available
        if self._start_time:
            elapsed = int(time.time() - self._start_time)
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)

            if hours > 0:
                time_str = f"[{hours}h {minutes:02d}m {seconds:02d}s]"
            elif minutes > 0:
                time_str = f"[{minutes}m {seconds:02d}s]"
            else:
                time_str = f"[{seconds}s]"

            if self._no_color:
                formatted += f" {time_str}"
            else:
                # Dark gray (90m) for timer
                formatted += f" \033[90m{time_str}{c_reset}"

        return formatted

    def tick(self) -> None:
        """Advance the spinner and reprint the footer."""
        if not self._is_active:
            return

        with self._lock:
            self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
            self._render_footer()

        # Print footer outside the lock
        self.print_footer()

    def print_footer(self) -> None:
        """Print the current footer at the bottom of the terminal."""
        if not self._supports_overprint:
            return

        with self._lock:
            # Re-check _current_footer inside the lock to avoid race conditions
            if not self._current_footer:
                return

            cols = shutil.get_terminal_size((80, 20)).columns
            rows = shutil.get_terminal_size((80, 20)).lines
            padded = self._current_footer[:cols].ljust(cols)
            # Save cursor position, move to bottom line, clear line, print footer, restore cursor
            self._stream.write("\0337")  # save cursor (alt: \033[s)
            self._stream.write(f"\033[{rows};1H")  # move to bottom-left
            self._stream.write("\033[K")  # clear line
            self._stream.write(padded)
            self._stream.write("\0338")  # restore cursor (alt: \033[u)
            self._stream.flush()

    def set_item(
        self,
        item_type: str,
        item_number: Optional[int],
        related_issues: Optional[list[int]] = None,
        branch_name: Optional[str] = None,
    ) -> None:
        """
        Set the current item being processed.

        Args:
            item_type: Type of item being processed ("PR" or "Issue")
            item_number: Number of the PR or Issue (optional)
            related_issues: List of related issue numbers
            branch_name: Branch name to display
        """
        with self._lock:
            # Reset timer if item changes or if it wasn't set
            if self._current_item_type != item_type or self._current_item_number != item_number or self._start_time is None:
                self._start_time = time.time()

            self._current_item_type = item_type
            self._current_item_number = item_number
            self._related_issues = related_issues or []
            self._branch_name = branch_name
            self._is_active = True
            self._render_footer()

        # Print footer at the bottom
        self.print_footer()

    def _render_footer(self) -> None:
        """Render the footer from current state (must be called with lock held)."""
        if self._current_item_type:
            self._current_footer = self._format_footer(self._current_item_type, self._current_item_number)
        else:
            self._current_footer = None

    def sink_wrapper(self, message):
        """
        Loguru sink wrapper that clears footer before log, then re-prints after.

        Args:
            message: The log message from loguru
        """
        text = str(message)

        # Clear footer before writing log (to avoid old footers in scrollback)
        if self._supports_overprint:
            with self._lock:
                # Re-check _current_footer inside the lock to avoid race conditions
                if self._current_footer:
                    rows = shutil.get_terminal_size((80, 20)).lines
                    self._stream.write("\0337")  # save cursor
                    self._stream.write(f"\033[{rows};1H")  # move to bottom-left
                    self._stream.write("\033[K")  # clear line
                    self._stream.write("\0338")  # restore cursor
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
                logger.debug(f"Progress: {self._current_item_type} #{self._current_item_number} - " f"{' / '.join(self._stage_stack)}")
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
                rows = shutil.get_terminal_size((80, 20)).lines
                self._stream.write("\0337")  # save cursor
                self._stream.write(f"\033[{rows};1H")  # move to bottom-left
                self._stream.write("\033[K")  # clear line
                self._stream.write("\0338")  # restore cursor
                self._stream.flush()

            self._current_footer = None
            self._is_active = False
            self._stage_stack.clear()
            self._current_item_type = None
            self._current_item_number = None
            self._related_issues = []
            self._branch_name = None
            self._start_time = None

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


def set_progress_item(
    item_type: str,
    item_number: Optional[int],
    related_issues: Optional[list[int]] = None,
    branch_name: Optional[str] = None,
) -> None:
    """
    Set the current item being processed in the global progress footer.

    Args:
        item_type: Type of item being processed ("PR" or "Issue")
        item_number: Number of the PR or Issue (optional)
        related_issues: List of related issue numbers
        branch_name: Branch name to display
    """
    footer = get_progress_footer()
    footer.set_item(item_type, item_number, related_issues, branch_name)


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

    def __init__(
        self,
        item_type: str,
        item_number: Optional[int],
        initial_stage: str,
        related_issues: Optional[list[int]] = None,
        branch_name: Optional[str] = None,
    ):
        """
        Initialize the progress context.

        Args:
            item_type: Type of item being processed ("PR" or "Issue")
            item_number: Number of the PR or Issue
            initial_stage: Initial processing stage
            related_issues: List of related issue numbers
            branch_name: Branch name to display
        """
        self.item_type = item_type
        self.item_number = item_number
        self.initial_stage = initial_stage
        self.related_issues = related_issues
        self.branch_name = branch_name
        # Use ProgressStage context manager for automatic stage push/pop
        self._progress_stage = None

    def __enter__(self):
        """Enter the context and display the initial footer."""
        set_progress_item(self.item_type, self.item_number, self.related_issues, self.branch_name)
        # Use ProgressStage context manager for the initial stage
        self._progress_stage = ProgressStage(self.initial_stage)
        self._progress_stage.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context and clear the footer."""
        # Exit the ProgressStage context first
        if self._progress_stage:
            self._progress_stage.__exit__(exc_type, exc_val, exc_tb)
        newline_progress()
        clear_progress()
        return False

    def update_stage(self, stage: str) -> None:
        """
        Update the processing stage by replacing the context manager.

        Args:
            stage: New processing stage
        """
        # Exit the current ProgressStage and enter a new one
        if self._progress_stage:
            self._progress_stage.__exit__(None, None, None)
        self._progress_stage = ProgressStage(stage)  # type: ignore[assignment]
        self._progress_stage.__enter__()  # type: ignore[union-attr,attr-defined]


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

    def __init__(self, *args, **kwargs):
        """
        Initialize the progress stage context.

        Args:
            *args: Can be:
                - (stage: str): Just push a stage onto the stack
                - (item_type: str, item_number: int, stage: str): Set item info and push stage
                - (item_type: str, item_number: int, stage: str, related_issues: list[int], branch_name: str): Full info
            **kwargs: Can include related_issues and branch_name for the 3-argument version
        """
        if len(args) == 1:
            # Just a stage
            self.item_type = None
            self.item_number = None
            self.stage = args[0]
            self.related_issues = None
            self.branch_name = None
        elif len(args) == 3:
            # Item type, number, and stage
            self.item_type = args[0]
            self.item_number = args[1]
            self.stage = args[2]
            self.related_issues = kwargs.get("related_issues")
            self.branch_name = kwargs.get("branch_name")
        elif len(args) == 5:
            # Full info: item_type, item_number, stage, related_issues, branch_name
            self.item_type = args[0]
            self.item_number = args[1]
            self.stage = args[2]
            self.related_issues = args[3]
            self.branch_name = args[4]
        else:
            raise ValueError("ProgressStage requires either 1 argument (stage), 3 arguments (item_type, item_number, stage), or 5 arguments (item_type, item_number, stage, related_issues, branch_name)")

    def __enter__(self):
        """Enter the context and push the stage."""
        footer = get_progress_footer()
        if self.item_type and self.item_number:
            # Set item info and push stage
            set_progress_item(self.item_type, self.item_number, self.related_issues, self.branch_name)
            footer.push_stage(self.stage)
        else:
            # Just push stage
            footer.push_stage(self.stage)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context and pop the stage."""
        footer = get_progress_footer()
        if self.item_type and self.item_number:
            # Pop stage and clear item info
            footer.pop_stage()
            clear_progress()
        else:
            # Just pop stage
            footer.pop_stage()
        return False
