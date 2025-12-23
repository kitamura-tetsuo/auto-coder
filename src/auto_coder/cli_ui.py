"""
UI helper functions for the CLI.
"""

import os
import sys
import time
from typing import Any, Dict

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


def sleep_with_countdown(seconds: int, message: str = "Sleeping") -> None:
    """
    Sleeps for a specified number of seconds, displaying a countdown.

    Args:
        seconds: Number of seconds to sleep.
        message: Message to display before the countdown.
    """
    if seconds <= 0:
        return

    # Check for NO_COLOR or non-interactive terminal
    no_color = "NO_COLOR" in os.environ
    # We use sys.stdout.isatty() to check if we are in an interactive terminal
    is_tty = sys.stdout.isatty()

    if not is_tty:
        # Fallback for non-interactive environments
        time.sleep(seconds)
        return

    try:
        # Save cursor
        sys.stdout.write("\0337")

        for remaining in range(seconds, 0, -1):
            # Calculate minutes and seconds
            minutes, secs = divmod(remaining, 60)

            # Create time string
            if minutes > 0:
                time_str = f"{minutes}m {secs:02d}s"
            else:
                time_str = f"{secs}s"

            # Create countdown string with color
            if not no_color:
                # Use cyan for the message and yellow for the time
                msg_display = click.style(f"⏳ {message}", fg="cyan")
                time_display = click.style(time_str, fg="yellow", bold=True)
                countdown_str = f"\r{msg_display}: {time_display}   "
            else:
                countdown_str = f"\r{message}: {time_str}   "

            sys.stdout.write(countdown_str)
            sys.stdout.flush()
            time.sleep(1)

        # Clear the line after countdown is done
        sys.stdout.write("\r\033[K")  # Return to start and clear line
        sys.stdout.flush()

    except KeyboardInterrupt:
        # Restore cursor and re-raise
        sys.stdout.write("\0338")
        sys.stdout.write("\033[K")
        sys.stdout.flush()
        raise
