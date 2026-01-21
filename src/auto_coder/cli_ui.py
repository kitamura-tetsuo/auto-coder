"""
UI helper functions for the CLI.
"""

import math
import os
import sys
import threading
import time
from typing import Any, Dict, Optional, TextIO

import click

SPINNER_FRAMES_UNICODE = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SPINNER_FRAMES_ASCII = ["|", "/", "-", "\\"]


def print_configuration_summary(title: str, config: Dict[str, Any]) -> None:
    """
    Prints a formatted summary of the configuration.

    Args:
        title: The title of the summary section.
        config: A dictionary of configuration items (key: label, value: setting).
    """
    # NO_COLOR standard: disable color if NO_COLOR env var is present (regardless of value)
    no_color = "NO_COLOR" in os.environ

    # Calculate padding for alignment
    if not config:
        return

    max_key_len = max(len(str(k)) for k in config.keys())

    # Print Title
    if not no_color:
        # Blue title with an emoji
        # Using secho which combines style and echo
        click.secho(f"🎨 {title}", bold=True, fg="blue")
    else:
        click.echo(f"{title}")

    for key, value in config.items():
        key_str = str(key)
        padding = " " * (max_key_len - len(key_str))

        # Format Key
        if not no_color:
            # Cyan for keys
            key_display = click.style(f"  • {key_str}{padding}", fg="cyan")
        else:
            key_display = f"  • {key_str}{padding}"

        # Format Value
        val_str = str(value)
        if not no_color:
            if value is True or (isinstance(value, str) and value.lower().startswith("enabled")):
                val_display = click.style(val_str, fg="green")
            elif value is False or (isinstance(value, str) and (value.lower().startswith("disabled") or "skip" in value.lower())):
                # "SKIP (default)" or "Disabled" -> yellow
                val_display = click.style(val_str, fg="yellow")
            else:
                val_display = val_str
        else:
            val_display = val_str

        click.echo(f"{key_display} : {val_display}")

    click.echo("")  # Add spacing after summary


def print_lock_error(lock_info: Any, is_running: bool) -> None:
    """
    Prints a formatted error message when the application is locked.

    Args:
        lock_info: Object containing lock details (pid, hostname, started_at).
        is_running: Boolean indicating if the process is still active.
    """
    no_color = "NO_COLOR" in os.environ

    # Header
    if not no_color:
        click.secho("\n🔒 auto-coder is already running!", fg="red", bold=True, err=True)
    else:
        click.echo("\nError: auto-coder is already running!", err=True)

    click.echo("", err=True)

    # Lock Info Box
    if not no_color:
        click.secho("  Lock Information:", fg="yellow", bold=True, err=True)
        click.echo(f"  {'PID':<12}: {click.style(str(lock_info.pid), fg='cyan')}", err=True)
        click.echo(f"  {'Hostname':<12}: {click.style(lock_info.hostname, fg='cyan')}", err=True)
        click.echo(f"  {'Started at':<12}: {click.style(lock_info.started_at, fg='cyan')}", err=True)
    else:
        click.echo("  Lock Information:", err=True)
        click.echo(f"  PID         : {lock_info.pid}", err=True)
        click.echo(f"  Hostname    : {lock_info.hostname}", err=True)
        click.echo(f"  Started at  : {lock_info.started_at}", err=True)

    click.echo("", err=True)

    # Status & Action
    if is_running:
        msg = "The process is still running. Please wait for it to complete."
        if not no_color:
            click.secho(f"  ⚠️  {msg}", fg="yellow", err=True)
        else:
            click.echo(msg, err=True)
    else:
        status_msg = "The process is no longer running (stale lock)."
        action_msg = "You can use '--force' to override or run 'auto-coder unlock' to remove the lock."

        if not no_color:
            click.secho(f"  ❌ {status_msg}", fg="red", err=True)
            click.echo(f"  💡 {action_msg}", err=True)
        else:
            click.echo(status_msg, err=True)
            click.echo(action_msg, err=True)

    click.echo("", err=True)


