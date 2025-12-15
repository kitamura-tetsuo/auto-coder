import os
import json
import unittest
from unittest.mock import patch, MagicMock
from dataclasses import asdict
import datetime

from src.auto_coder.log_utils import (
    get_log_dir,
    get_raw_log_dir,
    write_log_entry,
    create_log_entry,
    LogEntry,
)


class TestLogUtils(unittest.TestCase):
    @patch("src.auto_coder.log_utils.get_current_repo_name")
    @patch("os.path.expanduser")
    @patch("os.makedirs")
    def test_get_log_dir(self, mock_makedirs, mock_expanduser, mock_get_repo_name):
        mock_get_repo_name.return_value = "owner/repo"
        expected_dir = "/tmp/.auto-coder/owner_repo/test_log"
        mock_expanduser.return_value = expected_dir

        log_dir = get_log_dir()

        self.assertEqual(log_dir, expected_dir)
        mock_expanduser.assert_called_with("~/.auto-coder/owner_repo/test_log")
        mock_makedirs.assert_called_with(expected_dir, exist_ok=True)

    @patch("src.auto_coder.log_utils.get_current_repo_name")
    def test_get_log_dir_no_repo(self, mock_get_repo_name):
        mock_get_repo_name.return_value = None
        self.assertIsNone(get_log_dir())

    @patch("src.auto_coder.log_utils.get_log_dir")
    @patch("os.makedirs")
    def test_get_raw_log_dir(self, mock_makedirs, mock_get_log_dir):
        mock_get_log_dir.return_value = "/tmp/log_dir"
        expected_dir = "/tmp/log_dir/raw"

        raw_log_dir = get_raw_log_dir()

        self.assertEqual(raw_log_dir, expected_dir)
        mock_makedirs.assert_called_with(expected_dir, exist_ok=True)

    @patch("src.auto_coder.log_utils.get_log_dir")
    def test_get_raw_log_dir_no_log_dir(self, mock_get_log_dir):
        mock_get_log_dir.return_value = None
        self.assertIsNone(get_raw_log_dir())

    @patch("src.auto_coder.log_utils.get_raw_log_dir")
    @patch("builtins.open", new_callable=unittest.mock.mock_open)
    @patch("json.dump")
    def test_write_log_entry(self, mock_json_dump, mock_open, mock_get_raw_log_dir):
        mock_get_raw_log_dir.return_value = "/tmp/raw_log_dir"
        log_entry = LogEntry(
            test_file="test.py",
            stdout="out",
            stderr="err",
            exit_code=1,
            success=False,
            timestamp="2023-01-01T00:00:00",
        )
        expected_path = "/tmp/raw_log_dir/2023-01-01T00:00:00.json"

        write_log_entry(log_entry)

        mock_open.assert_called_with(expected_path, "w")
        mock_json_dump.assert_called_with(asdict(log_entry), mock_open(), indent=2)

    @patch("src.auto_coder.log_utils.get_raw_log_dir")
    def test_write_log_entry_no_raw_log_dir(self, mock_get_raw_log_dir):
        mock_get_raw_log_dir.return_value = None
        log_entry = LogEntry(
            test_file="test.py",
            stdout="out",
            stderr="err",
            exit_code=1,
            success=False,
            timestamp="2023-01-01T00:00:00",
        )
        # Just ensure no exception is raised
        write_log_entry(log_entry)


    def test_create_log_entry(self):
        test_file = "test.py"
        stdout = "output"
        stderr = "error"
        exit_code = 1
        success = False

        log_entry = create_log_entry(test_file, stdout, stderr, exit_code, success)

        self.assertEqual(log_entry.test_file, test_file)
        self.assertEqual(log_entry.stdout, stdout)
        self.assertEqual(log_entry.stderr, stderr)
        self.assertEqual(log_entry.exit_code, exit_code)
        self.assertEqual(log_entry.success, success)

        # Check timestamp is a valid ISO 8601 format
        try:
            datetime.datetime.fromisoformat(log_entry.timestamp)
        except ValueError:
            self.fail("Timestamp is not in valid ISO 8601 format")

if __name__ == "__main__":
    unittest.main()
