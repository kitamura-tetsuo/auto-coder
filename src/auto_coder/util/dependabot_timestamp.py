"""
This module manages the timestamp of the last Dependabot PR processing.
"""
import os
from datetime import datetime, timezone

def get_timestamp_path():
    """Returns the path to the dependabot timestamp file."""
    return os.path.expanduser("~/.auto-coder/dependabot_timestamp.txt")

def set_last_dependabot_run():
    """Sets the last run timestamp for Dependabot PR processing."""
    path = get_timestamp_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())

def get_last_dependabot_run():
    """Gets the last run timestamp for Dependabot PR processing."""
    path = get_timestamp_path()
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return datetime.fromisoformat(f.read().strip())
