from __future__ import annotations

import shutil
import subprocess
import time
from multiprocessing import Event, Process
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Thread
from unittest.mock import patch

from loguru import logger

from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_job_finalizer import IssueJobFinalizer
from auto_coder.issue_job_workspace import IssueJobSource, IssueJobWorkspaceProducer
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobState, LocalJobStore, WorkspaceReclamationPhase
from auto_coder.local_job_runner import process_owner
from auto_coder.local_job_workspace_cleanup import (
    DEFAULT_RETENTION_SECONDS,
    reclaim_finalized_workspaces,
    workspace_retention_seconds,
)
from auto_coder.pr_repair_finalizer import PRRepairJobFinalizer, PRRepairPublicationDisposition
from auto_coder.runtime_locks import ensure_lock_directory, file_lock

from .test_issue_job_finalizer import _completed_job, claim, repository
from .test_pr_repair_finalizer import completed_job as completed_pr_job
from .test_pr_repair_finalizer import observer
from .test_pr_repair_finalizer import repository as pr_repository


def _published(tmp_path: Path, number: int = 2583):
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, number)
    existing = {"number": 91, "head": {"ref": checkpoint.work_branch}, "body": f"Closes #{number}"}
    finalizer = IssueJobFinalizer(store, slots, lambda *_: existing, lambda *_: None)
    assert finalizer.finalize(checkpoint.job_id).disposition == "published"
    return store, checkpoint, finalizer


def _hold_workspace_lock(store_path: str, job_id: str, entered: Event, release: Event) -> None:
    store = LocalJobStore(Path(store_path))
    record = store.get(job_id)
    assert record is not None
    from auto_coder.local_job_workspace_cleanup import workspace_reclamation_lock

    lock = workspace_reclamation_lock(store, record)
    ensure_lock_directory(lock)
    with file_lock(lock):
        entered.set()
        assert release.wait(10)


def _published_muse_job(tmp_path: Path, number: int, *, live_writer: bool = False):
    source, _head = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    acquired = claim(store, number)
    with store._connect() as connection:
        connection.execute("UPDATE local_jobs SET runner_owner='executor' WHERE job_id=?", (acquired.record.job_id,))
    record = store.get(acquired.record.job_id)
    assert record is not None
    acquired = LocalJobClaim(record, True)
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json", tmp_path / "retired.json")
    assert slots.reserve_new(ImplementationOwner("issue", number))
    producer = IssueJobWorkspaceProducer(store, tmp_path / "workspaces", slots)

    def edit(workspace: Path, _prompt: str) -> str:
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
        (workspace / "value.txt").write_text("muse result\n", encoding="utf-8")
        return "ACTION_SUMMARY: completed by Muse"

    checkpoint = producer.execute(acquired, IssueJobSource("owner/repo", source, "main", f"issue-{number}"), edit, checkpoint_result=False)
    assert store.bind_continuation(acquired, f"session-{number}", "executor") is not None
    provider = checkpoint.workspace.parent / "muse-A"
    provider.mkdir()
    process = subprocess.Popen(["sleep", "60" if live_writer else "0.01"], start_new_session=True)
    owner = process_owner(process.pid)
    if not live_writer:
        process.wait(timeout=5)
    assert store.bind_continuation_writer(checkpoint.job_id, "executor", provider, owner)
    assert store.record_candidate_output(checkpoint.job_id, "executor", 0, checkpoint.output)
    assert store.confirm_final_output(checkpoint.job_id, "executor", 0)
    artifact = store.persist_result_artifact(acquired, InvocationOutcome.COMPLETED, checkpoint.result_reference, executor_token="executor")
    assert artifact is not None
    assert store.record_result(acquired, InvocationOutcome.COMPLETED, artifact.artifact_id, executor_token="executor")
    existing = {"number": 92, "head": {"ref": checkpoint.work_branch}, "body": f"Closes #{number}"}
    finalizer = IssueJobFinalizer(store, slots, lambda *_: existing, lambda *_: None)
    assert finalizer.finalize(checkpoint.job_id).disposition == "published"
    return store, checkpoint, process


