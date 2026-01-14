import os
from datetime import datetime, timedelta

TIMESTAMP_FILE = os.path.expanduser("~/.auto-coder/dependabot_timestamp.txt")


def set_dependabot_pr_processed_time():
    """Record the time a Dependabot PR was processed."""
    os.makedirs(os.path.dirname(TIMESTAMP_FILE), exist_ok=True)
    with open(TIMESTAMP_FILE, "w") as f:
        f.write(datetime.utcnow().isoformat())


def get_last_dependabot_pr_processed_time():
    """Get the last time a Dependabot PR was processed."""
    if not os.path.exists(TIMESTAMP_FILE):
        return None
    with open(TIMESTAMP_FILE, "r") as f:
        return datetime.fromisoformat(f.read().strip())


def should_process_dependabot_pr(interval_hours: int = 24):
    """Check if a Dependabot PR should be processed."""
    last_processed_time = get_last_dependabot_pr_processed_time()
    if last_processed_time is None:
        return True
    return datetime.utcnow() - last_processed_time > timedelta(hours=interval_hours)
