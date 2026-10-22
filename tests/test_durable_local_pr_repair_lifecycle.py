"""Joined durable local PR repair lifecycle regressions for Issue #2562.

These tests deliberately control only GitHub observations and the editing
transport. Admission, SQLite claims, the runner, immutable artifact producer,
Git commit/push finalizer, and wake/settlement transitions are production code.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.ci_observation import CIConclusion
from auto_coder.entity_invalidation import DurableInvalidationQueue, EntityIdentity
from auto_coder.github_pending_work import PendingWorkStore
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.local_job_handoff import LocalJobKind, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.local_pr_repair_recovery import LOCAL_PR_REPAIR_FINALIZATION_EFFECT, LocalPRRepairFinalizationHandler, rediscover_pr_repair_finalizations
from auto_coder.pr_local_job_runtime import build_pr_local_job_adapters
from auto_coder.pr_processor import ClaimedReviewThreadGateState
from auto_coder.pr_repair_finalizer import PRRepairJobFinalizer, PRRepairPublicationDisposition, PRRepairTargetObservation
from auto_coder.pr_repair_workspace import PRRepairWorkspaceProducer
from auto_coder.util.gh_cache import ReviewThread
from tests.test_local_conflict_repair_job import git as conflict_git
from tests.test_local_conflict_repair_job import history
from tests.test_z_local_ci_repair_job import Harness, git, metadata, wait_until

pytestmark = pytest.mark.pr_repair_contract


def _assert_fresh_h2_gates(h2: str, invalidation_path: Path, pr_number: int) -> None:
    """Drive the normal post-wake PR entry with stale and current evidence."""
    from auto_coder.ci_observation import ObservationAvailability
    from tests.test_pr_lifecycle_contract import _snapshot, run_public_ci_consumer

    restarted = DurableInvalidationQueue(invalidation_path)
    restarted.recover("owner/repo")
    claimed = restarted.claim("owner/repo")
    assert claimed is not None and claimed.identity == EntityIdentity("owner/repo", "pr", pr_number)
    assert restarted.begin_processing(claimed)

    stale_green = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS)
    stale_result, stale_merge, _ = run_public_ci_consumer(stale_green, head=h2, pr_number=pr_number)
    stale_merge.assert_not_called()
    assert stale_result.outcome.value == "deferred"

    pending_h2 = _snapshot(ObservationAvailability.KNOWN, CIConclusion.PENDING, head_sha=h2)
    pending_result, pending_merge, _ = run_public_ci_consumer(pending_h2, head=h2, pr_number=pr_number)
    pending_merge.assert_not_called()
    assert pending_result.outcome.value == "deferred"

    green_h2 = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS, head_sha=h2)
    blocked = ClaimedReviewThreadGateState(
        blocking_unresolved=(ReviewThread(id="h2-blocking-thread"),),
        has_blocking_unresolved=True,
    )
    review_result, review_blocked_merge, _ = run_public_ci_consumer(green_h2, head=h2, pr_number=pr_number, thread_state=blocked)
    review_blocked_merge.assert_not_called()
    assert review_result.outcome.value != "success"

    merged_result, eligible_merge, _ = run_public_ci_consumer(
        green_h2,
        head=h2,
        pr_number=pr_number,
        thread_state=ClaimedReviewThreadGateState(),
    )
    assert eligible_merge.call_count == 1, (merged_result.outcome, merged_result.actions_taken, merged_result.error)
    assert merged_result.outcome.value == "success"
    assert restarted.complete(claimed) is False
    assert restarted.pending_count("owner/repo") == 0


def _remote_observer(remote: Path, base: str):  # type: ignore[no-untyped-def]
    def observe(repository: str, number: int) -> PRRepairTargetObservation:
        head = git(remote, "rev-parse", "refs/heads/feature")
        return PRRepairTargetObservation(repository, number, "owner/repo", "feature", head, "owner/repo", "main", base, True, False, True)

    return observe


def test_ci_public_origin_runs_claim_artifact_publication_and_fresh_head_gate(tmp_path: Path, _use_real_commands) -> None:
    harness = Harness.create(tmp_path)
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    owner = ImplementationOwner("pr", 17)
    harness.implementation_execution_id = slots.start_execution(owner) or ""
    assert harness.implementation_execution_id
    job_id = harness.offer_from_pr_handler()
    wakes: list[str] = []
    invalidation_path = tmp_path / "invalidations.sqlite3"
    invalidations = DurableInvalidationQueue(invalidation_path)
    runner = harness.runner()

    assert runner.poll() == 1
    wait_until(lambda: runner.active_count() == 0)
    produced = harness.store.get(job_id)
    assert produced is not None and produced.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    artifact = harness.store.get_result_artifact(produced.result_reference)
    assert artifact is not None
    verified = PRRepairWorkspaceProducer(harness.store, harness.root / "workspaces").revalidate_result(produced, artifact)
    assert verified.manifest.job_id == job_id
    assert harness.editor_calls == 1
    git(Path(verified.manifest.workspace), "config", "user.email", "test@example.com")
    git(Path(verified.manifest.workspace), "config", "user.name", "Test")

    finalizer = PRRepairJobFinalizer(
        harness.store,
        _remote_observer(harness.remote, harness.base),
        lambda repository, number, head: not wakes.append(head) and invalidations.invalidate(EntityIdentity(repository, "pr", number)),
        finish_execution=lambda record: slots.finish_execution_by_id(record.implementation_execution_id),
    )
    publication = finalizer.finalize(job_id)
    runner.close()

    assert publication.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert publication.candidate_sha != harness.head
    assert git(harness.remote, "rev-parse", "refs/heads/feature") == publication.candidate_sha
    assert wakes == [publication.candidate_sha]
    assert harness.store.get(job_id).state is LocalJobState.SETTLED  # type: ignore[union-attr]
    assert slots.active_execution_ids(owner) == ()
    _assert_fresh_h2_gates(publication.candidate_sha, invalidation_path, 17)


def test_conflict_public_origin_runs_real_merge_artifact_and_exact_h2_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _use_real_commands,
) -> None:
    from auto_coder import pr_processor
    from auto_coder.pr_processor import _start_mergeability_remediation

    root, head, base = history(tmp_path)
    remote = tmp_path / "origin.git"
    unrelated_head = conflict_git(root, "rev-parse", "HEAD").stdout.strip()
    metadata_value = {
        "number": 7,
        "state": "open",
        "merged": False,
        "title": "Conflict",
        "body": "",
        "user": {"login": "human"},
        "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": head},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": base},
    }
    github = Mock(token="token")
    github.get_pull_request_metadata_strict.return_value = metadata_value
    api = Mock()
    api.pulls.get.return_value = metadata_value
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(root)
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    owner = ImplementationOwner("pr", 7)
    execution_id = slots.start_execution(owner)
    assert execution_id

    with (
        pr_processor.local_pr_repair_context(pr_processor.LocalPRRepairContext(LocalJobStore(), execution_id)),
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.pr_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.pr_processor._selected_local_backend_name", return_value="codex"),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
    ):
        admission = _start_mergeability_remediation(7, "DIRTY", "owner/repo")

    assert admission.repair_result is not None
    store = LocalJobStore()
    job = store.get(admission.repair_result.receipt)
    assert job is not None and job.kind is LocalJobKind.PR_MERGE_CONFLICT
    editor_calls = 0

    def edit(_prompt: str, workspace: Path) -> str:
        nonlocal editor_calls
        editor_calls += 1
        (workspace / "shared.txt").write_text("feature and base resolved\n", encoding="utf-8")
        return "ACTION_SUMMARY: resolved conflict"

    with patch("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", return_value=None):
        adapters = build_pr_local_job_adapters(
            github,
            AutomationConfig(),
            store,
            repository_path=root,
            conflict_editor_factory=lambda _backend, _job: edit,
            conflict_workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "workspaces"),
        )
        runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters=adapters)
        assert runner.poll() == 1
        wait_until(lambda: runner.active_count() == 0)

    wakes: list[str] = []
    invalidation_path = tmp_path / "invalidations.sqlite3"
    invalidations = DurableInvalidationQueue(invalidation_path)
    retained = store.get(job.job_id)
    assert retained is not None
    git(Path(retained.workspace_path), "config", "user.email", "test@example.com")
    git(Path(retained.workspace_path), "config", "user.name", "Test")
    publication = PRRepairJobFinalizer(
        store,
        _remote_observer(remote, base),
        lambda repository, number, h2: not wakes.append(h2) and invalidations.invalidate(EntityIdentity(repository, "pr", number)),
        finish_execution=lambda record: slots.finish_execution_by_id(record.implementation_execution_id),
    ).finalize(job.job_id)
    runner.close()
    assert publication.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert editor_calls == 1
    assert git(remote, "rev-parse", "refs/heads/feature") == publication.candidate_sha
    workspace = Path(store.get(job.job_id).workspace_path)  # type: ignore[union-attr]
    assert git(workspace, "show", "-s", "--format=%P", publication.candidate_sha).split() == [head, base]
    assert (workspace / "shared.txt").read_text(encoding="utf-8") == "feature and base resolved\n"
    assert conflict_git(root, "rev-parse", "HEAD").stdout.strip() == unrelated_head
    assert wakes == [publication.candidate_sha]
    assert store.get(job.job_id).state is LocalJobState.SETTLED  # type: ignore[union-attr]
    assert slots.active_execution_ids(owner) == ()
    _assert_fresh_h2_gates(publication.candidate_sha, invalidation_path, 7)


def test_conflict_terminal_failure_recovery_retires_exact_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """A definite adapter failure is recoverably final, unlike unknown entry."""
    from auto_coder import pr_processor
    from auto_coder.pr_processor import _start_mergeability_remediation

    root, head, base = history(tmp_path)
    remote = tmp_path / "origin.git"
    metadata_value = {
        "number": 7,
        "state": "open",
        "merged": False,
        "title": "Conflict",
        "body": "",
        "user": {"login": "human"},
        "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": head},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": base},
    }
    github = Mock(token="token")
    github.get_pull_request_metadata_strict.return_value = metadata_value
    api = Mock()
    api.pulls.get.return_value = metadata_value
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(root)
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    owner = ImplementationOwner("pr", 7)
    execution_id = slots.start_execution(owner)
    assert execution_id

    with (
        pr_processor.local_pr_repair_context(pr_processor.LocalPRRepairContext(LocalJobStore(), execution_id)),
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.pr_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.pr_processor._selected_local_backend_name", return_value="codex"),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
    ):
        admission = _start_mergeability_remediation(7, "DIRTY", "owner/repo")

    assert admission.repair_result is not None
    store = LocalJobStore()
    job = store.get(admission.repair_result.receipt)
    assert job is not None
    editor_calls = 0

    def leave_markers(_prompt: str, _workspace: Path) -> str:
        nonlocal editor_calls
        editor_calls += 1
        return "ACTION_SUMMARY: attempted resolution"

    with patch("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", return_value=None):
        adapters = build_pr_local_job_adapters(
            github,
            AutomationConfig(),
            store,
            repository_path=root,
            conflict_editor_factory=lambda _backend, _job: leave_markers,
            conflict_workspace_producer=PRRepairWorkspaceProducer(store, tmp_path / "failed-workspaces"),
        )
        runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters=adapters)
        assert runner.poll() == 1
        wait_until(lambda: runner.active_count() == 0)
        runner.close()

    recorded = store.get(job.job_id)
    assert recorded is not None and recorded.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert recorded.invocation_outcome.value == "failed"
    artifact = store.get_result_artifact(recorded.result_reference)
    assert artifact is not None and "failed" in artifact.output
    assert editor_calls == 1

    reopened = LocalJobStore(store.path)
    pending = PendingWorkStore(tmp_path / "failed-pending.sqlite3")
    identity = rediscover_pr_repair_finalizations("owner/repo", reopened, pending)[0]
    obligation = pending.get(identity)
    assert obligation is not None
    observer = Mock(side_effect=AssertionError("terminal failure must not observe or publish"))
    wake = Mock(side_effect=AssertionError("terminal failure must not wake H2"))
    finalizer = PRRepairJobFinalizer(
        reopened,
        observer,
        wake,
        finish_execution=lambda record: slots.finish_execution_by_id(record.implementation_execution_id),
    )
    handler = LocalPRRepairFinalizationHandler("owner/repo", reopened, finalizer)
    first = handler.recover(obligation)
    second = handler.recover(obligation)

    assert first.completed_effects == second.completed_effects == (LOCAL_PR_REPAIR_FINALIZATION_EFFECT,)
    settled = reopened.get(job.job_id)
    assert settled is not None and settled.state is LocalJobState.SETTLED
    assert settled.result_reference == artifact.artifact_id
    assert reopened.get_result_artifact(artifact.artifact_id) == artifact
    assert git(remote, "rev-parse", "refs/heads/feature") == head
    assert editor_calls == 1
    observer.assert_not_called()
    wake.assert_not_called()
    assert slots.active_execution_ids(owner) == ()
    assert slots.start_execution(owner) is not None
