"""Retained local results preserve exact ownership through controller death."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.execution_trace import get_trace_collector
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_job_workspace import IssueJobSource, IssueJobWorkspaceProducer
from auto_coder.local_job_handoff import LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore

from .test_dashboard_observability import _assert_required_stage_visible, _mounted_detail
from .test_issue_job_workspace import git, repository


@pytest.fixture
def retained_result(tmp_path: Path, monkeypatch, _use_real_commands):
    monkeypatch.setenv("HOME", str(tmp_path))
    git(tmp_path, "config", "--global", "user.email", "test@example.com")
    git(tmp_path, "config", "--global", "user.name", "Test")
    snapshot = {"number": 61, "title": "Retained implementation", "state": "open", "body": "## Objective\nUpdate the value.\n\n## Requirements\nREQ-001: Update value.txt.", "labels": []}
    github = MagicMock()
    github.get_issue_dispatch_snapshot_strict.return_value = snapshot
    github.find_pr_by_head_branch_strict.side_effect = lambda _repo, branch: {"number": 77, "head": {"ref": branch}, "body": "Closes #61"}
    engine = AutomationEngine(github, AutomationConfig())
    monkeypatch.setattr(engine, "_get_authoritative_parent_number", lambda *_: None)
    generation = engine._compute_implementation_generation("owner/repo", snapshot, None)
    slots = ImplementationSlotRepository("owner/repo", 1)
    owner = ImplementationOwner("issue", 61)
    execution = slots.start_execution(owner, generation=generation)
    assert execution is not None
    source, head = repository(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "set-url", "origin", str(remote))
    git(source, "push", "origin", "main")
    store = LocalJobStore()
    engine.local_job_store = store
    identity = IssueAttemptIdentity("owner", "repo", 61, "attempt-61")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    assert guard.reserve(identity, CandidateHandoff("codex", "local")).admitted
    offered = store.offer_issue(LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 61, "attempt-61", "codex", "implement", execution), identity, guard)
    assert offered is not None
    claim = store.claim(offered.job_id)
    assert claim is not None

    def implement(workspace, _prompt):
        (workspace / "value.txt").write_text("implemented\n")
        return "ACTION_SUMMARY: implemented and verified"

    checkpoint = IssueJobWorkspaceProducer(store, tmp_path / "workspaces", slots).execute(claim, IssueJobSource("owner/repo", source, "main", "issue-61"), implement)
    return engine, slots, store, owner, execution, checkpoint, remote, snapshot


def test_dead_controller_execution_remains_owned_by_unsettled_job(retained_result, monkeypatch):
    _engine, slots, store, owner, execution, checkpoint, _remote, _snapshot = retained_result
    monkeypatch.setattr(slots, "_read_process_identity", lambda _: None)
    assert slots.reclaim_stale_executions() == ()
    assert slots.active_execution_ids(owner) == (execution,)
    assert slots.release_unbound_idle_owner(owner) is False
    assert store.get(checkpoint.job_id).state is LocalJobState.RESULT_RECORDED


@pytest.mark.parametrize("condition", ["missing", "changed_contract", "replacement", "retired", "unreadable_contract"])
def test_retained_publication_restores_only_missing_current_owner(retained_result, condition):
    engine, slots, store, owner, execution, checkpoint, remote, snapshot = retained_result
    job = store.get(checkpoint.job_id)
    assert job is not None
    slots.release(owner)
    if condition == "changed_contract":
        snapshot["body"] += "\nREQ-002: Preserve another property."
    elif condition == "replacement":
        assert slots.reserve_new(owner)
    elif condition == "retired":
        slots._write_retired({job.owner_incarnation: {"kind": "issue", "number": 61, "incarnation": job.owner_incarnation, "generation": job.owner_generation}})
    elif condition == "unreadable_contract":
        engine.github.get_issue_dispatch_snapshot_strict.side_effect = TimeoutError("snapshot unavailable")
    traces = get_trace_collector()
    with traces.start_execution(repository="owner/repo", item_type="issue", item_number=61, origin="explicit-single-target", stage_id="issue.local-completion", label="retained result"):
        result = engine._resume_local_issue_publications("owner/repo", slots, job_id=job.job_id, origin="explicit-single-target")[0]
    events = [event for event in traces.get_snapshot(repository="owner/repo", item_type="issue", item_number=61).events if event.stage_id == "issue.local-publication-finalization" and event.facts.get("job_id") == job.job_id]
    assert events[-1].facts["owner_restored"] is (condition == "missing")
    if condition == "missing":
        assert result.disposition == "published", result.diagnostic
        assert result.pr_number == 77
        assert slots.owner_incarnation(owner) == job.owner_incarnation
        assert slots.active_execution_ids(owner) == ()
        assert git(remote, "show", "issue-61:value.txt") == "implemented"
        assert store.get(job.job_id).state is LocalJobState.SETTLED
        with patch("auto_coder.dashboard.ui") as mock_ui:
            diagram = _mounted_detail(mock_ui, "issue", 61)
        _assert_required_stage_visible(diagram, "publication finalization")
        assert "outcome: completed" in diagram
    else:
        assert result.disposition == "pending"
        assert store.get(job.job_id).state is LocalJobState.RESULT_RECORDED
        assert git(remote, "branch", "--list", "issue-61") == ""


def test_unreadable_job_authority_does_not_reclaim_execution(retained_result, monkeypatch):
    _engine, slots, _store, _owner, _execution, _checkpoint, _remote, _snapshot = retained_result
    monkeypatch.setattr(slots, "_read_process_identity", lambda _: None)

    def unavailable(_store, _repository):
        raise OSError("durable authority unreadable")

    monkeypatch.setattr(LocalJobStore, "retained_issue_execution_ids", unavailable)
    with pytest.raises(ImplementationSlotUnavailable, match="Cannot verify durable local execution responsibility"):
        slots.reclaim_stale_executions()
