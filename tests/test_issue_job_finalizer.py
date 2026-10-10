from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_job_finalizer import IssueJobFinalizer
from auto_coder.issue_job_workspace import IssueJobSource, IssueJobWorkspaceProducer
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobStore

from .test_issue_job_workspace import claim, git, repository


def _completed_job(tmp_path: Path, number: int = 41):
    source, head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, number)

    def edit(workspace: Path, _prompt: str) -> str:
        git(workspace, "config", "user.email", "test@example.com")
        git(workspace, "config", "user.name", "Test")
        (workspace / "value.txt").write_text("implemented\n", encoding="utf-8")
        return "ACTION_SUMMARY: implemented exact result"

    checkpoint = IssueJobWorkspaceProducer(store, tmp_path / "workspaces").execute(acquired, IssueJobSource("owner/repo", source, "main", head, f"issue-{number}"), edit)
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json", tmp_path / "retired.json")
    assert slots.reserve_new(ImplementationOwner("issue", number))
    return source, store, slots, checkpoint


def test_lost_pr_create_response_is_reconciled_without_duplicate(tmp_path: Path, _use_real_commands) -> None:
    source, store, slots, checkpoint = _completed_job(tmp_path)
    observed: dict[str, object] = {}
    creates = 0

    def lookup(_repository: str, branch: str):
        return observed.get(branch)

    def create(record, _summary: str) -> None:
        nonlocal creates
        creates += 1
        observed[record.work_branch] = {"number": 91, "head": {"ref": record.work_branch}, "body": f"Closes #{record.target_number}"}
        raise TimeoutError("response lost after remote creation")

    finalizer = IssueJobFinalizer(store, slots, lookup, create)
    result = finalizer.finalize(checkpoint.job_id)

    assert result.disposition == "published"
    assert result.pr_number == 91
    assert creates == 1
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "commit").state == "completed"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "push").state == "completed"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "pr").evidence == "91"  # type: ignore[union-attr]
    assert git(source, "rev-parse", "issue-41") == git(checkpoint.workspace, "rev-parse", "HEAD")
    assert finalizer.resume_all("owner/repo") == ()
    assert creates == 1


def test_unconfirmed_push_stays_pending_and_replay_does_not_create_pr(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    _source, store, slots, checkpoint = _completed_job(tmp_path, 42)
    calls = 0

    def failed_push(**_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(success=False, stderr="network result unknown")

    monkeypatch.setattr("auto_coder.issue_job_finalizer.git_push", failed_push)
    finalizer = IssueJobFinalizer(store, slots, lambda *_: None, lambda *_: (_ for _ in ()).throw(AssertionError("PR must not be created")))

    first = finalizer.finalize(checkpoint.job_id)
    second = IssueJobFinalizer(LocalJobStore(store.path), slots, lambda *_: None, lambda *_: None).finalize(checkpoint.job_id)

    assert first.disposition == second.disposition == "pending"
    assert calls == 2
    assert store.get(checkpoint.job_id).invocation_outcome is InvocationOutcome.COMPLETED  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "commit").state == "completed"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "push").state == "indeterminate"  # type: ignore[union-attr]
    assert store.get_effect(checkpoint.job_id, checkpoint.execution_incarnation, "pr") is None


def test_cannot_fix_never_touches_git_or_publication(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, 43)
    artifact = store.persist_result_artifact(acquired, InvocationOutcome.CANNOT_FIX, "CANNOT_FIX")
    assert artifact is not None
    assert store.record_result(acquired, InvocationOutcome.CANNOT_FIX, artifact.artifact_id)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json", tmp_path / "retired.json")
    finalizer = IssueJobFinalizer(store, slots, lambda *_: (_ for _ in ()).throw(AssertionError("lookup called")), lambda *_: None)

    result = finalizer.finalize(acquired.record.job_id)

    assert result.disposition == "cannot_fix"
    assert store.get(acquired.record.job_id).state.value == "settled"  # type: ignore[union-attr]
