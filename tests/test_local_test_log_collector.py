import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_coder.local_test_log_collector import (
    collect_and_save_logs,
    get_log_dir,
)


class TestLocalTestLogCollector(unittest.TestCase):
    @patch("auto_coder.local_test_log_collector.run_tests")
    @patch("auto_coder.local_test_log_collector.get_sanitized_repo_name")
    @patch("pathlib.Path.rename")
    @patch("pathlib.Path.exists")
    @patch("pathlib.Path.glob")
    def test_collect_and_save_logs(
        self,
        mock_glob,
        mock_exists,
        mock_rename,
        mock_get_repo_name,
        mock_run_tests,
    ):
        mock_get_repo_name.return_value = "test_owner/test_repo"
        mock_run_tests.return_value = ("stdout", "stderr", 0)
        mock_exists.return_value = True
        mock_glob.return_value = [Path("logs/tests/test.log")]

        collect_and_save_logs("tests/test_file.py")

        log_dir = get_log_dir()
        self.assertTrue(log_dir.exists())

        log_files = list(log_dir.glob("*.json"))
        self.assertEqual(len(log_files), 1)

        with open(log_files[0], "r") as f:
            log_data = json.load(f)

        self.assertEqual(log_data["test_file"], "tests/test_file.py")
        self.assertEqual(log_data["stdout"], "stdout")
        self.assertEqual(log_data["stderr"], "stderr")
        self.assertEqual(log_data["exit_code"], 0)
        self.assertTrue(log_data["success"])
        self.assertIn("raw_log_files", log_data)
        self.assertEqual(len(log_data["raw_log_files"]), 1)
        self.assertTrue(log_data["raw_log_files"][0].endswith("test_owner_test_repo/test_log/raw/test.log"))

        for log_file in log_files:
            log_file.unlink()


if __name__ == "__main__":
    unittest.main()
