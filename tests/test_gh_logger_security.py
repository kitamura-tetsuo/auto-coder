import os
import tempfile
from pathlib import Path

import pytest

from auto_coder.gh_logger import GHCommandLogger


class TestGHLoggerSecurity:

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_gh_logger_creates_secure_file(self, temp_dir):
        logger = GHCommandLogger(log_dir=temp_dir)

        # Log a command
        logger.log_command(["gh", "test"], "test_file.py", 1)

        # Find the log file
        log_files = list(temp_dir.glob("gh_commands_*.csv"))
        assert len(log_files) == 1
        log_file = log_files[0]

        # Check permissions
        st = os.stat(log_file)
        mode = st.st_mode & 0o777

        # Should be 0o600 (rw-------)
        # Check that group and other have no permissions
        assert mode & 0o077 == 0, f"File permissions {oct(mode)} are too permissive"
        # Check that user has read/write
        assert mode & 0o600 == 0o600, f"File permissions {oct(mode)} do not allow read/write"

    def test_gh_logger_fixes_insecure_file(self, temp_dir):
        logger = GHCommandLogger(log_dir=temp_dir)
        log_file_path = logger._get_log_file_path()

        # Create an insecure file first
        with open(log_file_path, "w") as f:
            f.write("header\n")

        # Make it world readable/writable (0o666)
        # Note: umask might restrict this, but we try our best
        os.chmod(log_file_path, 0o666)

        st = os.stat(log_file_path)
        # If the environment forces secure umask, this test setup might 'fail' to create insecure file
        # But if it is insecure, we want to check if it gets fixed.
        if (st.st_mode & 0o077) != 0:
            # Only run the fix verification if we managed to create an insecure file

            # Log a command, which should fix permissions
            logger.log_command(["gh", "test_fix"], "test_file.py", 2)

            st = os.stat(log_file_path)
            mode = st.st_mode & 0o777
            assert mode & 0o077 == 0, f"File permissions {oct(mode)} were not fixed"
