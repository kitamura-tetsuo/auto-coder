import json
import time
from pathlib import Path

from src.auto_coder.backend_session_manager import BackendSessionManager, BackendSessionState, create_session_state


def test_save_and_load_session_state(tmp_path):
    state_file = tmp_path / "session_state.json"
    manager = BackendSessionManager(state_file_path=str(state_file))

    saved_state = BackendSessionState(last_backend="claude", last_session_id="abc123", last_used_timestamp=123.45)
    assert manager.save_state(saved_state) is True

    loaded_state = manager.load_state()
    assert loaded_state.last_backend == "claude"
    assert loaded_state.last_session_id == "abc123"
    assert loaded_state.last_used_timestamp == 123.45


def test_load_missing_session_state_returns_default(tmp_path):
    state_file = tmp_path / "missing.json"
    manager = BackendSessionManager(state_file_path=str(state_file))

    loaded_state = manager.load_state()
    assert loaded_state == BackendSessionState()


def test_load_invalid_session_state_returns_default(tmp_path):
    state_file = tmp_path / "invalid.json"
    state_file.write_text("{invalid", encoding="utf-8")

    manager = BackendSessionManager(state_file_path=str(state_file))
    loaded_state = manager.load_state()
    assert loaded_state == BackendSessionState()


def test_create_session_state_sets_timestamp():
    before = time.time()
    state = create_session_state("claude", "sess-1")
    after = time.time()

    assert state.last_backend == "claude"
    assert state.last_session_id == "sess-1"
    assert before <= state.last_used_timestamp <= after
