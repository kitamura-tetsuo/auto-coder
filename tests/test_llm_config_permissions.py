import os
import stat

import pytest

from src.auto_coder.llm_backend_config import LLMBackendConfiguration


@pytest.mark.skipif(os.name == "nt", reason="Permissions check is different on Windows")
def test_llm_config_permissions(tmp_path):
    """Test that the llm_config.toml file is created with secure permissions (0600)."""
    config_path = tmp_path / "llm_config.toml"

    config = LLMBackendConfiguration()
    config.save_to_file(str(config_path))

    assert config_path.exists()

    # Check file permissions
    st_mode = os.stat(config_path).st_mode
    permissions = st_mode & 0o777

    # We expect 0600 (rw-------)
    assert permissions == 0o600, f"Expected 0600, got {oct(permissions)}"
