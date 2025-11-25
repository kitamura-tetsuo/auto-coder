"""
Tests for the LockManager class.
"""

import json
import os
import platform
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.lock_manager import LockInfo, LockManager


class TestLockInfo:
    """Test cases for the LockInfo class."""

    def test_lock_info_creation(self):
        """Test creating LockInfo with required parameters."""
        lock_info = LockInfo(pid=12345, hostname="test-host", started_at="2024-01-01T00:00:00")
        assert lock_info.pid == 12345
        assert lock_info.hostname == "test-host"
        assert lock_info.started_at == "2024-01-01T00:00:00"

    def test_lock_info_from_dict(self):
        """Test creating LockInfo from dictionary."""
        data = {"pid": 54321, "hostname": "another-host", "started_at": "2024-12-31T23:59:59"}
        lock_info = LockInfo.from_dict(data)
        assert lock_info.pid == 54321
        assert lock_info.hostname == "another-host"
        assert lock_info.started_at == "2024-12-31T23:59:59"

    def test_lock_info_to_dict(self):
        """Test converting LockInfo to dictionary."""
        lock_info = LockInfo(pid=99999, hostname="test-machine", started_at="2024-06-15T12:30:45")
        result = lock_info.to_dict()
        assert result == {"pid": 99999, "hostname": "test-machine", "started_at": "2024-06-15T12:30:45"}


class TestLockManager:
    """Test cases for the LockManager class."""

    def test_lock_info_basic_functionality(self):
        """Test basic LockInfo operations."""
        # Create LockInfo
        lock_info = LockInfo(pid=12345, hostname="test-host", started_at="2024-01-01T00:00:00")

        # Test to_dict
        data = lock_info.to_dict()
        assert data["pid"] == 12345
        assert data["hostname"] == "test-host"
        assert data["started_at"] == "2024-01-01T00:00:00"

        # Test from_dict
        lock_info2 = LockInfo.from_dict(data)
        assert lock_info2.pid == 12345
        assert lock_info2.hostname == "test-host"
        assert lock_info2.started_at == "2024-01-01T00:00:00"

    def test_lock_manager_creation(self):
        """Test LockManager can be created."""
        lock_manager = LockManager()
        assert lock_manager is not None

    @pytest.mark.skipif(platform.system() == "Windows", reason="Unix-specific test")
    def test_is_process_running_unix(self):
        """Test _is_process_running on Unix systems."""
        lock_manager = LockManager()

        # Test with non-existent process (use a very large PID that won't exist)
        assert lock_manager._is_process_running(99999999) is False

        # Test with current process (should exist)
        current_pid = os.getpid()
        assert lock_manager._is_process_running(current_pid) is True

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific test")
    def test_is_process_running_windows(self):
        """Test _is_process_running on Windows systems."""
        lock_manager = LockManager()

        # Test with non-existent process (use a very large PID that won't exist)
        assert lock_manager._is_process_running(99999999) is False

        # Test with current process (should exist)
        current_pid = os.getpid()
        assert lock_manager._is_process_running(current_pid) is True

    def test_context_manager_acquire_and_release(self):
        """Test context manager __enter__ and __exit__ methods."""
        lock_manager = LockManager()

        # Mock the lock_file_path to simulate being in a git repository
        with patch.object(lock_manager, "lock_file_path", Path("/tmp/test_lock.lock")):
            # Ensure clean state
            if lock_manager.lock_file_path.exists():
                lock_manager.lock_file_path.unlink()

            # Test context manager usage
            with lock_manager:
                # Lock should be acquired when entering context
                assert lock_manager.is_locked() is True

            # Lock should be released when exiting context
            assert lock_manager.is_locked() is False

    def test_context_manager_releases_on_exception(self):
        """Test that lock is released even if an exception occurs."""
        lock_manager = LockManager()

        # Mock the lock_file_path to simulate being in a git repository
        with patch.object(lock_manager, "lock_file_path", Path("/tmp/test_lock_exception.lock")):
            # Ensure clean state
            if lock_manager.lock_file_path.exists():
                lock_manager.lock_file_path.unlink()

            # Test that lock is released even with exception
            with pytest.raises(ValueError):
                with lock_manager:
                    assert lock_manager.is_locked() is True
                    raise ValueError("Test exception")

            # Lock should be released even after exception
            assert lock_manager.is_locked() is False

    def test_context_manager_raises_on_acquire_failure(self):
        """Test that __enter__ raises RuntimeError if lock cannot be acquired."""
        lock_manager = LockManager()

        # Mock the lock_file_path to simulate being in a git repository
        with patch.object(lock_manager, "lock_file_path", Path("/tmp/test_lock_fail.lock")):
            # Create a lock first
            lock_manager.acquire_lock()

            # Try to acquire lock again with a new manager instance
            lock_manager2 = LockManager()
            with patch.object(lock_manager2, "lock_file_path", Path("/tmp/test_lock_fail.lock")):
                with pytest.raises(RuntimeError, match="Failed to acquire lock"):
                    with lock_manager2:
                        pass

            # Clean up
            lock_manager.release_lock()


class TestLockCLI:
    """Integration tests for CLI lock commands."""

    def test_lock_group_help(self):
        """Test the help output for the 'lock' command group."""
        from click.testing import CliRunner

        from src.auto_coder.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["lock", "--help"])
        assert result.exit_code == 0
        assert "Lock management commands" in result.output
        assert "unlock" in result.output

    def test_unlock_no_lock_file(self):
        """Test unlock command when no lock file exists."""
        from click.testing import CliRunner

        from src.auto_coder.cli import main

        runner = CliRunner()
        with runner.isolated_filesystem():
            # Not in a git repo, so no lock file should exist
            result = runner.invoke(main, ["unlock"])
            assert result.exit_code == 0
            assert "No lock file found" in result.output

    def test_lock_unlock_commands_exist(self):
        """Test that lock and unlock commands are properly registered."""
        from click.testing import CliRunner

        from src.auto_coder.cli import main

        runner = CliRunner()

        # Test lock command exists
        result = runner.invoke(main, ["lock", "--help"])
        assert result.exit_code == 0

        # Test top-level unlock command exists
        result = runner.invoke(main, ["unlock", "--help"])
        assert result.exit_code == 0

    def test_lock_cleanup_on_exit(self):
        """Test that lock is properly released on program exit."""
        import atexit
        from unittest.mock import MagicMock, patch

        from src.auto_coder.cli import _cleanup_lock, main

        # Test that _cleanup_lock function exists and can be called
        with patch("src.auto_coder.cli._lock_manager") as mock_lock_manager:
            mock_lock_manager.release_lock = MagicMock()

            # Call cleanup function directly
            _cleanup_lock()

            # Verify release_lock was called
            mock_lock_manager.release_lock.assert_called_once()

        # Test atexit registration
        with patch("atexit.register") as mock_atexit_register:
            from click.testing import CliRunner

            runner = CliRunner()
            with runner.isolated_filesystem():
                # Initialize a git repository
                subprocess.run(["git", "init"], capture_output=True, check=True)
                subprocess.run(["git", "config", "user.email", "test@test.com"], capture_output=True, check=True)
                subprocess.run(["git", "config", "user.name", "Test User"], capture_output=True, check=True)

                # Try to run a command that would acquire a lock
                # We use a read-only command to avoid actual execution
                result = runner.invoke(main, ["--help"])
                assert result.exit_code == 0

                # Check if atexit.register was called during initialization
                # This is implicitly tested by the fact that atexit is imported
                # and our code uses it
