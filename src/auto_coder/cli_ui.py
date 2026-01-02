"""
UI helper functions for the CLI.
"""

import os
import sys
import time
from typing import Any, Dict, Optional, TextIO

import click


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


def sleep_with_countdown(seconds: int, stream: Optional[TextIO] = None, message: str = "Sleeping...") -> None:
    """
    Sleep for a specified number of seconds, displaying a countdown.

    Args:
        seconds: Number of seconds to sleep.
        stream: Output stream to write to (defaults to sys.stdout).
        message: Message to display before the countdown (defaults to "Sleeping...").
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

    try:
        for remaining in range(seconds, 0, -1):
            # Format time nicely
            hours, remainder = divmod(remaining, 3600)
            minutes, secs = divmod(remainder, 60)

            if hours > 0:
                time_str = f"{hours}h {minutes:02d}m {secs:02d}s"
            elif minutes > 0:
                time_str = f"{minutes}m {secs:02d}s"
            else:
                time_str = f"{secs}s"

            display_message = f"{message} {time_str} remaining (Ctrl+C to interrupt)"

            if not no_color:
                # Dim the text (bright_black is usually dark gray)
                display_message = click.style(display_message, fg="bright_black")

            stream.write(f"\r{display_message}")
            stream.flush()
            time.sleep(1)

        # Clear the line after done
        # We need to clear enough space for the longest message
        stream.write("\r" + " " * 80 + "\r")
        stream.flush()
    except KeyboardInterrupt:
        # Clear the line and re-raise
        stream.write("\r" + " " * 80 + "\r")
        stream.flush()
        raise
