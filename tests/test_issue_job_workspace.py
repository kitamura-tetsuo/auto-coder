from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

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
    with store._connect() as connection:
        store._insert(connection, offer, f"upstream-{target}")
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
