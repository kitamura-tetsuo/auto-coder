"""Production-path regressions for producer maintenance checkout ownership."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository_bytes(repository: Path) -> dict[str, bytes]:
    """Capture checkout and Git administrative state without invoking Git."""
    return {str(path.relative_to(repository)): path.read_bytes() for path in repository.rglob("*") if path.is_file()}


@pytest.fixture
def monitored_repository(tmp_path: Path, _use_real_commands) -> Path:
    repository = tmp_path / "monitored"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    (repository / "tracked.txt").write_text("original\n")
    (repository / "staged.txt").write_text("original\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    return repository


@pytest.mark.parametrize("checkout_state", ["local-edits", "detached-head", "merge-in-progress"])
def test_recurring_producer_maintenance_preserves_checkout(
    monitored_repository: Path,
    mock_github_client,
    monkeypatch: pytest.MonkeyPatch,
    checkout_state: str,
) -> None:
    """Run real producer iterations while representative local checkout state exists."""
    repository = monitored_repository
    if checkout_state == "local-edits":
        (repository / "tracked.txt").write_text("unstaged local edit\n")
        (repository / "staged.txt").write_text("staged local edit\n")
        _git(repository, "add", "staged.txt")
        (repository / "untracked.txt").write_text("must survive\n")
    elif checkout_state == "detached-head":
        _git(repository, "checkout", "--detach", "HEAD")
    else:
        _git(repository, "checkout", "-b", "external-merge")
        (repository / "merged.txt").write_text("external operation\n")
        _git(repository, "add", "merged.txt")
        _git(repository, "commit", "-m", "external change")
        _git(repository, "checkout", "main")
        _git(repository, "merge", "--no-commit", "--no-ff", "external-merge")
        assert (repository / ".git" / "MERGE_HEAD").is_file()

    engine = AutomationEngine(mock_github_client, config=AutomationConfig())
    monkeypatch.chdir(repository)
    before = _repository_bytes(repository)
    waits: list[float] = []

    async def finish_after_two_intervals(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt("producer iteration boundary")
        return False

    with (
        patch("src.auto_coder.automation_engine.check_for_updates_and_restart") as update_check,
        patch.object(engine, "_check_and_handle_closed_branch", return_value=True),
        patch.object(engine, "_sleep_or_wake", side_effect=finish_after_two_intervals),
    ):
        with pytest.raises(KeyboardInterrupt, match="producer iteration boundary"):
            asyncio.run(engine._producer_loop("owner/repository"))

    assert waits == [60, 60]
    assert update_check.call_count == 2
    assert _repository_bytes(repository) == before
