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


def test_muse_alias_prerequisite_failure_is_actionable() -> None:
    config = LLMBackendConfiguration(backends={"muse-payg": BackendConfig(name="muse-payg", backend_type="muse")})
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.cli_helpers.shutil.which", return_value=None):
        with pytest.raises(ClickException, match="muse CLI is not found in PATH"):
            check_backend_prerequisites(["muse-payg"])
