"""Real Git merge snapshots can enter the local private-workspace boundary."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.conflict_resolver import resolve_merge_conflicts_with_llm, scan_conflict_markers
from src.auto_coder.utils import CommandResult
from src.auto_coder.worktree_utils import isolated_local_llm_worktree


def git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True)


@pytest.mark.parametrize("repaired,response,pushed", [(True, "ACTION_SUMMARY: repaired", True), (False, "ACTION_SUMMARY: repaired", False), (False, "CANNOT_FIX", False)])
def test_local_conflict_snapshot_handoff(tmp_path, monkeypatch, _use_real_commands, repaired, response, pushed):
    """Staged markers survive cloning; only repaired, published work succeeds."""
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    source = tmp_path / "app.txt"
    source.write_text("original\n")
    git(tmp_path, "add", "app.txt")
    git(tmp_path, "commit", "-m", "initial")
    git(tmp_path, "checkout", "-b", "issue-5456")
    source.write_text("PR behavior\n")
    git(tmp_path, "commit", "-am", "feature")
    git(tmp_path, "checkout", "main")
    source.write_text("base behavior\n")
    git(tmp_path, "commit", "-am", "base")
    git(tmp_path, "checkout", "issue-5456")
    assert git(tmp_path, "merge", "main", check=False).returncode == 1
    assert git(tmp_path, "ls-files", "-u").stdout
    monkeypatch.chdir(tmp_path)
    calls = []

    def local_turn(prompt):
        calls.append(prompt)
        assert git(tmp_path, "ls-files", "-u").stdout == ""
        assert "app.txt" in scan_conflict_markers()
        with isolated_local_llm_worktree(tmp_path) as workspace:
            private_source = Path(workspace) / "app.txt"
            assert "<<<<<<<" in private_source.read_text()
            assert git(Path(workspace), "ls-files", "-u").stdout == ""
            if repaired:
                private_source.write_text("PR behavior\nbase behavior\n")
        return response

    success = CommandResult(True, "", "", 0)
    with (
        patch("src.auto_coder.conflict_resolver.GitHubClient"),
        patch("src.auto_coder.conflict_resolver.get_linked_issues_context", return_value=""),
        patch("src.auto_coder.conflict_resolver.get_commit_log", return_value="history"),
        patch("src.auto_coder.conflict_resolver.create_high_score_backend_manager", return_value=None),
        patch("src.auto_coder.conflict_resolver.run_llm_prompt", side_effect=local_turn),
        patch("src.auto_coder.conflict_resolver.git_commit_with_retry", return_value=success) as commit,
        patch("src.auto_coder.conflict_resolver.git_push", return_value=success) as push,
        patch("src.auto_coder.conflict_resolver._trigger_fallback_for_conflict_failure") as fallback,
    ):
        actions = resolve_merge_conflicts_with_llm({"number": 5465, "body": "<!-- auto-coder:local-llm -->", "base_branch": "main"}, "app.txt", AutomationConfig(), "owner/repo")

    assert len(calls) == 1
    assert ("ACTION_FLAG:SKIP_ANALYSIS" in actions) is pushed
    assert commit.call_count == int(pushed)
    assert push.call_count == int(pushed)
    assert fallback.call_count == int(not pushed)
    if repaired:
        assert source.read_text() == "PR behavior\nbase behavior\n"
    else:
        assert "app.txt" in scan_conflict_markers()
    assert git(tmp_path, "rev-parse", "--verify", "MERGE_HEAD").returncode == 0
