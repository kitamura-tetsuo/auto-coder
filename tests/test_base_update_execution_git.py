"""Production-entry regressions for local conflict execution."""

import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import _resolve_pr_merge_conflicts, _update_with_base_branch
from auto_coder.pr_repair_dispatch import RepairDisposition
from auto_coder.utils import CommandResult
from auto_coder.worktree_utils import isolated_local_llm_worktree

pytestmark = pytest.mark.pr_repair_contract


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True)


def conflict_repository(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "release/2.x", str(seed))
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "app.txt").write_text("original\n")
    git(seed, "add", "app.txt")
    git(seed, "commit", "-m", "initial")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "release/2.x")
    git(seed, "checkout", "-b", "feature")
    (seed / "app.txt").write_text("feature\n")
    git(seed, "commit", "-am", "feature")
    git(seed, "push", "-u", "origin", "feature")
    git(seed, "checkout", "release/2.x")
    (seed / "app.txt").write_text("base\n")
    git(seed, "commit", "-am", "base")
    git(seed, "push")
    git(tmp_path, "clone", "--branch", "feature", str(remote), str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    head = git(work, "rev-parse", "HEAD").stdout.strip()
    base = git(seed, "rev-parse", "release/2.x").stdout.strip()
    return work, {
        "number": 2542,
        "body": "<!-- auto-coder:local-llm -->",
        "author": {"login": "local-user"},
        "user": {"login": "local-user"},
        "head": {"ref": "feature", "sha": head},
        "base": {"ref": "release/2.x", "sha": base},
        "baseRefName": "release/2.x",
    }


@pytest.mark.parametrize(
    ("response", "resolve_file", "push_ok", "expected"),
    [
        ("CANNOT_FIX", False, True, RepairDisposition.FAILED),
        ("ACTION_SUMMARY: repaired", True, True, RepairDisposition.APPLIED),
        ("ACTION_SUMMARY: repaired", True, False, RepairDisposition.FAILED),
    ],
)
def test_update_entry_preserves_real_private_workspace_publication_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _use_real_commands: None,
    response: str,
    resolve_file: bool,
    push_ok: bool,
    expected: RepairDisposition,
) -> None:
    work, pr_data = conflict_repository(tmp_path)
    monkeypatch.chdir(work)
    turns = 0

    def local_turn(_prompt: str) -> str:
        nonlocal turns
        turns += 1
        assert git(work, "ls-files", "-u").stdout == ""
        with isolated_local_llm_worktree(work) as workspace:
            private_file = Path(workspace) / "app.txt"
            assert "<<<<<<<" in private_file.read_text()
            if resolve_file:
                private_file.write_text("feature\nbase\n")
        return response

    failed_push = CommandResult(False, "", "push rejected", 1)
    successful_push = CommandResult(True, "", "", 0)
    github = MagicMock()
    github.get_pull_request.return_value = pr_data
    github.get_pr_details.return_value = {**pr_data, "head_branch": "feature"}
    with (
        patch("auto_coder.conflict_resolver.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.conflict_resolver.get_linked_issues_context", return_value=""),
        patch("auto_coder.conflict_resolver.get_commit_log", return_value="history"),
        patch("auto_coder.conflict_resolver.create_high_score_backend_manager", return_value=None),
        patch("auto_coder.conflict_resolver.run_llm_prompt", side_effect=local_turn),
        patch(
            "auto_coder.conflict_resolver.git_push",
            side_effect=[successful_push] if push_ok else [failed_push, failed_push],
        ) as push,
        patch("auto_coder.conflict_resolver._trigger_fallback_for_conflict_failure"),
        patch("auto_coder.pr_processor._dispatch_cloud_conflict_repair") as cloud,
    ):
        cloud.return_value = MagicMock(disposition=RepairDisposition.REFUSED, reason="local origin", actions=())
        result = _update_with_base_branch("owner/repo", pr_data, AutomationConfig())

    assert result.repair_result is not None
    assert result.repair_result.disposition is expected
    assert turns == 1
    assert push.call_count == int(resolve_file)
    assert not any("degrade" in action.lower() for action in result)
    if expected is RepairDisposition.APPLIED:
        assert result.repair_result.resulting_head == git(work, "rev-parse", "HEAD").stdout.strip()


def test_definitive_entry_carries_non_main_base_into_real_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None) -> None:
    work, pr_data = conflict_repository(tmp_path)
    monkeypatch.chdir(work)
    api = MagicMock()
    api.pulls.get.return_value = pr_data
    github = MagicMock(token="token")
    with (
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.pr_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.pr_processor._execute_local_conflict_operation", return_value=False) as operation,
    ):
        assert not _resolve_pr_merge_conflicts("owner/repo", 2542, AutomationConfig())

    operation.assert_called_once_with("owner/repo", 2542, ANY)
