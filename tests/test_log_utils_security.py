import os
from pathlib import Path

import pytest

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
