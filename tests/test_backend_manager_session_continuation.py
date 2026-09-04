"""Regression tests for explicit and implementation session continuation."""

from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder.backend_manager import BackendManager
from auto_coder.exceptions import AutoCoderRetryableBackendError, AutoCoderUsageLimitError


class SessionClient:
    def __init__(self, fresh_session_id: str | None = None) -> None:
        self.model_name = "test-model"
        self.session_id = fresh_session_id
        self.fresh_prompts: list[str] = []
        self.continued: list[tuple[str, str, bool]] = []
        self.continue_error: Exception | None = None

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        self.fresh_prompts.append(prompt)
        return "fresh response"

    def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
        self.continued.append((session_id, prompt, is_noedit))
        if self.continue_error:
            raise self.continue_error
        self.session_id = session_id
        return "continued response"

    def get_last_session_id(self) -> str | None:
        return self.session_id

    def clear_last_session_id(self) -> None:
        self.session_id = None


def _manager(tmp_path: Path, clients: dict[str, SessionClient], automatic_session_resume: bool = True) -> BackendManager:
    first_name = next(iter(clients))
    with patch("pathlib.Path.home", return_value=tmp_path):
        return BackendManager(
            default_backend=first_name,
            default_client=clients[first_name],
            factories={name: (lambda client=client: client) for name, client in clients.items()},
            order=list(clients),
            automatic_session_resume=automatic_session_resume,
        )


def test_consecutive_implementation_prompts_continue_the_first_session(tmp_path):
    client = SessionClient(fresh_session_id="implementation-session")
    manager = _manager(tmp_path, {"claude": client})

    assert manager._run_llm_cli("first") == "fresh response"
    assert manager._run_llm_cli("second") == "continued response"

    assert client.fresh_prompts == ["first"]
    assert client.continued == [("implementation-session", "second", False)]


def test_stale_explicit_session_fallback_without_new_id_does_not_retain_old_id(tmp_path):
    client = SessionClient(fresh_session_id="stale-session")
    client.continue_error = RuntimeError("session not found")
    manager = _manager(tmp_path, {"claude": client}, automatic_session_resume=False)
    manager._last_session_id = "stale-session"

    assert manager.continue_session("stale-session", "full review", is_noedit=True) == "fresh response"

    assert client.continued == [("stale-session", "full review", True)]
    assert client.fresh_prompts == ["full review"]
    assert manager._last_session_id is None


def test_stale_implementation_session_fallback_clears_cached_client_id(tmp_path):
    client = SessionClient(fresh_session_id="stale-session")
    client.continue_error = RuntimeError("session not found")
    manager = _manager(tmp_path, {"claude": client})
    manager._last_session_id = "stale-session"

    assert manager._run_llm_cli("implementation context") == "fresh response"

    assert client.fresh_prompts == ["implementation context"]
    assert client.get_last_session_id() is None
    assert manager._last_session_id is None


def test_retryable_outage_during_automatic_resume_does_not_start_fresh_execution(tmp_path):
    client = SessionClient(fresh_session_id="persisted-session")
    client.continue_error = AutoCoderRetryableBackendError("Codex transport reconnects exhausted")
    manager = _manager(tmp_path, {"codex": client})
    manager._last_backend = "codex"
    manager._last_session_id = "persisted-session"

    with pytest.raises(AutoCoderRetryableBackendError, match="reconnects exhausted"):
        manager._run_llm_cli("implementation context")

    assert client.continued == [("persisted-session", "implementation context", False)]
    assert client.fresh_prompts == []


def test_resume_usage_limit_rotates_backend_without_same_client_fresh_retry(tmp_path):
    claude = SessionClient(fresh_session_id="claude-session")
    claude.continue_error = AutoCoderUsageLimitError("usage limit")
    codex = SessionClient(fresh_session_id="codex-session")
    manager = _manager(tmp_path, {"claude": claude, "codex": codex}, automatic_session_resume=False)

    assert manager.continue_session("claude-session", "review", is_noedit=True) == "fresh response"

    assert claude.fresh_prompts == []
    assert codex.fresh_prompts == ["review"]
    assert manager.get_last_backend_and_model() == ("codex", "test-model")
