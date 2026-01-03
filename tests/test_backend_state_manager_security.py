import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.auto_coder.backend_state_manager import BackendStateManager


class TestBackendStateManagerSecurity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file_path = os.path.join(self.temp_dir.name, "backend_state.json")
        self.manager = BackendStateManager(self.state_file_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("os.open")
    @patch("os.fdopen")
    @patch("os.chmod")
    def test_save_state_uses_secure_file_creation(self, mock_chmod, mock_fdopen, mock_os_open):
        """Verify that save_state uses os.open with O_CREAT | O_WRONLY | O_TRUNC and 0o600 permissions."""

        # Setup mocks
        mock_fd = 123
        mock_os_open.return_value = mock_fd
        mock_file = MagicMock()
        mock_fdopen.return_value = mock_file
        mock_file.__enter__.return_value = mock_file

        # Execute
        self.manager.save_state("test_backend", 123456789.0)

        # Verify os.open called with correct flags and mode
        expected_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        expected_mode = 0o600

        # Find the call to os.open for the temp file
        # We expect it to be called for a .tmp file
        found_secure_open = False
        for call in mock_os_open.call_args_list:
            args, _ = call
            path, flags, mode = args
            if str(path).endswith(".tmp"):
                self.assertEqual(flags, expected_flags, "os.open flags do not match")
                self.assertEqual(mode, expected_mode, "os.open mode does not match")
                found_secure_open = True

        self.assertTrue(found_secure_open, "Did not find os.open call for temp file")

        # Verify os.fdopen called with the file descriptor
        mock_fdopen.assert_called_with(mock_fd, "w")


if __name__ == "__main__":
    unittest.main()
