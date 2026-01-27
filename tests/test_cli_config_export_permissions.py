import os
import stat

import pytest
from click.testing import CliRunner

from src.auto_coder.cli_commands_config import config_group


@pytest.mark.skipif(os.name == "nt", reason="Permissions check is different on Windows")
def test_config_export_permissions(tmp_path):
    """Test that the exported config file has secure permissions (0600)."""
    runner = CliRunner()
    output_file = tmp_path / "exported_config.json"

    # Run the export command
    result = runner.invoke(config_group, ["export", "--output", str(output_file)])

    # Check command success
    assert result.exit_code == 0
    assert output_file.exists()

    # Check file permissions
    st_mode = os.stat(output_file).st_mode
    permissions = st_mode & 0o777

    # We expect 0600 (rw-------)
    assert permissions == 0o600, f"Expected 0600, got {oct(permissions)}"
