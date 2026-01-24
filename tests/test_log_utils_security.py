import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.git_commit import save_commit_failure_history
from auto_coder.log_utils import LogEntry


def test_log_entry_save_secure_permissions(tmp_path):
    """Test that LogEntry.save creates files with secure permissions (0o600)."""
    log_dir = tmp_path / "logs"
    filename = "test_log.json"

    entry = LogEntry(ts="2023-01-01T00:00:00", source="test", repo="test_repo", command="echo 'hello'", exit_code=0)

    entry.save(log_dir, filename)

    filepath = log_dir / filename
    assert filepath.exists()

    # Check permissions
    st = os.stat(filepath)
    permissions = st.st_mode & 0o777

    # We expect 0o600 (rw-------)
    assert permissions == 0o600, f"Expected permissions 0o600, got {oct(permissions)}"


def test_log_entry_save_fixes_insecure_permissions(tmp_path):
    """Test that LogEntry.save fixes permissions if file already exists with insecure permissions."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    filename = "existing_log.json"
    filepath = log_dir / filename

    # Create file with insecure permissions (0o666)
    filepath.touch(mode=0o666)
    os.chmod(filepath, 0o666)

    # Verify it is insecure
    st = os.stat(filepath)
    assert (st.st_mode & 0o777) != 0o600

    entry = LogEntry(ts="2023-01-01T00:00:00", source="test", repo="test_repo")

    entry.save(log_dir, filename)

    # Check permissions were fixed
    st = os.stat(filepath)
    permissions = st.st_mode & 0o777
    assert permissions == 0o600, f"Expected permissions 0o600, got {oct(permissions)}"


def test_save_commit_failure_history_secure_permissions():
    """Verify that save_commit_failure_history uses secure file permissions (0o600)."""
    with patch("os.open") as mock_os_open, patch("os.fdopen") as mock_os_fdopen, patch("pathlib.Path.mkdir") as mock_mkdir, patch("sys.exit") as mock_exit, patch("os.chmod") as mock_chmod:

        # Setup
        error_message = "Test error"
        context = {"test": "context"}
        repo_name = "test/repo"

        # Execute
        save_commit_failure_history(error_message, context, repo_name)

        # Verify
        assert mock_os_open.called
        args, kwargs = mock_os_open.call_args
        assert args[1] == os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        assert args[2] == 0o600

        # Verify chmod is called (we can't easily check args[0] as it's constructed inside, but we can check the mode)
        assert mock_chmod.called
        args_chmod, _ = mock_chmod.call_args
        assert args_chmod[1] == 0o600
