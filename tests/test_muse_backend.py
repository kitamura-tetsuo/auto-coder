"""Production-path regression coverage for the Muse local backend."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click import ClickException

from src.auto_coder.cli_helpers import build_backend_manager, check_backend_prerequisites
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Muse Tests")
    (repo / "tracked.txt").write_text("before\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _muse_script(tmp_path: Path, action: str) -> Path:
    script = tmp_path / "muse"
    script.write_text("#!/bin/sh\n" "if [ \"$1\" = --version ]; then echo 'Muse Code test'; exit 0; fi\n" '[ "$1" = exec ] || exit 8\n' f"{action}\n" "echo ACTION_SUMMARY: Muse completed\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _manager(config: LLMBackendConfiguration):
    backend_name = next(iter(config.backends))
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config):
        return build_backend_manager([backend_name], backend_name, {backend_name: "muse-spark-1.3"})


def test_config_alias_reaches_lazy_muse_exec_and_keeps_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _muse_script(tmp_path, "printf 'after\\n' > tracked.txt")
    config = LLMBackendConfiguration(
        backends={
            "muse-payg": BackendConfig(
                name="muse-payg",
                backend_type="muse",
                model="muse-spark-1.3",
                options=["--model", "[model_name]"],
                api_key="secret-not-for-prompt",
            )
        }
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    head = _git(repo, "rev-parse", "HEAD")

    output = _manager(config)._run_llm_cli("edit the file")

    assert output == "ACTION_SUMMARY: Muse completed"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / "tracked.txt").read_text() == "after\n"


def test_muse_commit_is_rejected_and_head_is_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _muse_script(tmp_path, "printf 'bad\\n' > tracked.txt; git add tracked.txt; git commit -m forbidden")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    head = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        _manager(config)._run_llm_cli("implement")

    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == "M tracked.txt"


def test_muse_staging_without_commit_is_rejected_and_unstaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _muse_script(tmp_path, "printf 'staged-by-muse\\n' > tracked.txt; git add tracked.txt")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        _manager(config)._run_llm_cli("implement")

    assert _git(repo, "diff", "--cached") == ""
    assert (repo / "tracked.txt").read_text() == "staged-by-muse\n"


def test_muse_created_branch_is_rejected_and_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    original_branch = _git(repo, "branch", "--show-current")
    script = _muse_script(tmp_path, f"git branch muse-temporary; git switch {original_branch}")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        _manager(config)._run_llm_cli("implement")

    assert _git(repo, "branch", "--list", "muse-temporary") == ""
    assert _git(repo, "branch", "--show-current") == original_branch


def test_muse_temporary_switch_to_existing_branch_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    original_branch = _git(repo, "branch", "--show-current")
    _git(repo, "branch", "other")
    script = _muse_script(tmp_path, f"git switch other; git switch {original_branch}")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with pytest.raises(RuntimeError, match="Git lifecycle command"):
        _manager(config)._run_llm_cli("implement")

    assert _git(repo, "branch", "--show-current") == original_branch
    assert _git(repo, "rev-parse", "other") == _git(repo, "rev-parse", "HEAD")


def test_muse_transient_branch_creation_and_deletion_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    original_branch = _git(repo, "branch", "--show-current")
    refs_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    script = _muse_script(tmp_path, "git branch muse-temporary; git branch -D muse-temporary")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with pytest.raises(RuntimeError, match="Git lifecycle command"):
        _manager(config)._run_llm_cli("implement")

    assert _git(repo, "branch", "--show-current") == original_branch
    assert _git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before


def test_muse_cannot_hide_transient_branch_mutation_by_disabling_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    original_branch = _git(repo, "branch", "--show-current")
    refs_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    script = _muse_script(tmp_path, "env -u GIT_TRACE2_EVENT git branch muse-temporary; env -u GIT_TRACE2_EVENT git branch -D muse-temporary")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with pytest.raises(RuntimeError, match="Git lifecycle command"):
        _manager(config)._run_llm_cli("implement")

    assert _git(repo, "branch", "--show-current") == original_branch
    assert _git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before


def test_muse_executes_without_inotify_and_portable_watch_enforces_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _muse_script(tmp_path, "printf 'after\\n' > tracked.txt")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    libc_without_inotify = object()

    with patch("src.auto_coder.muse_client.ctypes.CDLL", return_value=libc_without_inotify):
        assert _manager(config)._run_llm_cli("implement") == "ACTION_SUMMARY: Muse completed"

    assert (repo / "tracked.txt").read_text() == "after\n"


def test_portable_watch_rejects_trace_bypass_without_inotify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    refs_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    script = _muse_script(tmp_path, "env -u GIT_TRACE2_EVENT git branch temporary; env -u GIT_TRACE2_EVENT git branch -D temporary")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with patch("src.auto_coder.muse_client.ctypes.CDLL", return_value=object()), pytest.raises(RuntimeError, match="Git lifecycle command"):
        _manager(config)._run_llm_cli("implement")

    assert _git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before


def test_noedit_mutation_is_rejected_and_repository_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _muse_script(tmp_path, "printf 'bad\\n' > tracked.txt; printf 'new\\n' > untracked.txt")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", options_for_noedit=["--no-edit"])})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        manager._run_llm_cli("review")

    assert (repo / "tracked.txt").read_text() == "before\n"
    assert not (repo / "untracked.txt").exists()
    assert _git(repo, "status", "--porcelain") == ""


def test_noedit_restores_preexisting_index_and_untracked_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    (repo / "tracked.txt").write_text("staged-before\n")
    _git(repo, "add", "tracked.txt")
    (repo / "untracked.txt").write_text("untracked-before\n")
    before_status = _git(repo, "status", "--porcelain")
    script = _muse_script(tmp_path, "printf 'staged-after\\n' > tracked.txt; printf 'untracked-after\\n' > untracked.txt")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        manager._run_llm_cli("review")

    assert (repo / "tracked.txt").read_text() == "staged-before\n"
    assert (repo / "untracked.txt").read_text() == "untracked-before\n"
    assert _git(repo, "diff", "--cached")
    assert _git(repo, "status", "--porcelain") == before_status


def test_noedit_ignored_file_mutation_is_rejected_and_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("secrets.cache\n")
    (repo / "secrets.cache").write_bytes(b"before\x00secret")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore cache")
    script = _muse_script(tmp_path, "printf 'after' > secrets.cache; printf 'created' > another.cache")
    (repo / ".gitignore").write_text("secrets.cache\nanother.cache\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore another cache")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        manager._run_llm_cli("review")

    assert (repo / "secrets.cache").read_bytes() == b"before\x00secret"
    assert not (repo / "another.cache").exists()


@pytest.mark.parametrize(
    ("action", "timeout", "usage_markers"),
    [("sleep 2", 1, []), ("echo CUSTOM_QUOTA", 30, ["CUSTOM_QUOTA"])],
)
def test_muse_timeout_and_configured_usage_limit_rotate_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, action: str, timeout: int, usage_markers: list[str]) -> None:
    repo = _repository(tmp_path)
    script = _muse_script(tmp_path, action)
    config = LLMBackendConfiguration(
        backends={
            "muse": BackendConfig(name="muse", backend_type="muse", timeout=timeout, usage_markers=usage_markers),
            "fallback": BackendConfig(name="fallback", backend_type="qwen"),
        }
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    fallback_client = type(
        "FallbackClient",
        (),
        {"model_name": "fallback", "_run_llm_cli": lambda self, prompt, is_noedit=False: "fallback-success", "get_last_session_id": lambda self: None},
    )()
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config), patch("src.auto_coder.qwen_client.QwenClient", return_value=fallback_client):
        manager = build_backend_manager(["muse", "fallback"], "muse", {})
        assert manager._run_llm_cli("implement") == "fallback-success"
        assert manager.get_last_backend_and_model() == ("fallback", "fallback")


def test_muse_nonzero_exit_remains_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _muse_script(tmp_path, "echo failed >&2; exit 17")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with pytest.raises(RuntimeError, match="return code 17"):
        _manager(config)._run_llm_cli("implement")


def test_muse_alias_prerequisite_failure_is_actionable() -> None:
    config = LLMBackendConfiguration(backends={"muse-payg": BackendConfig(name="muse-payg", backend_type="muse")})
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.cli_helpers.shutil.which", return_value=None):
        with pytest.raises(ClickException, match="muse CLI is not found in PATH"):
            check_backend_prerequisites(["muse-payg"])
