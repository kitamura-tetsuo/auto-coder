import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.auto_coder.util.dependabot_timestamp import get_last_dependabot_pr_processed_time, set_dependabot_pr_processed_time, should_process_dependabot_pr


def test_should_process_dependabot_pr_no_timestamp_file():
    with patch("os.path.exists", return_value=False):
        assert should_process_dependabot_pr() is True


def test_should_process_dependabot_pr_timestamp_file_recent():
    with patch("src.auto_coder.util.dependabot_timestamp.get_last_dependabot_pr_processed_time", return_value=datetime.now(timezone.utc) - timedelta(hours=1)):
        assert should_process_dependabot_pr() is False


def test_should_process_dependabot_pr_timestamp_file_old():
    with patch("src.auto_coder.util.dependabot_timestamp.get_last_dependabot_pr_processed_time", return_value=datetime.now(timezone.utc) - timedelta(hours=25)):
        assert should_process_dependabot_pr() is True


def test_set_and_get_last_dependabot_pr_processed_time(tmpdir):
    timestamp_file = tmpdir.join("dependabot_timestamp.txt")
    with patch("src.auto_coder.util.dependabot_timestamp.TIMESTAMP_FILE", str(timestamp_file)):
        set_dependabot_pr_processed_time()
        assert os.path.exists(str(timestamp_file))
        last_processed_time = get_last_dependabot_pr_processed_time()
        assert last_processed_time is not None
        assert isinstance(last_processed_time, datetime)
        assert (datetime.now(timezone.utc) - last_processed_time).total_seconds() < 5
