import os
import tempfile
from pathlib import Path

import pytest

from src.auto_coder.cloud_manager import CloudManager


def test_cloud_manager_file_permissions():
    """Test that CloudManager creates the cloud file with secure permissions (600)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cloud_file = Path(tmpdir) / "cloud.csv"
        manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

        # Add a session, which should trigger file creation/writing
        manager.add_session(123, "secure-session-id")

        assert cloud_file.exists()

        # Check permissions
        st_mode = os.stat(cloud_file).st_mode
        # Mask with 0o777 to get permission bits
        permissions = st_mode & 0o777

        # Should be 0o600 (rw-------)
        assert permissions == 0o600, f"Expected 0o600 but got {oct(permissions)}"


def test_cloud_manager_existing_file_permissions():
    """Test that CloudManager secures an existing insecure file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cloud_file = Path(tmpdir) / "cloud.csv"

        # Create file with insecure permissions (e.g. 644)
        # We use os.open to be explicit, though standard open() usually does 644 or 664 depending on umask
        fd = os.open(str(cloud_file), os.O_WRONLY | os.O_CREAT, 0o644)
        os.close(fd)
        os.chmod(cloud_file, 0o644)

        assert (os.stat(cloud_file).st_mode & 0o777) == 0o644

        manager = CloudManager("owner/repo", cloud_file_path=cloud_file)

        # Add a session, should fix permissions
        manager.add_session(123, "secure-session-id")

        permissions = os.stat(cloud_file).st_mode & 0o777
        assert permissions == 0o600, f"Expected 0o600 but got {oct(permissions)}"
