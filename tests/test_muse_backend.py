"""Production-path regression coverage for the Muse local backend."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click import ClickException

from src.auto_coder.cli_helpers import build_backend_manager, check_backend_prerequisites
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.prompt_loader import render_prompt


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
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


@pytest.mark.parametrize("size", [256 * 1024, 2 * 1024 * 1024])
def test_muse_transports_full_rendered_prompt_through_private_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, size: int) -> None:
    repo = _repository(tmp_path)
    report = tmp_path / f"report-{size}.json"
    script = tmp_path / f"muse-{size}"
    script.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.0.2")
    raise SystemExit(0)
arguments = sys.argv[1:]
assert arguments[0] == "exec"
assert arguments.count("--prompt-file") == 1
prompt_path = Path(arguments[arguments.index("--prompt-file") + 1])
time.sleep(0.05)
payload = prompt_path.read_bytes()
file_stat = prompt_path.stat()
report = {
    "argv": arguments,
    "path": str(prompt_path),
    "regular": stat.S_ISREG(file_stat.st_mode),
    "mode": stat.S_IMODE(file_stat.st_mode),
    "size": len(payload),
    "digest": hashlib.sha256(payload).hexdigest(),
    "body_in_environment": any(payload.decode("utf-8") in value for value in os.environ.values()),
    "api_key": os.environ.get("MUSE_API_KEY"),
}
Path(os.environ["MUSE_TEST_REPORT"]).write_text(json.dumps(report))
print("ACTION_SUMMARY: Muse completed")
"""
    )
    script.chmod(0o700)
    config = LLMBackendConfiguration(
        backends={
            "muse": BackendConfig(
                name="muse",
                backend_type="muse",
                model="muse-spark-1.3",
                options=["--model", "[model_name]"],
                api_key="private-test-key",
            )
        }
    )
    task = (("Unicode ☃ @ ' \" ; $(false)\r\n" * ((size // 30) + 1))[:size]).rstrip("\n")
    expected = render_prompt("muse.execution", task_prompt=task, mode="edit").encode("utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    monkeypatch.setenv("MUSE_TEST_REPORT", str(report))

    assert _manager(config)._run_llm_cli(task) == "ACTION_SUMMARY: Muse completed"

    observed = json.loads(report.read_text())
    assert observed["argv"][:4] == ["exec", "--model", "muse-spark-1.3", "--prompt-file"]
    assert task not in observed["argv"]
    assert observed["regular"] is True
    assert observed["mode"] == 0o600
    assert observed["size"] == len(expected)
    assert observed["digest"] == hashlib.sha256(expected).hexdigest()
    assert observed["body_in_environment"] is False
    assert observed["api_key"] == "private-test-key"
    assert not Path(observed["path"]).exists()
    assert not Path(observed["path"]).is_relative_to(repo)
    assert _git(repo, "status", "--porcelain") == ""


def test_muse_uses_safe_external_directory_when_tmpdir_is_in_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    unsafe_temp = repo / "temporary"
    unsafe_temp.mkdir()
    report = tmp_path / "prompt-path"
    script = _muse_script(tmp_path, 'eval "prompt_path=\\${$(($# - 1))}"')
    # Use a small dedicated script because POSIX sh has no portable negative
    # positional-parameter syntax.
    script.write_text("#!/bin/sh\n" "if [ \"$1\" = --version ]; then echo 'Muse Code 1.0.2'; exit 0; fi\n" 'while [ "$1" != --prompt-file ]; do shift; done\n' 'printf \'%s\' "$2" > "$MUSE_TEST_REPORT"\n' 'cat "$2" >/dev/null\n' "echo ACTION_SUMMARY: Muse completed\n")
    script.chmod(0o700)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    monkeypatch.setenv("MUSE_TEST_REPORT", str(report))
    monkeypatch.setenv("TMPDIR", str(unsafe_temp))
    monkeypatch.setattr("src.auto_coder.muse_client.tempfile.tempdir", None)

    assert _manager(config)._run_llm_cli("implement") == "ACTION_SUMMARY: Muse completed"
    assert not Path(report.read_text()).is_relative_to(repo)
    assert _git(repo, "status", "--porcelain") == ""


def test_muse_rejects_configured_prompt_source_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    launched = tmp_path / "launched"
    script = _muse_script(tmp_path, f"touch {shlex.quote(str(launched))}")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", options=["--prompt-file", "configured.txt"])})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with pytest.raises(RuntimeError, match="must not configure a prompt source"):
        _manager(config)._run_llm_cli("implement")

    assert not launched.exists()


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


def test_muse_cannot_hide_transient_linked_worktree_by_disabling_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    linked_worktree = tmp_path / "muse-linked-worktree"
    worktrees_before = _git(repo, "worktree", "list", "--porcelain")
    action = f"env -u GIT_TRACE2_EVENT git worktree add --detach " f"{shlex.quote(str(linked_worktree))} HEAD; " f"env -u GIT_TRACE2_EVENT git worktree remove -f " f"{shlex.quote(str(linked_worktree))}"
    script = _muse_script(tmp_path, action)
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    # Exercise the portable parent-owned metadata journal as well as the
    # production manager path; the child has disabled cooperative tracing.
    with patch("src.auto_coder.muse_client.ctypes.CDLL", return_value=object()), pytest.raises(RuntimeError, match="Git lifecycle command"):
        _manager(config)._run_llm_cli("implement")

    assert _git(repo, "worktree", "list", "--porcelain") == worktrees_before
    assert not linked_worktree.exists()


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


@pytest.mark.parametrize(("filename", "ignored"), [("script.sh", False), ("ignored-script.sh", True)])
def test_noedit_mode_only_change_is_rejected_and_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, filename: str, ignored: bool) -> None:
    repo = _repository(tmp_path)
    if ignored:
        (repo / ".gitignore").write_text(f"{filename}\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore script")
    target = repo / filename
    target.write_bytes(b"unchanged bytes\n")
    target.chmod(0o644)
    script = _muse_script(tmp_path, f"chmod +x {filename}")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        manager._run_llm_cli("review")

    assert target.read_bytes() == b"unchanged bytes\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_noedit_tracked_non_executable_mode_change_is_rejected_and_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    target = repo / "tracked.txt"
    target.chmod(0o644)
    script = _muse_script(tmp_path, "chmod 600 tracked.txt")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        manager._run_llm_cli("review")

    assert target.read_text() == "before\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert _git(repo, "status", "--porcelain") == ""


def test_noedit_directory_mode_change_is_rejected_and_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    source_dir = repo / "src"
    source_dir.mkdir()
    (source_dir / "tracked.txt").write_text("tracked\n")
    source_dir.chmod(0o755)
    _git(repo, "add", "src/tracked.txt")
    _git(repo, "commit", "-m", "add nested source")
    script = _muse_script(tmp_path, "chmod 700 src")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        manager._run_llm_cli("review")

    assert stat.S_IMODE(source_dir.stat().st_mode) == 0o755
    assert (source_dir / "tracked.txt").read_text() == "tracked\n"
    assert _git(repo, "status", "--porcelain") == ""


def test_noedit_deleted_empty_directory_is_rejected_and_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    scratch = repo / "scratch"
    scratch.mkdir(mode=0o755)
    script = _muse_script(tmp_path, "rmdir scratch")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))
    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="Git-state invariant"):
        manager._run_llm_cli("review")

    assert scratch.is_dir()
    assert list(scratch.iterdir()) == []
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o755


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


def test_muse_noedit_sanitizes_options_and_enforces_readonly_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    captured_file = tmp_path / "captured_args.json"
    script = tmp_path / "muse"
    script.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.2.1")
    raise SystemExit(0)
arguments = sys.argv[1:]
Path(r"{captured_file}").write_text(json.dumps(arguments))
print("ACTION_SUMMARY: Muse review completed")
"""
    )
    script.chmod(0o700)
    config = LLMBackendConfiguration(
        backends={
            "muse-spark": BackendConfig(
                name="muse-spark",
                backend_type="muse",
                model="muse-spark-1.3",
                options=["exec", "--reasoning-effort", "low", "--yolo"],
                options_for_noedit=["exec", "--reasoning-effort", "low", "--yolo", "--disable-sandbox"],
            )
        }
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config):
        manager = build_backend_manager(["muse-spark"], "muse-spark", {"muse-spark": "muse-spark-1.3"}, use_noedit_options=True)
        manager._is_noedit = True
        output = manager._run_llm_cli("review the code")

    assert output == "ACTION_SUMMARY: Muse review completed"
    captured = json.loads(captured_file.read_text())
    assert captured[0] == "exec"
    assert captured.count("exec") == 1
    assert "--yolo" not in captured
    assert "--disable-sandbox" not in captured
    assert "--disable-write" in captured
    assert "--disable-shell" in captured
    assert "--disable-approval" in captured
    assert "--prompt-file" in captured


def test_muse_edit_mode_strips_duplicate_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    captured_file = tmp_path / "captured_edit_args.json"
    script = tmp_path / "muse"
    script.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.2.1")
    raise SystemExit(0)
arguments = sys.argv[1:]
Path(r"{captured_file}").write_text(json.dumps(arguments))
print("ACTION_SUMMARY: Muse edit completed")
"""
    )
    script.chmod(0o700)
    config = LLMBackendConfiguration(
        backends={
            "muse-spark": BackendConfig(
                name="muse-spark",
                backend_type="muse",
                model="muse-spark-1.3",
                options=["exec", "--reasoning-effort", "low", "--yolo"],
            )
        }
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config):
        manager = build_backend_manager(["muse-spark"], "muse-spark", {"muse-spark": "muse-spark-1.3"})
        manager._is_noedit = False
        output = manager._run_llm_cli("edit the code")

    assert output == "ACTION_SUMMARY: Muse edit completed"
    captured = json.loads(captured_file.read_text())
    assert captured[0] == "exec"
    assert captured.count("exec") == 1
    assert "--yolo" in captured


