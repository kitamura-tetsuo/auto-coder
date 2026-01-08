import json
import os
import stat
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.cli_commands_config import LLMBackendConfiguration, export


def test_export_file_permissions():
    """Verify that export command creates files with secure 0o600 permissions."""

    # Mock LLMBackendConfiguration to return some data
    with patch("src.auto_coder.cli_commands_config.LLMBackendConfiguration") as MockConfig:
        mock_instance = MockConfig.return_value
        # mocked config_to_dict result
        with patch("src.auto_coder.cli_commands_config.config_to_dict") as mock_to_dict:
            mock_to_dict.return_value = {"backends": {"test": {"api_key": "secret"}}}
            MockConfig.load_from_file.return_value = mock_instance

            # Create a temp file path
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                output_path = tmp.name
            os.remove(output_path)  # Ensure it doesn't exist yet so export creates it

            try:
                # Call export with output file
                # We need to mock click.echo to avoid clutter
                with patch("click.echo"):
                    export.callback(config_file=None, output=output_path)

                # Check permissions
                st = os.stat(output_path)
                permissions = stat.S_IMODE(st.st_mode)

                # Verify permissions are 0o600 (only user rw)
                # Note: On Windows this check might behave differently, but for Linux/Mac this is correct
                if os.name == "posix":
                    assert permissions & 0o077 == 0, f"File permissions {oct(permissions)} are insecure"
                    assert permissions & 0o600 == 0o600, "File should be readable/writable by owner"

            finally:
                if os.path.exists(output_path):
                    os.remove(output_path)


def test_export_overwrite_permissions():
    """Verify that exporting to an existing file updates its permissions."""
    if os.name != "posix":
        pytest.skip("Permission checks are POSIX specific")

    # Mock LLMBackendConfiguration
    with patch("src.auto_coder.cli_commands_config.LLMBackendConfiguration") as MockConfig:
        mock_instance = MockConfig.return_value
        with patch("src.auto_coder.cli_commands_config.config_to_dict") as mock_to_dict:
            mock_to_dict.return_value = {"backends": {"test": {"api_key": "secret"}}}
            MockConfig.load_from_file.return_value = mock_instance

            # Create a file with insecure permissions
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                output_path = tmp.name

            # Make it world readable/writable
            os.chmod(output_path, 0o666)

            try:
                # Verify initial state
                st = os.stat(output_path)
                assert stat.S_IMODE(st.st_mode) & 0o066 != 0

                # Call export
                with patch("click.echo"):
                    export.callback(config_file=None, output=output_path)

                # Check permissions are corrected
                st = os.stat(output_path)
                permissions = stat.S_IMODE(st.st_mode)
                assert permissions & 0o077 == 0, f"File permissions {oct(permissions)} are insecure after overwrite"

            finally:
                if os.path.exists(output_path):
                    os.remove(output_path)
