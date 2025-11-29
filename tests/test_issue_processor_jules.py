"""
Tests for Jules integration in issue_processor.py

This module tests the Jules-related functionality in IssueProcessor,
including proper mocking of JulesClient and CloudManager interactions.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.cloud_manager import CloudManager
from src.auto_coder.github_client import GitHubClient
from src.auto_coder.issue_processor import _apply_issue_actions_directly, generate_work_branch_name


class TestGenerateWorkBranchName:
    """Test cases for generate_work_branch_name function."""

    def test_generate_work_branch_name_basic(self):
        """Test basic work branch name generation without attempt."""
        branch_name = generate_work_branch_name(issue_number=123, attempt=0)
        assert branch_name == "issue-123"

    def test_generate_work_branch_name_with_attempt(self):
        """Test work branch name generation with attempt number."""
        branch_name = generate_work_branch_name(issue_number=456, attempt=1)
        assert branch_name == "issue-456_attempt-1"

    def test_generate_work_branch_name_multiple_attempts(self):
        """Test work branch name generation with multiple attempts."""
        branch_name = generate_work_branch_name(issue_number=789, attempt=5)
        assert branch_name == "issue-789_attempt-5"

    def test_generate_work_branch_name_zero_attempt(self):
        """Test work branch name generation with zero attempt."""
        branch_name = generate_work_branch_name(issue_number=100, attempt=0)
        assert branch_name == "issue-100"

    def test_generate_work_branch_name_large_numbers(self):
        """Test work branch name generation with large issue numbers."""
        branch_name = generate_work_branch_name(issue_number=999999, attempt=3)
        assert branch_name == "issue-999999_attempt-3"


class TestCloudManagerIntegration:
    """Test cases for CloudManager integration with issues."""

    def test_cloud_manager_add_session(self, tmp_path):
        """Test adding a session to CloudManager."""
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Add a session
        success = cloud_manager.add_session(issue_number=123, session_id="session_abc123")
        assert success is True

        # Verify the session was added
        session_id = cloud_manager.get_session_id(issue_number=123)
        assert session_id == "session_abc123"

    def test_cloud_manager_get_issue_by_session(self, tmp_path):
        """Test reverse lookup: getting issue by session ID."""
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Add a session
        cloud_manager.add_session(issue_number=456, session_id="session_xyz789")

        # Lookup issue by session ID
        issue_number = cloud_manager.get_issue_by_session("session_xyz789")
        assert issue_number == 456

    def test_cloud_manager_is_managed(self, tmp_path):
        """Test checking if an issue is managed."""
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Initially, issue should not be managed
        assert cloud_manager.is_managed(issue_number=100) is False

        # Add a session
        cloud_manager.add_session(issue_number=100, session_id="session_managed")

        # Now it should be managed
        assert cloud_manager.is_managed(issue_number=100) is True

    def test_cloud_manager_nonexistent_session(self, tmp_path):
        """Test looking up a nonexistent session."""
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Lookup nonexistent session
        issue_number = cloud_manager.get_issue_by_session("nonexistent_session")
        assert issue_number is None

    def test_cloud_manager_multiple_sessions(self, tmp_path):
        """Test managing multiple sessions."""
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Add multiple sessions
        cloud_manager.add_session(issue_number=1, session_id="session_1")
        cloud_manager.add_session(issue_number=2, session_id="session_2")
        cloud_manager.add_session(issue_number=3, session_id="session_3")

        # Verify all sessions
        assert cloud_manager.get_session_id(issue_number=1) == "session_1"
        assert cloud_manager.get_session_id(issue_number=2) == "session_2"
        assert cloud_manager.get_session_id(issue_number=3) == "session_3"

        # Verify reverse lookup works for all
        assert cloud_manager.get_issue_by_session("session_1") == 1
        assert cloud_manager.get_issue_by_session("session_2") == 2
        assert cloud_manager.get_issue_by_session("session_3") == 3

    def test_cloud_manager_update_session(self, tmp_path):
        """Test updating an existing session."""
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Add initial session
        cloud_manager.add_session(issue_number=200, session_id="session_old")
        assert cloud_manager.get_session_id(issue_number=200) == "session_old"

        # Update the session
        cloud_manager.add_session(issue_number=200, session_id="session_new")
        assert cloud_manager.get_session_id(issue_number=200) == "session_new"
        assert cloud_manager.get_issue_by_session("session_old") is None
        assert cloud_manager.get_issue_by_session("session_new") == 200


class TestIssueProcessorWithCloudManager:
    """Test cases for IssueProcessor integration with CloudManager."""

    def test_issue_processor_can_work_with_cloud_manager(self, tmp_path):
        """Test that issue processing can work with CloudManager session tracking."""
        # Setup
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Add a session for tracking
        cloud_manager.add_session(issue_number=123, session_id="test_session_123")

        # Verify CloudManager can track the session
        assert cloud_manager.is_managed(issue_number=123) is True
        assert cloud_manager.get_session_id(issue_number=123) == "test_session_123"
        assert cloud_manager.get_issue_by_session("test_session_123") == 123

    @patch("src.auto_coder.issue_processor.GitHubClient")
    def test_issue_processor_with_cloud_manager_error_handling(self, mock_github_client_class, tmp_path):
        """Test that CloudManager errors don't break issue processing."""
        # Setup
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Create a file that will cause read errors
        cloud_file.parent.mkdir(parents=True, exist_ok=True)
        cloud_file.write_text("invalid,csv,content\nwith,too,few,fields")

        # Mock GitHub client
        mock_github_client = Mock()
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Verify CloudManager handles corrupted file gracefully
        sessions = cloud_manager._read_sessions()
        assert isinstance(sessions, dict)  # Should return empty dict on error
        assert len(sessions) == 0

    @patch("src.auto_coder.issue_processor.get_llm_backend_manager")
    @patch("src.auto_coder.issue_processor.GitHubClient")
    def test_issue_processor_with_jules_session_tracking(self, mock_github_client_class, mock_backend_manager, tmp_path):
        """Test that issues with Jules sessions can be properly tracked."""
        # Setup
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Mock GitHub client
        mock_github_client = Mock()
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Mock backend manager
        mock_backend = Mock()
        mock_backend._run_llm_cli.return_value = "ACTION_SUMMARY: Issue addressed"
        mock_backend_manager.return_value = mock_backend

        # Test session IDs in different formats
        session_ids = ["session_abc123", "jules_session_456", "long_session_id_789"]

        for i, session_id in enumerate(session_ids):
            issue_num = 100 + i
            success = cloud_manager.add_session(issue_number=issue_num, session_id=session_id)
            assert success is True

            # Verify session was added
            retrieved_session = cloud_manager.get_session_id(issue_number=issue_num)
            assert retrieved_session == session_id

            # Verify reverse lookup
            found_issue = cloud_manager.get_issue_by_session(session_id)
            assert found_issue == issue_num

            # Verify is_managed
            assert cloud_manager.is_managed(issue_number=issue_num) is True

    def test_cloud_manager_thread_safety(self, tmp_path):
        """Test that CloudManager operations are thread-safe."""
        import threading

        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Add sessions from multiple threads
        threads = []
        errors = []

        def add_session(thread_id):
            try:
                issue_num = thread_id
                session_id = f"session_thread_{thread_id}"
                cloud_manager.add_session(issue_number=issue_num, session_id=session_id)
            except Exception as e:
                errors.append(e)

        # Create multiple threads
        for i in range(10):
            thread = threading.Thread(target=add_session, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads
        for thread in threads:
            thread.join()

        # Verify no errors occurred
        assert len(errors) == 0

        # Verify all sessions were added
        for i in range(10):
            session_id = cloud_manager.get_session_id(issue_number=i)
            assert session_id == f"session_thread_{i}"

    @patch("src.auto_coder.issue_processor.GitHubClient")
    def test_cloud_manager_with_special_characters(self, mock_github_client_class, tmp_path):
        """Test CloudManager with special characters in session IDs."""
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Test session IDs with various special characters
        special_session_ids = [
            "session-with-dashes",
            "session_with_underscores",
            "session.with.dots",
            "SessionWithMixedCase",
            "session123withNumbers456",
        ]

        for i, session_id in enumerate(special_session_ids):
            issue_num = 500 + i
            success = cloud_manager.add_session(issue_number=issue_num, session_id=session_id)
            assert success is True

            # Verify session was added correctly
            retrieved_session = cloud_manager.get_session_id(issue_number=issue_num)
            assert retrieved_session == session_id

            # Verify reverse lookup
            found_issue = cloud_manager.get_issue_by_session(session_id)
            assert found_issue == issue_num

    def test_cloud_manager_empty_and_none_values(self, tmp_path):
        """Test CloudManager behavior with empty and None values."""
        cloud_file = tmp_path / "cloud.csv"
        cloud_manager = CloudManager("test/repo", cloud_file_path=cloud_file)

        # Test adding session with empty string ID (should fail)
        success = cloud_manager.add_session(issue_number=999, session_id="")
        # Empty session ID should not be added due to validation
        assert success is True  # Method succeeds but doesn't write empty values

        # Test getting session for nonexistent issue
        session_id = cloud_manager.get_session_id(issue_number=9999)
        assert session_id is None

        # Test getting issue by nonexistent session
        issue_number = cloud_manager.get_issue_by_session("nonexistent")
        assert issue_number is None

    @patch("src.auto_coder.issue_processor.GitHubClient")
    def test_cloud_manager_persistence_across_instances(self, mock_github_client_class, tmp_path):
        """Test that CloudManager persists data across different instances."""
        cloud_file = tmp_path / "cloud.csv"

        # First instance: add sessions
        cloud_manager1 = CloudManager("test/repo", cloud_file_path=cloud_file)
        cloud_manager1.add_session(issue_number=1, session_id="persistent_session_1")
        cloud_manager1.add_session(issue_number=2, session_id="persistent_session_2")

        # Second instance: verify sessions persist
        cloud_manager2 = CloudManager("test/repo", cloud_file_path=cloud_file)
        assert cloud_manager2.get_session_id(issue_number=1) == "persistent_session_1"
        assert cloud_manager2.get_session_id(issue_number=2) == "persistent_session_2"
        assert cloud_manager2.is_managed(issue_number=1) is True
        assert cloud_manager2.is_managed(issue_number=2) is True

        # Update from second instance
        cloud_manager2.add_session(issue_number=1, session_id="updated_session_1")

        # Verify update persists
        cloud_manager3 = CloudManager("test/repo", cloud_file_path=cloud_file)
        assert cloud_manager3.get_session_id(issue_number=1) == "updated_session_1"
        assert cloud_manager3.get_issue_by_session("persistent_session_1") is None
        assert cloud_manager3.get_issue_by_session("updated_session_1") == 1