def test_muse_respects_command_execution_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    main_repo = _repository(tmp_path / "main")
    worktree_dir = tmp_path / "worktree"
    _git(main_repo, "worktree", "add", str(worktree_dir), "HEAD")
    captured_file = tmp_path / "captured_cwd_args.json"
    script = tmp_path / "muse"
    script.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.2.1")
    raise SystemExit(0)
arguments = sys.argv[1:]
Path(r"{captured_file}").write_text(json.dumps({{"cwd": os.getcwd(), "args": arguments}}))
print("ACTION_SUMMARY: Muse worktree completed")
"""
    )
    script.chmod(0o700)
    config = LLMBackendConfiguration(
        backends={
            "muse-spark": BackendConfig(
                name="muse-spark",
                backend_type="muse",
                model="muse-spark-1.3",
            )
        }
    )
    monkeypatch.chdir(main_repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    from src.auto_coder.utils import bind_command_execution_cwd, reset_command_execution_cwd

    token = bind_command_execution_cwd(str(worktree_dir))
    try:
        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config):
            manager = build_backend_manager(["muse-spark"], "muse-spark", {"muse-spark": "muse-spark-1.3"}, use_noedit_options=True)
            manager._is_noedit = True
            output = manager._run_llm_cli("review the code")
    finally:
        reset_command_execution_cwd(token)

    assert output == "ACTION_SUMMARY: Muse worktree completed"
    data = json.loads(captured_file.read_text())
    assert Path(data["cwd"]).resolve() == worktree_dir.resolve()


def test_muse_stderr_warnings_do_not_pollute_successful_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = tmp_path / "muse"
    script.write_text(
        """#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.2.1")
    raise SystemExit(0)
