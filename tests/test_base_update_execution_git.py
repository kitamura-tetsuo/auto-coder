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
    git(seed, "checkout", "-b", "develop")
    (seed / "develop.txt").write_text("develop\n")
    git(seed, "add", "develop.txt")
    git(seed, "commit", "-m", "develop base")
    git(seed, "push", "-u", "origin", "develop")
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


def clean_update_repository(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    """Create a PR branch that can merge a one-commit base advance cleanly."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "common.txt").write_text("common\n")
    git(seed, "add", "common.txt")
    git(seed, "commit", "-m", "initial")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    git(seed, "checkout", "-b", "feature")
    (seed / "feature.txt").write_text("feature\n")
    git(seed, "add", "feature.txt")
    git(seed, "commit", "-m", "feature")
    git(seed, "push", "-u", "origin", "feature")
    input_head = git(seed, "rev-parse", "HEAD").stdout.strip()
    git(seed, "checkout", "main")
    (seed / "base.txt").write_text("base\n")
    git(seed, "add", "base.txt")
    git(seed, "commit", "-m", "base advance")
    git(seed, "push")
    work = tmp_path / "work"
    git(tmp_path, "clone", "--branch", "feature", str(remote), str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    return (
        work,
        remote,
        {
            "number": 2552,
            "head": {"ref": "feature", "sha": input_head},
            "base": {"ref": "main"},
        },
    )


@pytest.mark.parametrize("outcome", ["lost-response", "actor-advanced"])
def test_clean_update_reconciles_failed_push_without_blind_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _use_real_commands: None,
    outcome: str,
) -> None:
    work, remote, pr_data = clean_update_repository(tmp_path)
    monkeypatch.chdir(work)
    push_calls = 0
    provider = MagicMock()

    def controlled_push() -> CommandResult:
        nonlocal push_calls
        push_calls += 1
        if outcome == "lost-response":
            git(work, "push", "origin", "HEAD:feature")
        else:
            actor = tmp_path / "actor"
            git(tmp_path, "clone", "--branch", "feature", str(remote), str(actor))
            git(actor, "config", "user.email", "actor@example.com")
            git(actor, "config", "user.name", "Actor")
            (actor / "actor.txt").write_text("actor\n")
            git(actor, "add", "actor.txt")
            git(actor, "commit", "-m", "actor advance")
            git(actor, "push", "origin", "feature")
        return CommandResult(False, "", "controlled lost response", 1)

    with (
        patch("auto_coder.pr_processor.git_push", side_effect=controlled_push),
        patch("auto_coder.pr_processor._dispatch_cloud_conflict_repair", provider),
        patch("auto_coder.base_update_execution.time.sleep") as sleep,
    ):
        result = _update_with_base_branch("owner/repo", pr_data, AutomationConfig())

    assert result.repair_result is not None
    candidate = git(work, "rev-parse", "HEAD").stdout.strip()
    remote_head = git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/feature").stdout.strip()
    assert result.repair_result.resulting_head == candidate
    assert result.repair_result.input_head == pr_data["head"]["sha"]
    assert result.repair_result.repository == "owner/repo"
    assert result.repair_result.pr_number == 2552
    assert push_calls == 1
    sleep.assert_not_called()
    provider.assert_not_called()
    if outcome == "lost-response":
        assert result.repair_result.disposition is RepairDisposition.APPLIED
        assert result.repair_result.synchronized is True
        assert remote_head == candidate
    else:
        assert result.repair_result.disposition is RepairDisposition.REFUSED
        assert result.repair_result.synchronized is False
        assert remote_head != candidate
        assert git(work, "status", "--porcelain").stdout == ""


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
        patch("auto_coder.base_update_execution._run_local_conflict_operation", return_value=False) as operation,
    ):
        assert not _resolve_pr_merge_conflicts("owner/repo", 2542, AutomationConfig())

    operation.assert_called_once_with(ANY, ANY, pr_data)


def test_definitive_entry_refuses_changed_head_and_base_before_git_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None) -> None:
    work, observed = conflict_repository(tmp_path)
    monkeypatch.chdir(work)
    changed = {
        **observed,
        "head": {"ref": "feature", "sha": "H2"},
        "base": {"ref": "develop", "sha": "B2"},
    }
    api = MagicMock()
    api.pulls.get.side_effect = [observed, changed]
    github = MagicMock(token="token")
    original_head = git(work, "rev-parse", "HEAD").stdout.strip()
    with (
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.pr_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.base_update_execution._run_local_conflict_operation") as operation,
    ):
        assert not _resolve_pr_merge_conflicts("owner/repo", 2542, AutomationConfig())

    operation.assert_not_called()
    assert git(work, "rev-parse", "HEAD").stdout.strip() == original_head
    assert git(work, "status", "--porcelain").stdout == ""
