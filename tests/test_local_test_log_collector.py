import json
import unittest
from pathlib import Path
from unittest.mock import patch
import tempfile
import os

from auto_coder.local_test_log_collector import (
    collect_and_save_logs,
)


class TestLocalTestLogCollector(unittest.TestCase):
    @patch("auto_coder.local_test_log_collector.run_tests")
    @patch("auto_coder.local_test_log_collector.get_sanitized_repo_name")
    def test_collect_and_save_logs(
        self,
        mock_get_sanitized_repo_name,
        mock_run_tests,
    ):
        mock_get_sanitized_repo_name.return_value = "test_owner_test_repo"
        mock_run_tests.return_value = ("stdout", "stderr", 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Create the expected source log directory inside the temp directory
            source_log_dir = tmpdir_path / "logs" / "tests"
            source_log_dir.mkdir(parents=True)
            source_log_file = source_log_dir / "test.log"
            source_log_file.write_text("raw log data")

            # Patch Path.home and os.getcwd to point inside our temp directory
            with patch("pathlib.Path.home", return_value=tmpdir_path), patch("os.getcwd", return_value=str(tmpdir_path)):

                collect_and_save_logs("tests/test_file.py")

                # Define expected paths
                log_dir = tmpdir_path / ".auto-coder" / "test_owner_test_repo" / "test_log"
                raw_log_dir = log_dir / "raw"
                moved_log_path = raw_log_dir / "test.log"

                # Verify JSON log file was created and is correct
                json_files = list(log_dir.glob("*.json"))
                self.assertEqual(len(json_files), 1)

                with open(json_files[0], "r") as f:
                    log_data = json.load(f)

                self.assertEqual(log_data["test_file"], "tests/test_file.py")
                self.assertEqual(log_data["stdout"], "stdout")
                self.assertEqual(log_data["stderr"], "stderr")
                self.assertEqual(log_data["exit_code"], 0)
                self.assertTrue(log_data["success"])

                # Verify raw log file was moved correctly
                self.assertTrue(moved_log_path.exists())
                self.assertEqual(moved_log_path.read_text(), "raw log data")
                self.assertFalse(source_log_file.exists())

                # Verify the path in the JSON log data is correct
                self.assertEqual(len(log_data["raw_log_files"]), 1)
                self.assertEqual(log_data["raw_log_files"][0], str(moved_log_path))