def test_positive_release_uses_first_release_time_and_replays_after_deletion(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, finalizer = _published(tmp_path)
    root = tmp_path / "workspaces"

    first = reclaim_finalized_workspaces(store, now=100.0, retention_seconds=10.0, issue_root=root)
    assert first[0].decision == "retained"
    release = store.get_workspace_reclamation(checkpoint.job_id, checkpoint.execution_incarnation)
    assert release is not None
    assert release.first_release_at == 100.0
    assert release.phase is WorkspaceReclamationPhase.RELEASED

    second = reclaim_finalized_workspaces(store, now=105.0, retention_seconds=0.0, issue_root=root)
    assert second[0].decision == "reclaimed"
    assert not checkpoint.workspace.parent.exists()
    assert finalizer.finalize(checkpoint.job_id).disposition == "published"
    reopened = store.__class__(store.path).get_workspace_reclamation(checkpoint.job_id, checkpoint.execution_incarnation)
    assert reopened is not None and reopened.phase is WorkspaceReclamationPhase.DELETED
    assert reopened.first_release_at == 100.0


def test_replaced_incarnation_is_never_deleted(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _finalizer = _published(tmp_path)
    root = tmp_path / "workspaces"
    assert reclaim_finalized_workspaces(store, now=1.0, retention_seconds=10.0, issue_root=root)[0].decision == "retained"
    shutil.rmtree(checkpoint.workspace.parent)
    checkpoint.workspace.parent.mkdir(parents=True)
    marker = checkpoint.workspace.parent / "unrelated.txt"
    marker.write_text("keep", encoding="utf-8")

    result = reclaim_finalized_workspaces(store, now=20.0, retention_seconds=10.0, issue_root=root)

    assert result[0].decision == "retained"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_replacement_before_first_release_scan_is_never_adopted(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _finalizer = _published(tmp_path)
    bound = store.get(checkpoint.job_id)
    assert bound is not None and bound.workspace_device > 0 and bound.workspace_inode > 0
    shutil.rmtree(checkpoint.workspace.parent)
    checkpoint.workspace.mkdir(parents=True)
    marker = checkpoint.workspace / "important.txt"
    marker.write_text("unrelated replacement", encoding="utf-8")

    immediate = reclaim_finalized_workspaces(LocalJobStore(store.path), now=1, retention_seconds=0, issue_root=tmp_path / "workspaces")
    later = reclaim_finalized_workspaces(LocalJobStore(store.path), now=100000, retention_seconds=86400, issue_root=tmp_path / "workspaces")

    assert immediate[0].decision == later[0].decision == "retained"
    assert "physical identity" in immediate[0].reason
    assert store.get_workspace_reclamation(checkpoint.job_id, checkpoint.execution_incarnation) is None
    assert marker.read_text(encoding="utf-8") == "unrelated replacement"


def test_unproven_or_external_workspace_is_retained(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _finalizer = _published(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    original = checkpoint.workspace.parent
    shutil.rmtree(original)
    original.symlink_to(external, target_is_directory=True)

    result = reclaim_finalized_workspaces(store, now=10.0, retention_seconds=0.0, issue_root=tmp_path / "workspaces")

    assert result[0].decision == "retained"
    assert external.exists()


def test_retention_configuration(monkeypatch) -> None:
    monkeypatch.setenv("AUTO_CODER_JOB_WORKSPACE_RETENTION_SECONDS", "0")
    assert workspace_retention_seconds() == 0
    monkeypatch.setenv("AUTO_CODER_JOB_WORKSPACE_RETENTION_SECONDS", "nan")
    assert workspace_retention_seconds() == DEFAULT_RETENTION_SECONDS
    monkeypatch.setenv("AUTO_CODER_JOB_WORKSPACE_RETENTION_SECONDS", "-1")
    assert workspace_retention_seconds() == DEFAULT_RETENTION_SECONDS


def test_pr_positive_cleanup_and_receipt_only_replay(tmp_path: Path, _use_real_commands) -> None:
    source, remote, head, base = pr_repository(tmp_path)
    store, claim_record, prepared, _artifact = completed_pr_job(tmp_path, source, head, base)
    wakes = 0

    def wake(*_args) -> bool:
        nonlocal wakes
        wakes += 1
        return True

    finalizer = PRRepairJobFinalizer(store, observer(remote, base), wake)
    published = finalizer.finalize(claim_record.record.job_id)
    assert published.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert wakes == 1

    result = reclaim_finalized_workspaces(store, retention_seconds=0, pr_root=tmp_path / "workspaces-7")

    assert result[0].decision == "reclaimed"
    assert not prepared.workspace.parent.exists()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("receipt-only replay must not touch GitHub, Git, or CI wake")

    replay = PRRepairJobFinalizer(LocalJobStore(store.path), forbidden, forbidden)
    with patch.object(replay, "_git", side_effect=forbidden):
        repeated = replay.finalize(claim_record.record.job_id)
    assert repeated.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert repeated.candidate_sha == published.candidate_sha
    assert wakes == 1


def test_existing_success_receipts_are_adopted_but_unproven_backlog_is_retained(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _finalizer = _published(tmp_path)
    assert store.get_workspace_reclamation(checkpoint.job_id, checkpoint.execution_incarnation) is None

    adopted = reclaim_finalized_workspaces(store, now=50, retention_seconds=100, issue_root=tmp_path / "workspaces")

    assert adopted[0].decision == "retained"
    release = LocalJobStore(store.path).get_workspace_reclamation(checkpoint.job_id, checkpoint.execution_incarnation)
    assert release is not None and release.first_release_at == 50

    with store._connect() as connection:
        connection.execute(
            "DELETE FROM local_job_effects WHERE job_id=? AND execution_incarnation=? AND name='association'",
            (checkpoint.job_id, checkpoint.execution_incarnation),
        )
        connection.execute(
            "DELETE FROM local_job_workspace_reclamations WHERE job_id=? AND execution_incarnation=?",
            (checkpoint.job_id, checkpoint.execution_incarnation),
        )
    unproven = reclaim_finalized_workspaces(store, now=1000, retention_seconds=0, issue_root=tmp_path / "workspaces")
    assert unproven[0].decision == "retained"
    assert checkpoint.workspace.exists()


def test_active_completed_workspace_is_preserved_at_zero_retention(tmp_path: Path, _use_real_commands) -> None:
    _source, _remote, store, _slots, checkpoint = _completed_job(tmp_path)
    retained = store.get(checkpoint.job_id)
    assert retained is not None and retained.state is LocalJobState.RESULT_RECORDED

    assert reclaim_finalized_workspaces(store, retention_seconds=0, issue_root=tmp_path / "workspaces") == ()
    assert checkpoint.workspace.exists()


def test_final_confirmed_muse_requires_every_sibling_to_be_attributed(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _process = _published_muse_job(tmp_path, 2584)
    unknown = checkpoint.workspace.parent / "muse-B"
    unknown.mkdir()
    (unknown / "partial.txt").write_text("unattributed work", encoding="utf-8")

    retained = reclaim_finalized_workspaces(store, retention_seconds=0, issue_root=tmp_path / "workspaces")

    assert retained[0].decision == "retained"
    assert unknown.joinpath("partial.txt").read_text(encoding="utf-8") == "unattributed work"


def test_final_confirmed_muse_with_only_settled_attributed_root_is_reclaimed(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _process = _published_muse_job(tmp_path, 2585)

    reclaimed = reclaim_finalized_workspaces(store, retention_seconds=0, issue_root=tmp_path / "workspaces")

    assert reclaimed[0].decision == "reclaimed"
    assert not checkpoint.workspace.parent.exists()


def test_final_confirmed_muse_with_live_writer_is_retained(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, process = _published_muse_job(tmp_path, 2586, live_writer=True)
    try:
        retained = reclaim_finalized_workspaces(store, retention_seconds=0, issue_root=tmp_path / "workspaces")
        assert retained[0].decision == "retained"
        assert checkpoint.workspace.exists()
    finally:
        process.kill()
        process.wait(timeout=5)


def test_final_confirmed_muse_with_unknown_writer_is_retained(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _process = _published_muse_job(tmp_path, 2587)
    assert store.bind_continuation_writer(checkpoint.job_id, "executor", checkpoint.workspace.parent / "muse-A", "ambiguous:unknown")

    retained = reclaim_finalized_workspaces(store, retention_seconds=0, issue_root=tmp_path / "workspaces")

    assert retained[0].decision == "retained"
    assert checkpoint.workspace.exists()


def test_two_process_finalizer_authority_window_excludes_cleanup(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _finalizer = _published(tmp_path)
    entered = Event()
    release = Event()
    holder = Process(target=_hold_workspace_lock, args=(str(store.path), checkpoint.job_id, entered, release))
    holder.start()
    assert entered.wait(5)
    results = []
    cleaner = Thread(target=lambda: results.extend(reclaim_finalized_workspaces(store, retention_seconds=0, issue_root=tmp_path / "workspaces")))
    cleaner.start()
    time.sleep(0.1)
    assert cleaner.is_alive()
    assert checkpoint.workspace.exists()
    release.set()
    cleaner.join(5)

    holder.join(5)
    assert holder.exitcode == 0
    assert not cleaner.is_alive()
    assert results[0].decision == "reclaimed"


def test_restart_resumes_deleting_after_partial_git_removal(tmp_path: Path, _use_real_commands) -> None:
    store, checkpoint, _finalizer = _published(tmp_path)
    root = tmp_path / "workspaces"
    assert reclaim_finalized_workspaces(store, now=1, retention_seconds=10, issue_root=root)[0].decision == "retained"
    release = store.get_workspace_reclamation(checkpoint.job_id, checkpoint.execution_incarnation)
    assert release is not None
    assert store.transition_workspace_reclamation(release, WorkspaceReclamationPhase.RELEASED, WorkspaceReclamationPhase.DELETING)
    shutil.rmtree(checkpoint.workspace / ".git")

    resumed = reclaim_finalized_workspaces(LocalJobStore(store.path), now=20, retention_seconds=10, issue_root=root)

    assert resumed[0].decision == "reclaimed"
    assert not checkpoint.workspace.parent.exists()


def test_every_cleanup_decision_is_logged_once_with_exact_context(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    logs: list[str] = []
    sink = logger.add(lambda message: logs.append(str(message)), format="{message}")
    try:
        retained_root = tmp_path / "retained"
        retained_root.mkdir()
        retained_store, retained_checkpoint, _ = _published(retained_root, 2601)
        retained = reclaim_finalized_workspaces(retained_store, now=1, retention_seconds=10, issue_root=retained_root / "workspaces")[0]

        unproven_root = tmp_path / "unproven"
        unproven_root.mkdir()
        unproven_store, unproven_checkpoint, _ = _published(unproven_root, 2602)
        with unproven_store._connect() as connection:
            connection.execute(
                "DELETE FROM local_job_effects WHERE job_id=? AND execution_incarnation=? AND name='association'",
                (unproven_checkpoint.job_id, unproven_checkpoint.execution_incarnation),
            )
        unproven = reclaim_finalized_workspaces(unproven_store, retention_seconds=0, issue_root=unproven_root / "workspaces")[0]

        shutil.rmtree(retained_checkpoint.workspace.parent)
        retained_checkpoint.workspace.mkdir(parents=True)
        changed = reclaim_finalized_workspaces(retained_store, now=20, retention_seconds=10, issue_root=retained_root / "workspaces")[0]

        reclaimed_root = tmp_path / "reclaimed"
        reclaimed_root.mkdir()
        reclaimed_store, _reclaimed_checkpoint, _ = _published(reclaimed_root, 2603)
        reclaimed = reclaim_finalized_workspaces(reclaimed_store, retention_seconds=0, issue_root=reclaimed_root / "workspaces")[0]

        failed_root = tmp_path / "failed"
        failed_root.mkdir()
        failed_store, _failed_checkpoint, _ = _published(failed_root, 2604)
        monkeypatch.setattr("auto_coder.local_job_workspace_cleanup.shutil.rmtree", lambda _path: (_ for _ in ()).throw(OSError("forced removal failure")))
        failed = reclaim_finalized_workspaces(failed_store, retention_seconds=0, issue_root=failed_root / "workspaces")[0]
    finally:
        logger.remove(sink)

    for result in (retained, unproven, changed, reclaimed, failed):
        expected = f"Workspace cleanup decision={result.decision} job={result.job_id} path={result.path} reason={result.reason}"
        assert sum(expected in entry for entry in logs) == 1


def test_issue_replay_refreshes_settlement_before_checkout_after_cleanup(tmp_path: Path, _use_real_commands) -> None:
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, 2701)
    existing = {"number": 101, "head": {"ref": checkpoint.work_branch}, "body": "Closes #2701"}
    stale_store = LocalJobStore(store.path)
    initial_read = ThreadEvent()
    resume = ThreadEvent()
    original_get = stale_store.get
    reads = 0

    def delayed_first_get(job_id: str):
        nonlocal reads
        record = original_get(job_id)
        reads += 1
        if reads == 1:
            initial_read.set()
            assert resume.wait(10)
        return record

    stale_store.get = delayed_first_get  # type: ignore[method-assign]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("settled replay must not repeat publication")

    replay_finalizer = IssueJobFinalizer(stale_store, slots, forbidden, forbidden)
    replay_results = []
    replay = Thread(target=lambda: replay_results.append(replay_finalizer.finalize(checkpoint.job_id)))
    replay.start()
    assert initial_read.wait(5)

    published = IssueJobFinalizer(store, slots, lambda *_: existing, lambda *_: None).finalize(checkpoint.job_id)
    assert published.disposition == "published"
    assert reclaim_finalized_workspaces(store, retention_seconds=0, issue_root=tmp_path / "workspaces")[0].decision == "reclaimed"
    with patch("auto_coder.issue_job_finalizer.checkout_lock", side_effect=AssertionError("deleted checkout accessed")):
        resume.set()
        replay.join(5)

    assert not replay.is_alive()
    assert replay_results[0].disposition == "published"
    assert replay_results[0].pr_number == published.pr_number


def test_pr_replay_refreshes_settlement_before_checkout_after_cleanup(tmp_path: Path, _use_real_commands) -> None:
    source, remote, head, base = pr_repository(tmp_path)
    slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json", tmp_path / "retired.json")
    owner = ImplementationOwner("pr", 7)
    unrelated_owner = ImplementationOwner("pr", 8)
    execution = slots.start_execution(owner)
    unrelated = slots.start_execution(unrelated_owner)
    assert execution is not None and unrelated is not None
    store, claim_record, prepared, _artifact = completed_pr_job(tmp_path, source, head, base, execution_id=execution)
    stale_store = LocalJobStore(store.path)
    initial_read = ThreadEvent()
    resume = ThreadEvent()
    original_get = stale_store.get
    reads = 0

    def delayed_first_get(job_id: str):
        nonlocal reads
        record = original_get(job_id)
        reads += 1
        if reads == 1:
            initial_read.set()
            assert resume.wait(10)
        return record

    stale_store.get = delayed_first_get  # type: ignore[method-assign]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("settled replay must not perform external work")

    replay_finalizer = PRRepairJobFinalizer(
        stale_store,
        forbidden,
        forbidden,
        finish_execution=lambda record: slots.finish_execution_by_id(record.implementation_execution_id),
    )
    replay_results = []
    replay = Thread(target=lambda: replay_results.append(replay_finalizer.finalize(claim_record.record.job_id)))
    replay.start()
    assert initial_read.wait(5)

    wakes = 0

    def wake(*_args) -> bool:
        nonlocal wakes
        wakes += 1
        return True

    published = PRRepairJobFinalizer(
        store,
        observer(remote, base),
        wake,
        finish_execution=lambda record: slots.finish_execution_by_id(record.implementation_execution_id),
    ).finalize(claim_record.record.job_id)
    assert published.disposition is PRRepairPublicationDisposition.PUBLISHED
    assert reclaim_finalized_workspaces(store, retention_seconds=0, pr_root=tmp_path / "workspaces-7")[0].decision == "reclaimed"
    with (
        patch("auto_coder.pr_repair_finalizer.checkout_lock", side_effect=AssertionError("deleted checkout accessed")),
        patch.object(replay_finalizer, "_git", side_effect=AssertionError("deleted Git state accessed")),
    ):
        resume.set()
        replay.join(5)

    assert not replay.is_alive()
    assert replay_results[0].disposition is PRRepairPublicationDisposition.PUBLISHED
    assert replay_results[0].candidate_sha == published.candidate_sha
    assert wakes == 1
    assert slots.active_execution_ids(owner) == ()
    assert owner in slots.active_owners()
    assert slots.active_execution_ids(unrelated_owner) == (unrelated,)
    assert not prepared.workspace.parent.exists()
