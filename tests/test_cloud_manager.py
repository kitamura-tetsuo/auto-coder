"""
Unit tests for CloudManager.
"""

import csv
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.cloud_manager import CloudManager


class TestCloudManager:
    """Test suite for CloudManager."""

    def test_init_default_path(self):
        """Test that CloudManager creates the correct default path."""
        repo_name = "owner/repo"
        manager = CloudManager(repo_name)
        assert manager.repo_name == repo_name
        expected_path = Path.home() / ".auto-coder" / repo_name / "cloud.csv"
        assert manager.cloud_file_path == expected_path

    def test_init_custom_path(self):
        """Test that CloudManager uses custom path when provided."""
        repo_name = "owner/repo"
        custom_path = Path("/custom/path/cloud.csv")
        manager = CloudManager(repo_name, cloud_file_path=custom_path)
        assert manager.repo_name == repo_name
        assert manager.cloud_file_path == custom_path

    def test_add_session(self):
        """Test adding a session for an issue."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add a session
            result = manager.add_session(123, "session-abc")
            assert result is True
            assert cloud_file.exists()

            # Verify the CSV content
            with open(cloud_file, "r") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["issue_number"] == "123"
                assert rows[0]["session_id"] == "session-abc"

    def test_add_session_creates_directory(self):
        """Test that add_session creates the directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a path with non-existent nested directory
            cloud_file = Path(tmpdir) / "nonexistent" / "dir" / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add session should create the directory
            result = manager.add_session(123, "session-abc")
            assert result is True
            assert cloud_file.exists()

    def test_add_multiple_sessions(self):
        """Test adding multiple sessions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add multiple sessions
            manager.add_session(123, "session-abc")
            manager.add_session(456, "session-def")
            manager.add_session(789, "session-xyz")

            # Verify all sessions are stored
            assert manager.get_session_id(123) == "session-abc"
            assert manager.get_session_id(456) == "session-def"
            assert manager.get_session_id(789) == "session-xyz"

    def test_add_duplicate_issue_updates_session(self):
        """Test that adding a session for the same issue updates it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add initial session
            manager.add_session(123, "session-abc")
            assert manager.get_session_id(123) == "session-abc"

            # Update session for same issue
            manager.add_session(123, "session-xyz")
            assert manager.get_session_id(123) == "session-xyz"

            # Verify only one entry exists
            with open(cloud_file, "r") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["issue_number"] == "123"
                assert rows[0]["session_id"] == "session-xyz"

    def test_get_session_id_exists(self):
        """Test getting a session ID that exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add a session
            manager.add_session(123, "session-abc")

            # Get the session
            session_id = manager.get_session_id(123)
            assert session_id == "session-abc"

    def test_get_session_id_not_exists(self):
        """Test getting a session ID that doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Get session for non-existent issue
            session_id = manager.get_session_id(999)
            assert session_id is None

    def test_get_session_id_empty_file(self):
        """Test getting a session ID from an empty CSV file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            cloud_file.touch()  # Create empty file
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            session_id = manager.get_session_id(123)
            assert session_id is None

    def test_is_managed_true(self):
        """Test is_managed returns True for an issue with a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add a session
            manager.add_session(123, "session-abc")

            # Check if managed
            assert manager.is_managed(123) is True

    def test_is_managed_false(self):
        """Test is_managed returns False for an issue without a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Don't add any session for issue 999

            # Check if managed
            assert manager.is_managed(999) is False

    def test_csv_header(self):
        """Test that CSV file has correct header."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add a session
            manager.add_session(123, "session-abc")

            # Verify header
            with open(cloud_file, "r") as f:
                reader = csv.reader(f)
                header = next(reader)
                assert header == ["issue_number", "session_id"]

    def test_csv_sorted_by_issue_number(self):
        """Test that CSV entries are sorted by issue number."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add sessions in random order
            manager.add_session(789, "session-xyz")
            manager.add_session(123, "session-abc")
            manager.add_session(456, "session-def")

            # Verify sorted order
            with open(cloud_file, "r") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 3
                assert rows[0]["issue_number"] == "123"
                assert rows[0]["session_id"] == "session-abc"
                assert rows[1]["issue_number"] == "456"
                assert rows[1]["session_id"] == "session-def"
                assert rows[2]["issue_number"] == "789"
                assert rows[2]["session_id"] == "session-xyz"

    def test_thread_safety(self):
        """Test that operations are thread-safe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Simulate concurrent access
            def add_session(issue_num):
                manager.add_session(issue_num, f"session-{issue_num}")

            # Create multiple threads
            import threading

            threads = []
            for i in range(10):
                thread = threading.Thread(target=add_session, args=(i,))
                threads.append(thread)
                thread.start()

            # Wait for all threads to complete
            for thread in threads:
                thread.join()

            # Verify all sessions were added
            for i in range(10):
                assert manager.get_session_id(i) == f"session-{i}"

    def test_invalid_csv_handling(self):
        """Test handling of invalid CSV file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            # Create invalid CSV
            with open(cloud_file, "w") as f:
                f.write("invalid,csv,content\nwithout,proper,headers\n")

            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Should return None for non-existent issue
            session_id = manager.get_session_id(123)
            assert session_id is None

            # Should return False for non-existent issue
            assert manager.is_managed(123) is False

    def test_malformed_csv_row_handling(self):
        """Test handling of malformed CSV rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            # Create CSV with some valid and some invalid rows
            with open(cloud_file, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["issue_number", "session_id"])
                writer.writerow(["123", "session-abc"])
                writer.writerow(["invalid", "row"])  # Missing fields
                writer.writerow(["456"])  # Only issue_number

            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Should handle malformed rows gracefully
            assert manager.get_session_id(123) == "session-abc"
            assert manager.get_session_id(456) is None

    def test_special_characters_in_session_id(self):
        """Test handling of special characters in session ID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add session with special characters
            special_session = "session-abc-123_456@domain.com"
            manager.add_session(123, special_session)

            # Verify session is stored correctly
            session_id = manager.get_session_id(123)
            assert session_id == special_session

    def test_large_session_id(self):
        """Test handling of large session IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Add session with large session ID
            large_session = "session-" + "x" * 10000
            manager.add_session(123, large_session)

            # Verify session is stored correctly
            session_id = manager.get_session_id(123)
            assert session_id == large_session

    def test_file_permission_error_handling(self):
        """Test handling of file permission errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cloud_file = Path(tmpdir) / "cloud.csv"
            # Create a directory instead of a file to cause permission error
            cloud_file.mkdir()

            manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

            # Operations should fail gracefully
            assert manager.add_session(123, "session-abc") is False
            assert manager.get_session_id(123) is None
            assert manager.is_managed(123) is False
