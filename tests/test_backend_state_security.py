import json
import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from auto_coder.backend_state_manager import BackendStateManager


class TestBackendStateSecurity:
    @patch("auto_coder.backend_state_manager.os.open")
    @patch("auto_coder.backend_state_manager.os.fdopen")
    @patch("auto_coder.backend_state_manager.os.chmod")
    @patch("auto_coder.backend_state_manager.Path")
    def test_save_state_uses_secure_permissions(self, mock_path, mock_chmod, mock_fdopen, mock_os_open):
        # Setup mocks
        mock_file_handle = MagicMock()
        mock_fdopen.return_value.__enter__.return_value = mock_file_handle

        # Mock Path behavior
        mock_path_obj = MagicMock()
        mock_path.return_value = mock_path_obj
        mock_path_obj.expanduser.return_value = mock_path_obj
        mock_path_obj.resolve.return_value = mock_path_obj
        # Setup temp path
        mock_temp_path = MagicMock()
        mock_path_obj.with_suffix.return_value = mock_temp_path
        str(mock_temp_path)  # Should return a string representation

        # Mock os.open return value (file descriptor)
        mock_os_open.return_value = 123

        # Initialize manager
        manager = BackendStateManager()

        # Call save_state
        manager.save_state("test_backend", 12345.67)

        # Verification

        # 1. Verify os.open was called with secure permissions (0o600)
        # secure_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        # mock_os_open.assert_called_with(str(mock_temp_path), secure_flags, 0o600)

        # Since we are testing BEFORE the fix, we expect this test to FAIL if we assert os.open is called.
        # But to prove it works later, I will comment out the assertion or expect failure?
        # Actually, for the "reproduction" step, running it and seeing failure is good.

        # Let's assert what we WANT to happen.
        mock_os_open.assert_called()
        args, _ = mock_os_open.call_args
        # Check permissions arg (3rd argument)
        assert args[2] == 0o600, f"Expected permissions 0o600, got {oct(args[2]) if len(args) > 2 else 'None'}"

        # 2. Verify os.fdopen was called with the file descriptor
        mock_fdopen.assert_called_with(123, "w", encoding="utf-8")

        # 3. Verify chmod is still called (defense in depth)
        mock_chmod.assert_called()