sys.stderr.write("muse: workspace root: /tmp/isolated\\n")
sys.stderr.write("muse: warning: rules file at /workspace/CLAUDE.md is ignored\\n")
print('{"verdict": "APPROVE"}')
"""
    )
    script.chmod(0o700)
    config = LLMBackendConfiguration(
        backends={
            "muse-spark": BackendConfig(
                name="muse-spark",
                backend_type="muse",
                model="muse-spark-1.3",
            )
        }
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config):
        manager = build_backend_manager(["muse-spark"], "muse-spark", {"muse-spark": "muse-spark-1.3"}, use_noedit_options=True)
        manager._is_noedit = True
        output = manager._run_llm_cli("review the code")

    assert output == '{"verdict": "APPROVE"}'


def test_extract_muse_jsonl_result_completed() -> None:
    from src.auto_coder.adversarial_validator import _extract_muse_jsonl_result

    raw = """muse: workspace root: /workspace
muse: warning: something ignored
{"schema_version":1,"record_type":"event","payload_type":"run.lifecycle.started","payload":{}}
{"schema_version":1,"record_type":"event","payload_type":"run.terminal.completed","payload":{"terminal":"completed","text":"{\\"verdict\\": \\"READY\\"}"}}
"""
    detected, text, error = _extract_muse_jsonl_result(raw)
    assert detected is True
    assert text == '{"verdict": "READY"}'
    assert error is None


def test_extract_muse_jsonl_result_failed() -> None:
    from src.auto_coder.adversarial_validator import _extract_muse_jsonl_result

    raw = """{"schema_version":1,"record_type":"event","payload_type":"run.terminal.failed","payload":{"terminal":"failed","reason":"model error"}}
