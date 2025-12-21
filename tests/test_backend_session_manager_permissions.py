import os
import pytest
from src.auto_coder.backend_session_manager import BackendSessionManager, BackendSessionState

@pytest.mark.skipif(os.name == 'nt', reason="Permissions check is different on Windows")
def test_backend_session_manager_permissions(tmp_path):
    """Test that the session state file is created with secure permissions (0600)."""
    state_path = tmp_path / "backend_session_state.json"

    manager = BackendSessionManager(state_file_path=str(state_path))
    state = BackendSessionState(last_backend="codex")
    manager.save_state(state)

    assert state_path.exists()

    # Check file permissions
    st_mode = os.stat(state_path).st_mode
    permissions = st_mode & 0o777

    # We expect 0600 (rw-------)
    assert permissions == 0o600, f"Expected 0600, got {oct(permissions)}"
