import os
from datetime import datetime, timedelta, timezone

TIMESTAMP_FILE = os.path.expanduser("~/.auto-coder/dependabot_timestamp.txt")


def set_dependabot_pr_processed_time():
    """Record the time a Dependabot PR was processed."""
    os.makedirs(os.path.dirname(TIMESTAMP_FILE), exist_ok=True)
    with open(TIMESTAMP_FILE, "w") as f:
        # Use timezone-aware UTC
        f.write(datetime.now(timezone.utc).isoformat())


def get_last_dependabot_pr_processed_time():
    """Get the last time a Dependabot PR was processed."""
    if not os.path.exists(TIMESTAMP_FILE):
        return None
    with open(TIMESTAMP_FILE, "r") as f:
        content = f.read().strip()
        if not content:
            return None
        return datetime.fromisoformat(content)


def should_process_dependabot_pr(interval_hours: int = 24):
    """Check if a Dependabot PR should be processed."""
    last_processed_time = get_last_dependabot_pr_processed_time()
    if last_processed_time is None:
        return True

    now = datetime.now(timezone.utc)

    # If the stored time is naive (from older versions), assume it is UTC and make it aware
    if last_processed_time.tzinfo is None:
        last_processed_time = last_processed_time.replace(tzinfo=timezone.utc)

    return now - last_processed_time > timedelta(hours=interval_hours)