"""
    detected, text, error = _extract_muse_jsonl_result(raw)
    assert detected is True
    assert text is None
    assert error == "Muse emitted failure event: model error"


def test_extract_muse_jsonl_result_task_failed() -> None:
    from src.auto_coder.adversarial_validator import _extract_muse_jsonl_result

    raw = """{"schema_version":1,"record_type":"event","payload_type":"task.lifecycle.failed","payload":{"event":{"kind":"failed","reason":"task crashed"}}}
"""
    detected, text, error = _extract_muse_jsonl_result(raw)
    assert detected is True
    assert text is None
    assert error == "Muse emitted failure event: task crashed"


def test_extract_muse_jsonl_result_delta_fallback() -> None:
    from src.auto_coder.adversarial_validator import _extract_muse_jsonl_result

    raw = """{"schema_version":1,"record_type":"event","payload_type":"run.output.delta","payload":{"text":"part1"}}
{"schema_version":1,"record_type":"event","payload_type":"run.output.delta","payload":{"text":"part2"}}
"""
    detected, text, error = _extract_muse_jsonl_result(raw)
    assert detected is True
    assert text == "part1part2"
    assert error is None


def test_extract_muse_jsonl_result_not_muse_jsonl() -> None:
    from src.auto_coder.adversarial_validator import _extract_muse_jsonl_result

    raw = "Hello, plain text output\nNot a muse JSONL stream"
    detected, text, error = _extract_muse_jsonl_result(raw)
    assert detected is False
    assert text is None
    assert error is None


def test_muse_run_llm_cli_extracts_jsonl_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = tmp_path / "muse"
    event_payload = {
        "schema_version": 1,
        "record_type": "event",
        "payload_type": "run.terminal.completed",
        "payload": {
            "terminal": "completed",
            "text": '{"verdict": "READY", "findings": []}',
        },
    }
    script.write_text(
        f"""#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.2.1")
    raise SystemExit(0)
print("muse: workspace root: /workspace")
print({repr(json.dumps(event_payload))})
"""
    )
    script.chmod(0o700)
    config = LLMBackendConfiguration(
        backends={
            "muse-spark": BackendConfig(
                name="muse-spark",
                backend_type="muse",
                model="muse-spark-1.3",
                options_for_noedit=["--json", "--reasoning-effort", "low"],
            )
        }
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(script))

    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config):
        manager = build_backend_manager(["muse-spark"], "muse-spark", {"muse-spark": "muse-spark-1.3"}, use_noedit_options=True)
        manager._is_noedit = True
        output = manager._run_llm_cli("review the code")

    assert output == '{"verdict": "READY", "findings": []}'
