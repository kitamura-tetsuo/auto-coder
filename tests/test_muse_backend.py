"""Production-path regression coverage for the local Muse Code backend."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click import ClickException

from src.auto_coder.cli_helpers import (
    build_backend_manager,
    check_backend_prerequisites,
)
from src.auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.llm_backend_config import get_llm_config
from src.auto_coder.muse_client import MuseClient
from src.auto_coder.utils import CommandResult


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None) -> Path:
    git(tmp_path, "init", "-b", "work")
    git(tmp_path, "config", "user.email", "tests@example.com")
    git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-m", "initial")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def configure_muse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, noedit: str = 'options_for_noedit = ["--audit"]', markers: str = 'usage_markers = ["capacity exhausted"]') -> None:
    config = tmp_path / "llm.toml"
    config.write_text(
        f"""[backend]\ndefault = "muse-payg"\norder = ["muse-payg"]\n\n[backends.muse-payg]\nbackend_type = "muse"\nmodel = "muse-spark-1.3-contributor"\napi_key = "secret-meta-key"\ntimeout = 77\noptions = ["--yolo"]\n{noedit}\n{markers}\n""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTO_CODER_CONFIG_PATH", str(config))


def test_config_alias_reaches_lazy_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_muse(tmp_path, monkeypatch)
    config = get_llm_config()
    assert config.get_backend_config("muse-payg").backend_type == "muse"

    with patch("src.auto_coder.muse_client.MuseClient") as muse_class:
        muse_class.return_value = MagicMock()
        manager = build_backend_manager(
            selected_backends=config.backend_order,
            primary_backend=config.default_backend,
            models={"muse-payg": "muse-spark-1.3-contributor"},
        )

    assert manager is not None
    muse_class.assert_called_once_with(backend_name="muse-payg", use_noedit_options=False)


def test_prerequisite_alias_uses_actionable_muse_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_muse(tmp_path, monkeypatch)
    with patch("src.auto_coder.cli_helpers.shutil.which", return_value=None):
        with pytest.raises(ClickException, match="muse CLI is not found in PATH"):
            check_backend_prerequisites(["muse-payg"])


def test_edit_run_uses_exec_options_model_environment_and_keeps_changes(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_muse(tmp_path, monkeypatch)
    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> CommandResult:
        observed.update(command=command, env=kwargs["env"], timeout=kwargs["idle_timeout"])
        prompt_path = Path(command[command.index("--prompt-file") + 1])
        prompt = prompt_path.read_text(encoding="utf-8")
        assert "Do not change branches or HEAD" in prompt
        assert "secret-meta-key" not in prompt
        (repo / "tracked.txt").write_text("implemented\n", encoding="utf-8")
        return CommandResult(True, "ACTION_SUMMARY: implemented", "", 0)

    with patch("src.auto_coder.muse_client.CommandExecutor.run_command", side_effect=run):
        result = MuseClient("muse-payg")._run_llm_cli("Implement it")

    command = observed["command"]
    assert isinstance(command, list)
    assert command[:3] == ["muse", "exec", "--yolo"]
    assert command[command.index("--model") + 1] == "muse-spark-1.3-contributor"
    assert "secret-meta-key" not in command
    assert observed["env"]["META_API_KEY"] == "secret-meta-key"  # type: ignore[index]
    assert observed["timeout"] == 77
    assert result == "ACTION_SUMMARY: implemented"
    assert git(repo, "branch", "--show-current") == "work"
    assert git(repo, "rev-list", "--count", "HEAD") == "1"
    assert git(repo, "diff", "--", "tracked.txt")


def test_head_mutation_is_restored_and_rejected(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_muse(tmp_path, monkeypatch)
    original_head = git(repo, "rev-parse", "HEAD")
    original_status = git(repo, "status", "--porcelain")

    def run(command: list[str], **kwargs: object) -> CommandResult:
        (repo / "tracked.txt").write_text("forbidden commit\n", encoding="utf-8")
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-m", "Muse must not commit")
        return CommandResult(True, "looks successful", "", 0)

    with patch("src.auto_coder.muse_client.CommandExecutor.run_command", side_effect=run):
        with pytest.raises(RuntimeError, match="Git-state invariant"):
            MuseClient("muse-payg")._run_llm_cli("Implement it")

    assert git(repo, "branch", "--show-current") == "work"
    assert git(repo, "rev-parse", "HEAD") == original_head
    assert git(repo, "status", "--porcelain") == original_status


def test_staging_without_commit_is_restored_and_rejected(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_muse(tmp_path, monkeypatch)
    original_status = git(repo, "status", "--porcelain")

    def run(command: list[str], **kwargs: object) -> CommandResult:
        (repo / "tracked.txt").write_text("staged by Muse\n", encoding="utf-8")
        git(repo, "add", "tracked.txt")
        return CommandResult(True, "looks successful", "", 0)

    with patch("src.auto_coder.muse_client.CommandExecutor.run_command", side_effect=run):
        with pytest.raises(RuntimeError, match="changing index"):
            MuseClient("muse-payg")._run_llm_cli("Implement it")

    assert git(repo, "status", "--porcelain") == original_status


def test_noedit_honors_options_and_restores_dirty_baseline(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_muse(tmp_path, monkeypatch)
    (repo / "tracked.txt").write_text("pre-existing staged\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    (repo / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    before_status = subprocess.run(["git", "status", "--porcelain=v1", "-z"], cwd=repo, check=True, capture_output=True).stdout
    before_cached = git(repo, "diff", "--cached")

    def run(command: list[str], **kwargs: object) -> CommandResult:
        assert "--audit" in command
        assert all(flag in command for flag in ("--trust-workspace", "--disable-approval", "--disable-write"))
        (repo / "tracked.txt").write_text("unauthorized\n", encoding="utf-8")
        (repo / "untracked.txt").write_text("also unauthorized\n", encoding="utf-8")
        (repo / "new.txt").write_text("remove me\n", encoding="utf-8")
        return CommandResult(True, "text response", "", 0)

    with patch("src.auto_coder.muse_client.CommandExecutor.run_command", side_effect=run):
        with pytest.raises(RuntimeError, match="working tree"):
            MuseClient("muse-payg")._run_llm_cli("Inspect", is_noedit=True)

    after_status = subprocess.run(["git", "status", "--porcelain=v1", "-z"], cwd=repo, check=True, capture_output=True).stdout
    assert after_status == before_status
    assert git(repo, "diff", "--cached") == before_cached
    assert (repo / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"
    assert not (repo / "new.txt").exists()


@pytest.mark.parametrize(
    ("result", "exception"),
    [
        (CommandResult(False, "", "ordinary failure", 2), RuntimeError),
        (CommandResult(False, "", "model timed out", -1), AutoCoderTimeoutError),
        (CommandResult(False, "", "capacity exhausted", 1), AutoCoderUsageLimitError),
    ],
)
def test_failures_preserve_existing_classification(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: CommandResult, exception: type[Exception]) -> None:
    configure_muse(tmp_path, monkeypatch)
    with patch("src.auto_coder.muse_client.CommandExecutor.run_command", return_value=result):
        with pytest.raises(exception):
            MuseClient("muse-payg")._run_llm_cli("Implement it")
    assert git(repo, "rev-list", "--count", "HEAD") == "1"
