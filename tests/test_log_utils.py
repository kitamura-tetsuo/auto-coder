
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from src.auto_coder.log_utils import (LogEntry, get_sanitized_repo_name,
                                      setup_test_log_dir)


class TestLogUtils(unittest.TestCase):

    @patch('src.auto_coder.log_utils.get_current_repo_name')
    def test_get_sanitized_repo_name(self, mock_get_current_repo_name):
        mock_get_current_repo_name.return_value = "owner/repo-name"
        self.assertEqual(get_sanitized_repo_name(), "owner_repo-name")

        mock_get_current_repo_name.return_value = "owner/repo/name"
        self.assertEqual(get_sanitized_repo_name(), "owner_repo_name")

        mock_get_current_repo_name.return_value = "owner"
        self.assertEqual(get_sanitized_repo_name(), "owner")

        mock_get_current_repo_name.return_value = None
        self.assertIsNone(get_sanitized_repo_name())

    @patch('src.auto_coder.log_utils.get_sanitized_repo_name')
    def test_setup_test_log_dir(self, mock_get_sanitized_repo_name):
        mock_get_sanitized_repo_name.return_value = "test_repo"
        log_dir = setup_test_log_dir()
        self.assertIsNotNone(log_dir)
        self.assertTrue(log_dir.exists())
        self.assertTrue(log_dir.is_dir())
        self.assertEqual(log_dir.name, "raw")

        # Clean up the created directory
        shutil.rmtree(Path.home() / ".auto-coder" / "test_repo")

        mock_get_sanitized_repo_name.return_value = None
        self.assertIsNone(setup_test_log_dir())

    def test_log_entry_to_dict(self):
        log_entry = LogEntry(
            timestamp=1620000000.0,
            test_file="test_example.py",
            stdout=".",
            stderr="",
            exit_code=0,
            success=True
        )
        expected_dict = {
            "timestamp": 1620000000.0,
            "test_file": "test_example.py",
            "stdout": ".",
            "stderr": "",
            "exit_code": 0,
            "success": True
        }
        self.assertEqual(log_entry.to_dict(), expected_dict)


if __name__ == '__main__':
    unittest.main()
