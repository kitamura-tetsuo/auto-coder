import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from auto_coder.git_commit import save_commit_failure_history
from auto_coder.log_utils import LogEntry


def test_log_entry_save_secure_permissions():
    """Verify that LogEntry.save uses secure file permissions (0o600)."""
    with patch("os.open") as mock_os_open, patch("os.fdopen") as mock_os_fdopen, patch("pathlib.Path.mkdir") as mock_mkdir, patch("os.chmod") as mock_chmod:

        # Setup
        entry = LogEntry(ts="2023-01-01", source="test", repo="test_repo")
        log_dir = MagicMock()
        filepath_mock = MagicMock()
        log_dir.__truediv__.return_value = filepath_mock

        # Execute
        entry.save(log_dir, "test.json")

        # Verify
        mock_os_open.assert_called_with(filepath_mock, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # Verify chmod is called to handle existing files
        mock_chmod.assert_called_with(filepath_mock, 0o600)


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
