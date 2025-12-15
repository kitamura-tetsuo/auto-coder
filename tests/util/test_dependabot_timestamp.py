"""
Tests for the Dependabot timestamp manager.
"""
import os
from datetime import datetime, timezone
from unittest.mock import patch

from auto_coder.util.dependabot_timestamp import get_last_dependabot_run, set_last_dependabot_run, get_timestamp_path

def test_dependabot_timestamp():
    """Tests the Dependabot timestamp manager."""
    with patch("auto_coder.util.dependabot_timestamp.get_timestamp_path") as mock_get_path:
        mock_get_path.return_value = "/tmp/dependabot_timestamp.txt"

        # Test that the timestamp is None when the file doesn't exist
        if os.path.exists(mock_get_path.return_value):
            os.remove(mock_get_path.return_value)
        assert get_last_dependabot_run() is None

        # Test that the timestamp is set correctly
        set_last_dependabot_run()
        assert os.path.exists(mock_get_path.return_value)

        # Test that the timestamp is read correctly
        timestamp = get_last_dependabot_run()
        assert timestamp is not None
        assert isinstance(timestamp, datetime)
        assert (datetime.now(timezone.utc) - timestamp).total_seconds() < 5
