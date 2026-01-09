"""
UI helper functions for the CLI.
"""

import os
import shutil
import sys
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


def sleep_with_countdown(seconds: int, stream: Optional[TextIO] = None) -> None:
    """
    Sleep for a specified number of seconds, displaying a countdown with spinner.

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
            remaining_int = int(remaining + 0.9)  # Ceiling integer for display
            hours, remainder = divmod(remaining_int, 3600)
            minutes, secs = divmod(remainder, 60)

            if hours > 0:
                time_str = f"{hours}h {minutes:02d}m {secs:02d}s"
            elif minutes > 0:
                time_str = f"{minutes}m {secs:02d}s"
            else:
                time_str = f"{secs}s"

            spinner = spinner_frames[spinner_idx % len(spinner_frames)]
            spinner_idx += 1

            message = f"{spinner} Sleeping... {time_str} remaining (Ctrl+C to interrupt)"

            # Pad the message to ensure we overwrite previous longer messages
            # Use terminal width if possible, else 80
            cols = shutil.get_terminal_size((80, 20)).columns
            padding = " " * max(0, cols - len(message) - 1)  # -1 to avoid accidental wrap
            padded_message = message + padding

            if not no_color:
                # Dim the text (bright_black is usually dark gray)
                padded_message = click.style(padded_message, fg="bright_black")

            stream.write(f"\r{padded_message}")
            stream.flush()

            # Sleep for a short interval for smoother animation
            # But don't sleep past the end time
            sleep_time = min(0.1, remaining)
            time.sleep(sleep_time)

        # Clear the line after done
        # We need to clear enough space for the longest message
        cols = shutil.get_terminal_size((80, 20)).columns
        stream.write("\r" + " " * (cols - 1) + "\r")
        stream.flush()
    except KeyboardInterrupt:
        # Clear the line and re-raise
        cols = shutil.get_terminal_size((80, 20)).columns
        stream.write("\r" + " " * (cols - 1) + "\r")
        stream.flush()
        raise
