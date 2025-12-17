"""
Tests for GHCommandLogger.
"""

import csv
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import pytest

from auto_coder.gh_logger import GH_LOGGING_DISABLED, GHCommandLogger, get_gh_logger, set_gh_logger


@pytest.fixture
def _use_custom_subprocess_mock():
    """Marker fixture to indicate test uses custom subprocess mocking."""
    pass


class TestGHCommandLogger:
    """Test cases for GHCommandLogger class."""

    def test_init_default_log_dir(self):
        """Test initialization with default log directory."""
        logger = GHCommandLogger()
        # The default log directory uses Path.home() which may be stubbed in tests
        assert str(logger.log_dir).endswith(".auto-coder/log") or str(logger.log_dir).endswith(".auto-coder\\log")
        assert logger._hostname == socket.gethostname()

    def test_init_custom_log_dir(self):
        """Test initialization with custom log directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=custom_dir)
            assert logger.log_dir == custom_dir

    def test_ensure_log_dir_creates_directory(self):
        """Test that _ensure_log_dir creates the log directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir) / "test_logs"
            logger = GHCommandLogger(log_dir=test_dir)
            logger._ensure_log_dir()
            assert test_dir.exists()
            assert test_dir.is_dir()

    def test_get_log_file_path(self):
        """Test that _get_log_file_path returns correct path."""
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)
            expected_date = datetime.now().strftime("%Y-%m-%d")
            expected_path = test_dir / f"gh_commands_{expected_date}.csv"
            actual_path = logger._get_log_file_path()
            assert actual_path == expected_path

    def test_format_csv_row(self):
        """Test that _format_csv_row returns correct dictionary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = GHCommandLogger(log_dir=Path(tmpdir))
            row = logger._format_csv_row(
                caller_file="/test/file.py",
                caller_line=42,
                command="gh",
                args=["api", "graphql"],
                repo="owner/repo",
            )

            assert "timestamp" in row
            assert row["caller_file"] == "/test/file.py"
            assert row["caller_line"] == "42"
            assert row["command"] == "gh"
            assert row["args"] == "api graphql"
            assert row["repo"] == "owner/repo"
            assert row["hostname"] == socket.gethostname()

    def test_log_command_creates_csv_file(self):
        """Test that log_command creates and writes to CSV file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Log a command
            logger.log_command(
                command_list=["gh", "auth", "status"],
                caller_file="/test/auth_utils.py",
                caller_line=36,
                repo="owner/repo",
            )

            # Check file was created
            log_file = logger._get_log_file_path()
            assert log_file.exists()

            # Read and verify CSV content
            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

                assert len(rows) == 1
                assert rows[0]["command"] == "gh"
                assert "auth" in rows[0]["args"]
                assert "status" in rows[0]["args"]
                assert rows[0]["repo"] == "owner/repo"
                assert rows[0]["caller_file"] == "/test/auth_utils.py"
                assert rows[0]["caller_line"] == "36"

    def test_log_command_multiple_commands(self):
        """Test logging multiple commands creates multiple rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Log multiple commands
            logger.log_command(
                command_list=["gh", "auth", "status"],
                caller_file="/test/auth_utils.py",
                caller_line=36,
            )
            logger.log_command(
                command_list=["gh", "api", "graphql"],
                caller_file="/test/github_client.py",
                caller_line=100,
                repo="owner/repo",
            )

            # Check both commands were logged
            log_file = logger._get_log_file_path()
            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

                assert len(rows) == 2
                assert rows[0]["command"] == "gh"
                assert rows[1]["command"] == "gh"

    def test_log_command_respects_disabled_flag(self):
        """Test that logging is disabled when GH_LOGGING_DISABLED is set."""
        with patch("auto_coder.gh_logger.GH_LOGGING_DISABLED", True):
            from auto_coder.gh_logger import GHCommandLogger

            with tempfile.TemporaryDirectory() as tmpdir:
                test_dir = Path(tmpdir)
                logger = GHCommandLogger(log_dir=test_dir)

                # Log a command (should be ignored due to disabled flag)
                logger.log_command(
                    command_list=["gh", "auth", "status"],
                    caller_file="/test/auth_utils.py",
                    caller_line=36,
                )

                # Check file was NOT created
                log_file = logger._get_log_file_path()
                assert not log_file.exists()

    def test_execute_with_logging(self, monkeypatch, _use_custom_subprocess_mock):
        """Test execute_with_logging method."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Mock subprocess.run to avoid actually executing commands
            mock_result = Mock(returncode=0, stdout="test output", stderr="")

            def mock_run_func(*args, **kwargs):
                return mock_result

            # Patch subprocess.run using patch
            with patch("auto_coder.gh_logger.subprocess.run", side_effect=mock_run_func):
                # Execute a command with logging
                result = logger.execute_with_logging(
                    ["gh", "auth", "status"],
                    capture_output=True,
                    text=True,
                )

            # Verify command was logged
            log_file = logger._get_log_file_path()
            assert log_file.exists()
            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["command"] == "gh"
                assert "auth" in rows[0]["args"]
                assert "status" in rows[0]["args"]

    def test_logged_subprocess_context_manager(self, monkeypatch, _use_custom_subprocess_mock):
        """Test logged_subprocess context manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Mock subprocess.run
            mock_result = Mock(returncode=0, stdout="test", stderr="")

            def mock_run_func(*args, **kwargs):
                return mock_result

            # Patch subprocess.run using patch instead of monkeypatch.setattr
            with patch("auto_coder.gh_logger.subprocess.run", side_effect=mock_run_func):
                # Use context manager
                with logger.logged_subprocess(
                    ["gh", "auth", "token"],
                    capture_output=True,
                    text=True,
                ) as result:
                    assert result.returncode == 0

            # Verify command was logged
            log_file = logger._get_log_file_path()
            assert log_file.exists()
            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["command"] == "gh"
                assert "auth" in rows[0]["args"]
                assert "token" in rows[0]["args"]

    def test_log_command_with_empty_repo(self, _use_custom_subprocess_mock):
        """Test logging command without repo parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Direct test of log_command without subprocess
            logger.log_command(
                command_list=["gh", "--version"],
                caller_file="/test/file.py",
                caller_line=1,
                repo=None,
            )

            log_file = logger._get_log_file_path()
            assert log_file.exists()
            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["repo"] == ""

    def test_global_logger_instance(self):
        """Test global logger instance functions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_logger = GHCommandLogger(log_dir=Path(tmpdir))

            # Set global logger
            set_gh_logger(custom_logger)

            # Get global logger
            retrieved_logger = get_gh_logger()
            assert retrieved_logger is custom_logger

    def test_log_command_with_special_characters_in_args(self, _use_custom_subprocess_mock):
        """Test logging command with special characters in args."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Direct test of log_command without subprocess
            logger.log_command(
                command_list=["gh", "api", "graphql", "-f", "query=query { ... }"],
                caller_file="/test/file.py",
                caller_line=1,
                repo="owner/repo",
            )

            log_file = logger._get_log_file_path()
            assert log_file.exists()
            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                # Special characters should be preserved
                assert "query=query { ... }" in rows[0]["args"]

    def test_compress_json_string_with_valid_json(self):
        """Test _compress_json_string with valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = GHCommandLogger(log_dir=Path(tmpdir))

            # Test with valid JSON object
            result = logger._compress_json_string('{"key": "value", "nested": {"a": 1}}')
            assert result == '{"key":"value","nested":{"a":1}}'

            # Test with valid JSON array
            result = logger._compress_json_string("[1, 2, 3]")
            assert result == "[1,2,3]"

            # Test with multi-line JSON with indentation
            multi_line_json = """{
                "key": "value",
                "nested": {
                    "a": 1
                }
            }"""
            result = logger._compress_json_string(multi_line_json)
            assert result == '{"key":"value","nested":{"a":1}}'

    def test_compress_json_string_with_non_json(self):
        """Test _compress_json_string with non-JSON strings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = GHCommandLogger(log_dir=Path(tmpdir))

            # Test with non-JSON strings - should remain unchanged
            assert logger._compress_json_string("auth") == "auth"
            assert logger._compress_json_string("status") == "status"
            assert logger._compress_json_string("graphql") == "graphql"
            assert logger._compress_json_string("not-a-json") == "not-a-json"

    def test_compress_json_string_with_malformed_json(self):
        """Test _compress_json_string with malformed JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = GHCommandLogger(log_dir=Path(tmpdir))

            # Test with incomplete JSON
            result = logger._compress_json_string('{"incomplete": ')
            assert result == '{"incomplete": '

            # Test with invalid JSON syntax (missing quotes)
            result = logger._compress_json_string("{key: value}")
            assert result == "{key: value}"

    def test_log_command_with_json_args(self, _use_custom_subprocess_mock):
        """Test that JSON in command args is compressed when logged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Log a command with JSON argument
            logger.log_command(
                command_list=["gh", "api", "graphql", "-f", 'query={"user": "test"}'],
                caller_file="/test/file.py",
                caller_line=1,
            )

            # Read the CSV file and verify the args column contains compressed JSON
            log_file = logger._get_log_file_path()
            assert log_file.exists()
            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                # Verify JSON is compressed (no newlines or extra spaces)
                assert 'query={"user":"test"}' in rows[0]["args"]

    def test_log_command_with_graphql_query(self, _use_custom_subprocess_mock):
        """Test real-world GraphQL query compression."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Log a command with multi-line JSON query (representing a GraphQL query as JSON)
            graphql_query = """{
    "query": "query { user(id: 123) { name email } }",
    "variables": {
        "id": 123
    }
}"""
            logger.log_command(
                command_list=["gh", "api", "graphql", "-f", graphql_query],
                caller_file="/test/graphql_client.py",
                caller_line=42,
                repo="owner/repo",
            )

            # Read the CSV file and verify JSON query is compressed
            log_file = logger._get_log_file_path()
            assert log_file.exists()
            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                # Verify JSON is compressed to single-line (no newlines)
                assert rows[0]["args"].count("\n") == 0
                # Verify the compressed JSON is valid when parsed
                # The JSON should be the last argument (after -f flag)
                import json

                # Extract the JSON part from args - it's everything after "-f"
                args_parts = rows[0]["args"].split(" ", 3)  # Split into max 4 parts
                assert len(args_parts) >= 4
                json_part = args_parts[3]  # The JSON is the 4th part
                # Parse the compressed JSON to verify it's valid
                parsed = json.loads(json_part)
                assert "query" in parsed
                assert "variables" in parsed
                assert parsed["variables"]["id"] == 123

    def test_format_csv_row_with_mixed_args(self):
        """Test that a mix of JSON and non-JSON args are handled correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = GHCommandLogger(log_dir=Path(tmpdir))

            # Format CSV row with mixed args
            row = logger._format_csv_row(
                caller_file="/test/mixed_args.py",
                caller_line=50,
                command="gh",
                args=["api", "graphql", "-f", 'query={"test": 1}'],
                repo="owner/repo",
            )

            # Verify non-JSON args remain unchanged
            assert "api" in row["args"]
            assert "graphql" in row["args"]
            assert "-f" in row["args"]

            # Verify JSON args are compressed
            assert 'query={"test":1}' in row["args"]

            # Verify all args are joined with spaces
            assert 'api graphql -f query={"test":1}' == row["args"]

    def test_log_command_redacts_sensitive_info(self, _use_custom_subprocess_mock):
        """Test that sensitive information (tokens) is redacted from logs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            logger = GHCommandLogger(log_dir=test_dir)

            # Test data
            gh_token = "ghp_SECRET1234567890abcdefghijklmnop"
            gemini_key = "AIzaSyD_SECRET_KEY_1234567890abcdefghijk"

            # Log command with sensitive args
            logger.log_command(
                command_list=["gh", "secret", "set", "MY_SECRET", "-b", gh_token],
                caller_file="/test/file.py",
                caller_line=1,
            )

            # Log another command with Gemini key
            logger.log_command(
                command_list=["gemini", "config", "set", "api_key", gemini_key],
                caller_file="/test/file.py",
                caller_line=2,
            )

            # Check log file
            log_file = logger._get_log_file_path()
            assert log_file.exists()

            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

                assert len(rows) == 2

                # Check GitHub token redaction
                assert "[REDACTED]" in rows[0]["args"]
                assert gh_token not in rows[0]["args"]

                # Check Gemini key redaction
                assert "[REDACTED]" in rows[1]["args"]
                assert gemini_key not in rows[1]["args"]