def sleep_with_countdown(seconds: int, stream: Optional[TextIO] = None) -> None:
    """
    Sleep for a specified number of seconds, displaying a countdown.

    Args:
        seconds: Number of seconds to sleep.
        stream: Output stream to write to (defaults to sys.stdout).
    """
    if stream is None:
        stream = sys.stdout

    if seconds <= 0:
        return

    # Check if we are in a non-interactive environment
    if not stream.isatty():
        time.sleep(seconds)
        return

    no_color = "NO_COLOR" in os.environ
    spinner_frames = SPINNER_FRAMES_ASCII if no_color else SPINNER_FRAMES_UNICODE

    end_time = time.time() + seconds
    spinner_idx = 0

    try:
        while True:
            remaining = end_time - time.time()
            if remaining <= 0:
                break

            # Format time nicely
            remaining_int = int(math.ceil(remaining))
            hours, remainder = divmod(remaining_int, 3600)
            minutes, secs = divmod(remainder, 60)

            if hours > 0:
                time_str = f"{hours}h {minutes:02d}m {secs:02d}s"
            elif minutes > 0:
                time_str = f"{minutes}m {secs:02d}s"
            else:
                time_str = f"{secs}s"

            spinner = spinner_frames[spinner_idx % len(spinner_frames)]
            message = f"{spinner} Sleeping... {time_str} remaining (Ctrl+C to interrupt)"

            if not no_color:
                # Dim the text (bright_black is usually dark gray)
                message = click.style(message, fg="bright_black")

            stream.write(f"\r{message}")
            stream.flush()

            time.sleep(0.1)
            spinner_idx += 1

        # Clear the line after done
        # We need to clear enough space for the longest message
        stream.write("\r" + " " * 80 + "\r")
        stream.flush()
    except KeyboardInterrupt:
        # Clear the line and re-raise
        stream.write("\r" + " " * 80 + "\r")
        stream.flush()
        raise


class Spinner:
    """
    A context manager that displays a spinner animation while a block of code executes.
    """

    def __init__(self, message: str = "Loading...", delay: float = 0.1):
        self.message = message
        self.delay = delay
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.no_color = "NO_COLOR" in os.environ

    def spin(self) -> None:
        spinner_frames = SPINNER_FRAMES_ASCII if self.no_color else SPINNER_FRAMES_UNICODE
        idx = 0

        while not self.stop_event.is_set():
            frame = spinner_frames[idx % len(spinner_frames)]
            if self.no_color:
                msg = f"{frame} {self.message}"
            else:
                msg = click.style(f"{frame} {self.message}", fg="cyan")

            sys.stdout.write(f"\r{msg}")
            sys.stdout.flush()

            # Wait for delay or stop event
            if self.stop_event.wait(self.delay):
                break

            idx += 1

    def __enter__(self) -> "Spinner":
        if sys.stdout.isatty():
            self.thread = threading.Thread(target=self.spin)
            self.thread.start()
        else:
            # If not a TTY, just print the message once
            sys.stdout.write(f"{self.message}\n")
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, exc_traceback: Any) -> None:
        if self.thread:
            self.stop_event.set()
            self.thread.join()

            # Clear the line first
            sys.stdout.write("\r" + " " * (len(self.message) + 10) + "\r")

            # Determine symbol and color based on success/failure
            if self.no_color:
                symbol = "[OK]" if exc_type is None else "[ERR]"
                color_func = lambda x, **kwargs: x
            else:
                symbol = "✅" if exc_type is None else "❌"
                color_func = click.style

            final_msg = f"{symbol} {self.message}"

            if not self.no_color:
                if exc_type is None:
                    final_msg = color_func(final_msg, fg="green")
                else:
                    final_msg = color_func(final_msg, fg="red")

            sys.stdout.write(f"{final_msg}\n")
            sys.stdout.flush()
