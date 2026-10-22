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
from auto_coder.ci_repair_authority import evaluate_ci_repair_snapshot
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.local_job_handoff import LocalJobKind, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.pr_local_job_runtime import build_pr_local_job_adapters
from auto_coder.pr_repair_finalizer import PRRepairJobFinalizer, PRRepairPublicationDisposition, PRRepairTargetObservation
from auto_coder.pr_repair_workspace import PRRepairWorkspaceProducer
from tests.test_local_conflict_repair_job import git as conflict_git
from tests.test_local_conflict_repair_job import history
from tests.test_z_local_ci_repair_job import Harness, git, metadata, wait_until

pytestmark = pytest.mark.pr_repair_contract


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
        lambda _repo, _number, head: not wakes.append(head),
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
    # Publication is not a gate result: a pending H2 remains ineligible, while
    # a fresh terminal-success observation for exactly H2 is independently usable.
    pending_allowed, _ = evaluate_ci_repair_snapshot(harness_snapshot(publication.candidate_sha, CIConclusion.PENDING))
    green_allowed, _ = evaluate_ci_repair_snapshot(harness_snapshot(publication.candidate_sha, CIConclusion.SUCCESS))
    assert pending_allowed is False and green_allowed is False


def harness_snapshot(head: str, conclusion: CIConclusion):  # type: ignore[no-untyped-def]
    from tests.test_z_local_ci_repair_job import snapshot

    return snapshot(head, conclusion)


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
    retained = store.get(job.job_id)
    assert retained is not None
    git(Path(retained.workspace_path), "config", "user.email", "test@example.com")
    git(Path(retained.workspace_path), "config", "user.name", "Test")
    publication = PRRepairJobFinalizer(
        store,
        _remote_observer(remote, base),
        lambda _repo, _number, h2: not wakes.append(h2),
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
