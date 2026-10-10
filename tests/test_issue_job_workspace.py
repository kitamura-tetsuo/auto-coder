from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_job_workspace import IssueJobSource, IssueJobWorkspaceError, IssueJobWorkspaceProducer
from auto_coder.local_job_handoff import LocalJobKind, LocalJobOffer, LocalJobStore


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "value.txt").write_text("original\n", encoding="utf-8")
    git(repo, "add", "value.txt")
    git(repo, "commit", "-m", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def claim(store: LocalJobStore, target: int):
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", target, f"attempt-{target}", "codex", f"prompt-{target}")
    identity = IssueAttemptIdentity("owner", "repo", target, f"attempt-{target}")
    guard = IssueDispatchGuard(store.path.with_name(f"dispatch-{target}.sqlite3"))
    dispatch = guard.reserve(identity, CandidateHandoff("codex", "local"))
    assert dispatch.admitted
    accepted = store.offer_issue(offer, identity, guard)
    assert accepted is not None
    acquired = store.claim(offer.job_id)
    assert acquired is not None and acquired.acquired
    return acquired


def test_concurrent_jobs_use_distinct_pinned_workspaces(tmp_path: Path, _use_real_commands) -> None:
    repo, head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")
    claims = [claim(store, number) for number in (1, 2)]
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    results = []

    def run(index: int) -> None:
        source = IssueJobSource("owner/repo", repo, "main", head, f"issue-{index + 1}")

        def invoke(workspace: Path, prompt: str) -> str:
            entered[index].set()
            assert entered[1 - index].wait(5)
            assert release.wait(5)
            (workspace / "value.txt").write_text(f"{prompt}\n", encoding="utf-8")
            return f"ACTION_SUMMARY: {prompt}"

        results.append(producer.execute(claims[index], source, invoke))

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    assert entered[0].wait(5) and entered[1].wait(5)
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--show-current") == "main"
    release.set()
    for thread in threads:
        thread.join(5)

    assert len(results) == 2
    assert results[0].workspace != results[1].workspace
    assert {item.work_branch for item in results} == {"issue-1", "issue-2"}
    assert {item.workspace.joinpath("value.txt").read_text(encoding="utf-8") for item in results} == {"prompt-1\n", "prompt-2\n"}
    reopened = LocalJobStore(tmp_path / "jobs.sqlite3")
    assert all(reopened.get(item.job_id).result_reference == item.result_reference for item in results)  # type: ignore[union-attr]
    assert all(Path(reopened.get(item.job_id).workspace_path).is_dir() for item in results)  # type: ignore[union-attr]


def test_source_advance_does_not_replace_authorized_commit(tmp_path: Path, _use_real_commands) -> None:
    repo, head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 3)
    (repo / "value.txt").write_text("later\n", encoding="utf-8")
    git(repo, "commit", "-am", "later")
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    with pytest.raises(IssueJobWorkspaceError, match="no longer identifies"):
        producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", head, "issue-3"), lambda *_: "ACTION_SUMMARY: done")

    record = store.get(acquired.record.job_id)
    assert record is not None
    assert record.result_reference == ""
    assert record.workspace_path == ""


def test_model_head_change_is_not_a_successful_checkpoint(tmp_path: Path, _use_real_commands) -> None:
    repo, head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 4)
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    def commit(workspace: Path, _prompt: str) -> str:
        git(workspace, "config", "user.email", "test@example.com")
        git(workspace, "config", "user.name", "Test")
        (workspace / "value.txt").write_text("committed\n", encoding="utf-8")
        git(workspace, "commit", "-am", "unauthorized")
        return "ACTION_SUMMARY: done"

    with pytest.raises(IssueJobWorkspaceError, match="changed private HEAD"):
        producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", head, "issue-4"), commit)

    record = store.get(acquired.record.job_id)
    assert record is not None and record.result_reference == ""
    assert Path(record.workspace_path).joinpath("value.txt").read_text(encoding="utf-8") == "committed\n"


def test_index_flags_cannot_hide_working_file_result(tmp_path: Path, _use_real_commands) -> None:
    repo, head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 5)
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    def edit_hidden_from_git(workspace: Path, _prompt: str) -> str:
        git(workspace, "update-index", "--assume-unchanged", "value.txt")
        (workspace / "value.txt").write_text("actual result\n", encoding="utf-8")
        assert git(workspace, "diff", "--", "value.txt") == ""
        return "ACTION_SUMMARY: changed value"

    checkpoint = producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", head, "issue-5"), edit_hidden_from_git)
    artifact = LocalJobStore(store.path).get_result_artifact(checkpoint.result_reference)
    assert artifact is not None
    manifest = json.loads(artifact.output)
    assert manifest["changed_files"]["value.txt"]["checksum"] == __import__("hashlib").sha256(b"actual result\n").hexdigest()


@pytest.mark.parametrize("response", ["CANNOT_FIX", "implementation failed", "ACTION_SUMMARY:", "ACTION_SUMMARY: ok\nextra output"])
def test_unconfirmed_response_never_records_completed_result(tmp_path: Path, _use_real_commands, response: str) -> None:
    repo, head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 10 + len(response))
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    with pytest.raises(IssueJobWorkspaceError, match="confirmed implementation result"):
        producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", head, f"issue-{10 + len(response)}"), lambda *_: response)

    reopened = LocalJobStore(store.path)
    record = reopened.get(acquired.record.job_id)
    assert record is not None and record.result_reference == ""
    assert record.invocation_outcome is None


def test_preparation_preserves_authorized_tracked_untracked_and_ignored_content(tmp_path: Path, _use_real_commands) -> None:
    repo, head = repository(tmp_path)
    (repo / ".gitignore").write_text("local.env\n", encoding="utf-8")
    (repo / "tracked-context.txt").write_text("committed\n", encoding="utf-8")
    git(repo, "add", ".gitignore", "tracked-context.txt")
    git(repo, "commit", "-m", "context baseline")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "tracked-context.txt").write_text("authorized modification\n", encoding="utf-8")
    (repo / "staged-context.txt").write_text("authorized staged content\n", encoding="utf-8")
    git(repo, "add", "staged-context.txt")
    (repo / "untracked-context.txt").write_text("authorized untracked content\n", encoding="utf-8")
    (repo / "local.env").write_text("TOKEN=accepted-input\n", encoding="utf-8")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 6)
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces")

    def inspect(workspace: Path, prompt: str) -> str:
        assert prompt == "prompt-6"
        assert (workspace / "tracked-context.txt").read_text(encoding="utf-8") == "authorized modification\n"
        assert (workspace / "staged-context.txt").read_text(encoding="utf-8") == "authorized staged content\n"
        assert (workspace / "untracked-context.txt").read_text(encoding="utf-8") == "authorized untracked content\n"
        assert (workspace / "local.env").read_text(encoding="utf-8") == "TOKEN=accepted-input\n"
        return "ACTION_SUMMARY: inspected retained context"

    checkpoint = producer.execute(acquired, IssueJobSource("owner/repo", repo, "main", head, "issue-6"), inspect)
    assert checkpoint.workspace.joinpath("local.env").read_text(encoding="utf-8") == "TOKEN=accepted-input\n"
