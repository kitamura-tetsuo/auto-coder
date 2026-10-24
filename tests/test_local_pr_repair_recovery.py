"""Retained PR repairs survive controller death and lost execution records."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.ci_repair_authority import CIRepairAuthority
from auto_coder.execution_trace import Outcome, get_trace_collector
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobExecutionResult
from auto_coder.pr_repair_dispatch import RepairKind
from auto_coder.process_identity import ProcessIdentity
from tests.test_dashboard_observability import _assert_required_stage_visible, _mounted_detail
from tests.test_local_pr_repair_engine_entry import _install_real_offer, _pr, _production_scope
from tests.test_local_pr_repair_job import snapshot


@pytest.fixture
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))


@pytest.mark.parametrize("kind", [LocalJobKind.PR_CI_FAILURE, LocalJobKind.PR_MERGE_CONFLICT, LocalJobKind.PR_REVIEW_CORRECTION])
@pytest.mark.parametrize("state", list(LocalJobState))
def test_dead_pr_controller_retains_every_unsettled_job_execution(runtime_root, monkeypatch: pytest.MonkeyPatch, kind: LocalJobKind, state: LocalJobState) -> None:
    slots = ImplementationSlotRepository("owner/repo", 2)
    monkeypatch.setattr(slots, "_current_process_identity", lambda: ProcessIdentity(1, "old-boot", 10))
    monkeypatch.setattr(slots, "_read_process_identity", lambda pid: ProcessIdentity(pid, "old-boot", 10))
    owner = ImplementationOwner("issue", 10)
    execution = slots.start_execution(owner, implementation_pr=17)
    unrelated = slots.start_execution(ImplementationOwner("pr", 18))
    assert execution is not None and unrelated is not None
    store = LocalJobStore()
    offered = LocalJobOffer(kind, "owner/repo", 17, "attempt", "codex", "repair", execution)
    with store._connect() as connection:
        store._insert(connection, offered, "incarnation")
        store._insert(connection, LocalJobOffer(kind, "foreign/repo", 18, "foreign-attempt", "codex", "foreign repair", unrelated), "foreign-incarnation")
        connection.execute("UPDATE local_jobs SET state=? WHERE job_id=?", (state.value, offered.job_id))
        connection.commit()
    monkeypatch.setattr(slots, "_read_process_identity", lambda pid: ProcessIdentity(pid, "new-boot", 20))

    removed = slots.reclaim_stale_executions()

    assert set(removed) == ({execution, unrelated} if state is LocalJobState.SETTLED else {unrelated})
    assert slots.active_execution_ids(owner) == (() if state is LocalJobState.SETTLED else (execution,))
    assert slots.owner_incarnation(owner) is not None
    assert LocalJobStore().retained_execution_ids("foreign/repo") == frozenset({unrelated})


@pytest.fixture(params=[("pr", RepairKind.CI_FAILURE), ("issue", RepairKind.CI_FAILURE), ("pr", RepairKind.MERGE_CONFLICT), ("issue", RepairKind.MERGE_CONFLICT)])
def pending_repair(runtime_root, request):
    github = MagicMock()
    github.get_pull_request_metadata_strict.side_effect = lambda _repo, number: _pr(number)
    store = LocalJobStore()
    slots = ImplementationSlotRepository("owner/repo", 3)
    owner_kind, repair_kind = request.param
    owner = ImplementationOwner(owner_kind, 10 if owner_kind == "issue" else 17)

    def engine() -> AutomationEngine:
        result = AutomationEngine(github, AutomationConfig())
        result.local_job_store = store
        result.implementation_slots = slots
        slots.resolve_owner = MagicMock(return_value=owner)
        result._check_and_handle_closed_branch = MagicMock(return_value=True)
        result._create_candidate_from_single = MagicMock(side_effect=lambda _repo, _kind, number, **_kwargs: Candidate("pr", _pr(number), 0))
        return result

    initial = engine()
    observed = snapshot()
    authority = CIRepairAuthority(True, "current head failure", "H17", replace(observed, subject=replace(observed.subject, head_sha="H17")))
    with _production_scope(initial), _install_real_offer(initial, github, repair_kind, authority):
        result = initial._process_single_candidate_unified("owner/repo", Candidate("pr", _pr(17), 0), initial.config, origin="worker")
    assert result.repair_result is not None and result.repair_result.local_handoff is not None
    job = store.get(result.repair_result.local_handoff.job_id)
    assert job is not None and job.state is LocalJobState.PENDING and not job.provider_entered
    return engine(), slots, owner, job


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("execution_missing", [False, True])
@patch("auto_coder.dashboard.ui")
def test_explicit_restart_runs_only_original_pr_job_and_reaches_mounted_detail(mock_ui, pending_repair, force: bool, execution_missing: bool) -> None:
    engine, slots, owner, original = pending_repair
    store = engine.local_job_store
    incarnation = slots.owner_incarnation(owner)
    if execution_missing:
        # Reproduce #5752: the old controller already reclaimed this ID.
        slots.finish_execution(owner, original.implementation_execution_id)
    invoked = []

    class Adapter:
        def authorize_provider_entry(self, job):
            assert slots.active_execution_ids(owner) == (original.implementation_execution_id,)
            return True

        def invoke(self, job):
            invoked.append(job.job_id)
            return LocalJobExecutionResult(InvocationOutcome.CANNOT_FIX, "CANNOT_FIX")

    traces = get_trace_collector()
    traces.clear()
    with (
        _production_scope(engine),
        patch("auto_coder.automation_engine.process_pull_request", side_effect=AssertionError("retained repair must not be offered again")) as process,
        patch("auto_coder.pr_local_job_runtime.build_pr_local_job_adapters", return_value={original.kind: Adapter()}),
    ):
        result = engine.process_single("owner/repo", "pr", 17, explicit_only=True, force=force)

    process.assert_not_called()
    assert result["errors"] == []
    assert result["target_outcome"] == ExplicitTargetOutcome.DEFERRED.value
    assert invoked == [original.job_id]
    assert result["repair_result"].local_handoff.job_id == original.job_id
    assert result["repair_result"].local_handoff.implementation_execution_id == original.implementation_execution_id
    retained = store.get(original.job_id)
    assert retained is not None and retained.state is LocalJobState.SETTLED
    assert retained.invocation_outcome is InvocationOutcome.CANNOT_FIX
    assert slots.active_execution_ids(owner) == ()
    assert slots.owner_incarnation(owner) == incarnation
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_jobs").fetchone()[0] == 1
    events = [event for event in traces.get_snapshot(repository="owner/repo", item_type="pr", item_number=17).events if event.stage_id == "pr.repair-delegation"]
    assert len(events) == 1
    assert events[0].outcome == Outcome.DEFERRED.value
    assert events[0].facts["admission_status"] == "retained"
    assert events[0].facts["job_id"] == original.job_id
    assert events[0].facts["execution_restored"] is execution_missing
    diagram = _mounted_detail(mock_ui, "pr", 17)
    _assert_required_stage_visible(diagram, "local repair admission")
    assert "outcome: deferred" in diagram


@pytest.mark.parametrize("condition", ["running", "entered", "missing_owner", "different_owner", "competing_execution", "provider_session", "missing_claim"])
def test_missing_execution_refuses_uncertain_or_competing_authority(pending_repair, condition: str) -> None:
    engine, slots, owner, original = pending_repair
    store = engine.local_job_store
    slots.finish_execution(owner, original.implementation_execution_id)
    if condition == "running":
        assert store.claim(original.job_id) is not None
    elif condition == "entered":
        with store._connect() as connection:
            connection.execute("UPDATE local_jobs SET provider_entered=1 WHERE job_id=?", (original.job_id,))
            connection.commit()
    elif condition == "missing_owner":
        slots.release(owner)
    elif condition == "different_owner":
        slots.resolve_owner = MagicMock(return_value=ImplementationOwner("pr", 19))
        assert slots.reserve_new(ImplementationOwner("pr", 19))
    elif condition == "competing_execution":
        assert slots.start_execution(owner) is not None
    elif condition == "provider_session":
        slots.record_provider_session(owner, "competing-session")
    elif condition == "missing_claim":
        with store._connect() as connection:
            connection.execute("DELETE FROM local_pr_repair_claims WHERE job_id=?", (original.job_id,))
            connection.commit()
    before = slots.storage_path.read_bytes()
    with _production_scope(engine), patch("auto_coder.automation_engine.process_pull_request") as process:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("pr", _pr(17), 0), engine.config, explicit_only=True, force=True, origin="explicit-single-target")
    process.assert_not_called()
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.repair_result is None
    assert result.target_reason == "Cannot attach explicit processing to retained PR repair authority or execution"
    assert slots.storage_path.read_bytes() == before
    assert store.get(original.job_id).implementation_execution_id == original.implementation_execution_id


def test_restore_rejects_foreign_job_and_wrong_issue_membership(pending_repair) -> None:
    _engine, slots, owner, job = pending_repair
    slots.finish_execution(owner, job.implementation_execution_id)
    assert slots.restore_local_pr_repair_execution(replace(job, repository="foreign/repo"), owner) is False
    unrelated = ImplementationOwner("issue", 19)
    assert slots.reserve_new(unrelated)
    assert slots.restore_local_pr_repair_execution(job, unrelated) is False
    assert slots.active_execution_ids(owner) == ()
    assert slots.active_execution_ids(unrelated) == ()


@patch("auto_coder.dashboard.ui")
def test_changed_base_retires_unentered_job_and_executes_fresh_repair(mock_ui, pending_repair) -> None:
    engine, slots, owner, original = pending_repair
    store = engine.local_job_store
    incarnation = slots.owner_incarnation(owner)
    changed = _pr(17)
    changed["base"]["sha"] = "B2"
    engine.github.get_pull_request_metadata_strict.side_effect = lambda *_: changed
    engine._create_candidate_from_single.side_effect = lambda *_args, **_kwargs: Candidate("pr", changed, 0)
    observed = snapshot()
    authority = CIRepairAuthority(True, "current head failure", "H17", replace(observed, subject=replace(observed.subject, head_sha="H17")))
    invoked = []

    class Adapter:
        def authorize_provider_entry(self, job):
            return True

        def invoke(self, job):
            invoked.append(job.job_id)
            return LocalJobExecutionResult(InvocationOutcome.CANNOT_FIX, "CANNOT_FIX")

    kind = RepairKind.CI_FAILURE if original.kind is LocalJobKind.PR_CI_FAILURE else RepairKind.MERGE_CONFLICT
    traces = get_trace_collector()
    traces.clear()
    with (
        _production_scope(engine),
        _install_real_offer(engine, engine.github, kind, authority),
        patch("auto_coder.pr_local_job_runtime.build_pr_local_job_adapters", return_value={original.kind: Adapter()}),
    ):
        result = engine.process_single("owner/repo", "pr", 17, explicit_only=True, force=True)

    assert result["errors"] == []
    assert result["target_outcome"] == ExplicitTargetOutcome.DEFERRED.value
    assert len(invoked) == 1 and invoked[0] != original.job_id
    old = store.get(original.job_id)
    assert old.state is LocalJobState.SETTLED
    assert not old.provider_entered and old.invocation_outcome is None
    assert store.get_pr_repair_claim(invoked[0]).base_identity == "B2"
    assert store.get(invoked[0]).state is LocalJobState.SETTLED
    assert slots.active_execution_ids(owner) == ()
    assert slots.owner_incarnation(owner) == incarnation
    events = [event for event in traces.get_snapshot(repository="owner/repo", item_type="pr", item_number=17).events if event.stage_id == "pr.repair-delegation"]
    superseded = [event for event in events if event.facts.get("admission_status") == "superseded"]
    assert len(superseded) == 1
    assert superseded[0].facts["job_id"] == original.job_id
    assert superseded[0].facts["provider_started"] is False
    diagram = _mounted_detail(mock_ui, "pr", 17)
    _assert_required_stage_visible(diagram, "local repair admission")
    rows = [row for call in mock_ui.table.call_args_list for row in call.kwargs.get("rows", [])]
    assert any("admission_status: superseded" in row.get("facts", "") and original.job_id in row["facts"] for row in rows)


@pytest.mark.parametrize("condition", ["unchanged", "incomplete", "unavailable", "running", "entered", "claim_race"])
def test_stale_retirement_preserves_uncertain_or_claimed_jobs(pending_repair, condition) -> None:
    engine, slots, owner, original = pending_repair
    store = engine.local_job_store
    changed = _pr(17)
    changed["base"]["sha"] = "B2"
    engine.github.get_pull_request_metadata_strict.side_effect = lambda *_: changed
    if condition == "unchanged":
        changed["base"]["sha"] = "B1"
    elif condition == "incomplete":
        changed["head"]["sha"] = ""
    elif condition == "unavailable":
        engine.github.get_pull_request_metadata_strict.side_effect = RuntimeError("offline")
    elif condition == "running":
        assert store.claim(original.job_id) is not None
        original = store.get(original.job_id)
    elif condition == "entered":
        original = replace(original, provider_entered=True)
    elif condition == "claim_race":
        assert store.claim(original.job_id) is not None
    assert engine._retire_superseded_pending_pr_repair("owner/repo", original, slots) is False
    assert store.get(original.job_id).state is not LocalJobState.SETTLED
    assert slots.active_execution_ids(owner) == (original.implementation_execution_id,)


def test_explicit_pending_reason_includes_preparation_failure(pending_repair) -> None:
    engine, _slots, _owner, original = pending_repair

    class Adapter:
        def authorize_provider_entry(self, job):
            raise RuntimeError("source unavailable")

    with _production_scope(engine), patch("auto_coder.pr_local_job_runtime.build_pr_local_job_adapters", return_value={original.kind: Adapter()}):
        result = engine.process_single("owner/repo", "pr", 17, explicit_only=True, force=True)
    assert result["errors"] == []
    assert "authorization unavailable: source unavailable" in result["target_reason"]
    assert engine.local_job_store.get(original.job_id).state is LocalJobState.PENDING
