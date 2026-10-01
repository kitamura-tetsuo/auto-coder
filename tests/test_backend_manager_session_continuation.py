"""Regression tests for explicit and implementation session continuation."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.exceptions import (
    AutoCoderRetryableBackendError,
    AutoCoderTimeoutError,
    AutoCoderUsageLimitError,
    LocalWriterSettlementError,
    SessionWorkspaceCompatibilityError,
)
from src.auto_coder.local_session_continuation import LocalContinuationError
from src.auto_coder.opencode_client import OpenCodeClient


class SessionClient:
    supports_retained_local_continuation = True

    def __init__(self, fresh_session_id: str | None = None) -> None:
        self.model_name = "test-model"
        self.session_id: str | None = None
        self.next_fresh_session_id = fresh_session_id
        self.fresh_prompts: list[str] = []
        self.continued: list[tuple[str, str, bool]] = []
        self.continue_error: Exception | None = None

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        self.fresh_prompts.append(prompt)
        self.session_id = self.next_fresh_session_id
        return "fresh response"

    def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
        self.continued.append((session_id, prompt, is_noedit))
        self.session_id = session_id
        if self.continue_error:
            raise self.continue_error
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


def test_consecutive_local_implementation_prompts_are_fresh(tmp_path):
    client = SessionClient(fresh_session_id="implementation-session")
    manager = _manager(tmp_path, {"claude": client})

    assert manager._run_llm_cli("first") == "fresh response"
    client.next_fresh_session_id = "second-session"
    assert manager._run_llm_cli("second") == "fresh response"

    assert client.fresh_prompts == ["first", "second"]
    assert client.continued == []
    assert manager._last_session_id == "second-session"


def test_incompatible_explicit_session_fails_without_fresh_submission(tmp_path):
    client = SessionClient(fresh_session_id="stale-session")
    client.continue_error = SessionWorkspaceCompatibilityError("session belongs to another workspace")
    manager = _manager(tmp_path, {"claude": client}, automatic_session_resume=False)
    manager._last_session_id = "stale-session"

    with pytest.raises(SessionWorkspaceCompatibilityError, match="another workspace"):
        manager.continue_session("stale-session", "full review", is_noedit=True)

    assert client.continued == [("stale-session", "full review", True)]
    assert client.fresh_prompts == []
    assert manager._last_session_id == "stale-session"


def test_stale_local_session_selects_fresh_execution(tmp_path):
    client = SessionClient(fresh_session_id="new-session")
    client.continue_error = RuntimeError("session not found")
    manager = _manager(tmp_path, {"claude": client})
    manager._last_session_id = "stale-session"

    assert manager._run_llm_cli("implementation context") == "fresh response"

    assert client.fresh_prompts == ["implementation context"]
    assert client.continued == []
    assert client.get_last_session_id() == "new-session"
    assert manager._last_session_id == "new-session"


def test_restored_local_session_is_history_only_for_first_fresh_call(tmp_path):
    state_dir = tmp_path / ".auto-coder"
    state_dir.mkdir()
    (state_dir / "backend_session_state.json").write_text(
        json.dumps(
            {
                "last_backend": "renamed-local",
                "last_session_id": "restored-session",
                "last_used_timestamp": 1.0,
            }
        )
    )
    client = SessionClient(fresh_session_id="current-session")
    client.config_backend = type("Config", (), {"backend_type": "muse"})()
    manager = _manager(tmp_path, {"renamed-local": client})

    assert manager.run_prompt("current task") == "fresh response"

    assert client.fresh_prompts == ["current task"]
    assert client.continued == []
    assert manager.get_last_session_id() == "current-session"


def test_fresh_local_call_does_not_report_previous_identity_when_provider_returns_none(tmp_path):
    client = SessionClient()
    client.config_backend = type("Config", (), {"backend_type": "muse"})()
    manager = _manager(tmp_path, {"renamed-local": client})
    manager._last_backend = "renamed-local"
    manager._last_session_id = "previous-session"
    client.session_id = "previous-session"

    assert manager.run_prompt("current task") == "fresh response"

    assert manager.get_last_session_id() is None
    assert client.continued == []


def test_local_history_does_not_select_retryable_continuation(tmp_path):
    client = SessionClient(fresh_session_id="persisted-session")
    client.continue_error = AutoCoderRetryableBackendError("Codex transport reconnects exhausted")
    manager = _manager(tmp_path, {"codex": client})
    manager._last_backend = "codex"
    manager._last_session_id = "persisted-session"

    assert manager._run_llm_cli("implementation context") == "fresh response"

    assert client.continued == []
    assert client.fresh_prompts == ["implementation context"]


def test_local_history_does_not_select_writer_uncertain_continuation(tmp_path):
    client = SessionClient(fresh_session_id="new-session")
    client.continue_error = LocalWriterSettlementError("writer settlement is uncertain")
    manager = _manager(tmp_path, {"opencode": client})
    manager._last_backend = "opencode"
    manager._last_session_id = "persisted-session"

    assert manager._run_llm_cli("implementation context") == "fresh response"

    assert client.continued == []
    assert client.fresh_prompts == ["implementation context"]
    assert manager._last_session_id == "new-session"


@pytest.mark.parametrize("backend_type", ["claude-routine", "codex-cloud", "jules"])
def test_cloud_alias_still_automatically_resumes_matching_history(tmp_path, backend_type):
    client = SessionClient(fresh_session_id="cloud-session")
    client.config_backend = type("Config", (), {"backend_type": backend_type})()
    manager = _manager(tmp_path, {"renamed-cloud": client})
    manager._last_backend = "renamed-cloud"
    manager._last_session_id = "cloud-session"

    assert manager._run_llm_cli("continue cloud task") == "continued response"

    assert client.fresh_prompts == []
    assert client.continued == [("cloud-session", "continue cloud task", False)]


def test_ordinary_local_call_resets_previous_continuity_indicator(tmp_path):
    client = SessionClient(fresh_session_id="old-session")
    client.config_backend = type("Config", (), {"backend_type": "opencode"})()
    manager = _manager(tmp_path, {"renamed-local": client})
    manager._last_continue_session_resumed = True

    assert manager.run_prompt("fresh task") == "fresh response"

    assert manager._last_continue_session_resumed is False
    assert client.continued == []


def test_uncertain_writer_during_explicit_resume_does_not_launch_replacement(tmp_path):
    opencode = SessionClient(fresh_session_id="opencode-session")
    opencode.continue_error = LocalWriterSettlementError("writer settlement is uncertain")
    fallback = SessionClient(fresh_session_id="fallback-session")
    manager = _manager(tmp_path, {"opencode": opencode, "codex": fallback}, automatic_session_resume=False)

    with pytest.raises(LocalWriterSettlementError, match="settlement is uncertain"):
        manager.continue_session("opencode-session", "review", is_noedit=True)

    assert opencode.continued == [("opencode-session", "review", True)]
    assert opencode.fresh_prompts == []
    assert fallback.fresh_prompts == []
    assert manager.get_current_backend_identity()[0] == "opencode"
    assert manager._last_continue_session_resumed is False


def test_explicit_resume_usage_limit_does_not_rotate_or_start_fresh(tmp_path):
    claude = SessionClient(fresh_session_id="claude-session")
    claude.continue_error = AutoCoderUsageLimitError("usage limit")
    codex = SessionClient(fresh_session_id="codex-session")
    manager = _manager(tmp_path, {"claude": claude, "codex": codex}, automatic_session_resume=False)

    with pytest.raises(AutoCoderUsageLimitError, match="usage limit"):
        manager.continue_session("claude-session", "review", is_noedit=True)

    assert claude.fresh_prompts == []
    assert codex.fresh_prompts == []


def test_unsupported_explicit_continuation_fails_before_provider_or_fresh_submission(tmp_path):
    client = SessionClient(fresh_session_id="session")
    client.supports_retained_local_continuation = False
    manager = _manager(tmp_path, {"codex": client}, automatic_session_resume=False)

    with pytest.raises(RuntimeError, match="cannot prove retained-workspace"):
        manager.continue_session("session", "continue")

    assert client.continued == []
    assert client.fresh_prompts == []
    assert manager._last_continue_session_resumed is False


@pytest.mark.parametrize(
    "continuation_error",
    [
        AutoCoderTimeoutError("Muse continuation timed out"),
        AutoCoderUsageLimitError("Muse quota exhausted"),
    ],
    ids=["timeout", "usage-limit"],
)
def test_muse_continuation_execution_error_does_not_use_fresh_fallback(tmp_path, continuation_error):
    muse = SessionClient(fresh_session_id="exact-session")
    muse.continue_error = continuation_error
    fallback = SessionClient(fresh_session_id="fallback-session")
    manager = _manager(tmp_path, {"muse": muse, "codex": fallback}, automatic_session_resume=False)

    with pytest.raises(type(continuation_error), match=str(continuation_error)):
        manager.continue_session("exact-session", "review follow-up", is_noedit=True)

    assert muse.continued == [("exact-session", "review follow-up", True)]
    assert muse.fresh_prompts == []
    assert fallback.fresh_prompts == []
    assert manager.get_current_backend_identity()[0] == "muse"
    assert manager._last_continue_session_resumed is False


def test_explicit_resume_resets_continuity_before_unexpected_error(tmp_path):
    client = SessionClient(fresh_session_id="session")
    client.continue_error = OSError("unexpected transport failure")
    manager = _manager(tmp_path, {"codex": client}, automatic_session_resume=False)
    manager._last_continue_session_resumed = True

    with pytest.raises(OSError, match="unexpected transport failure"):
        manager.continue_session("session", "review", is_noedit=True)

    assert manager._last_continue_session_resumed is False
    assert client.fresh_prompts == []


def test_empty_explicit_session_is_rejected_without_claiming_continuity(tmp_path):
    client = SessionClient()
    manager = _manager(tmp_path, {"muse": client}, automatic_session_resume=False)
    manager._last_continue_session_resumed = True

    with pytest.raises(ValueError, match="Session ID must be nonempty"):
        manager.continue_session("", "review", is_noedit=True)

    assert manager._last_continue_session_resumed is False
    assert client.continued == []
    assert client.fresh_prompts == []


def test_unreadable_opencode_preflight_does_not_start_fresh_or_clear_session(tmp_path, monkeypatch):
    class PreflightClient(SessionClient):
        command = ["opencode"]

        def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
            self.continued.append((session_id, prompt, is_noedit))
            self.session_id = session_id
            OpenCodeClient._verify_resumable_session_in_current_workspace(self, session_id=session_id, cwd=tmp_path, env={})
            raise AssertionError("continued task must not be submitted")

    client = PreflightClient(fresh_session_id="prior-session")
    manager = _manager(tmp_path, {"opencode": client}, automatic_session_resume=False)
    manager._last_session_id = "prior-session"
    import subprocess

    original_run = subprocess.run

    def unreadable_session_list(command, *args, **kwargs):
        if command[:2] == ["opencode", "session"]:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(
        "src.auto_coder.opencode_client.subprocess.run",
        unreadable_session_list,
    )

    with pytest.raises(SessionWorkspaceCompatibilityError, match="could not be verified"):
        manager.continue_session("prior-session", "continue", is_noedit=True)

    assert client.continued == [("prior-session", "continue", True)]
    assert client.fresh_prompts == []
    assert manager._last_session_id == "prior-session"
    assert manager._last_continue_session_resumed is False
