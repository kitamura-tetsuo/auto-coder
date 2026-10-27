from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore, PRRepairAdmission
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.local_job_workspace_cleanup import reclaim_finalized_workspaces, reclaim_unentered_workspaces
from auto_coder.local_review_repair import LocalReviewRepairStore
from auto_coder.pr_processing_evidence import EditingInvocationEvidence
from auto_coder.pr_repair_workspace import PRRepairSource, PRRepairWorkspaceError, PRRepairWorkspaceProducer


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "controller"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "feature")
    (repo / "feature.txt").write_text("feature\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feature")
    head = git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main", "feature")
    return repo, head, base


def admitted(store: LocalJobStore, tmp_path: Path, head: str, base: str, *, target: int = 7):
    offer = LocalJobOffer(LocalJobKind.PR_CI_FAILURE, "owner/repo", target, f"attempt-{target}", "codex", "repair")
    admission = PRRepairAdmission(
        "owner/repo",
        "feature",
        head,
        "owner/repo",
        "main",
        base,
        "ci_failure",
        f"evidence-{target}",
        f"upstream-{target}",
        ("checks / tests",),
        True,
        True,
        True,
        True,
        ci_complete_failure=True,
    )
    repair_store = LocalReviewRepairStore(tmp_path / f"review-{target}.sqlite3")
    assert store.offer_pr_repair(offer, admission, repair_store) is not None
    claim = store.claim(offer.job_id, "runner")
    assert claim is not None and claim.acquired
    return claim


def admitted_conflict(store: LocalJobStore, tmp_path: Path, head: str, base: str, *, target: int = 8):
    offer = LocalJobOffer(LocalJobKind.PR_MERGE_CONFLICT, "owner/repo", target, f"attempt-{target}", "codex", "repair")
    admission = PRRepairAdmission(
        "owner/repo",
        "feature",
        head,
        "owner/repo",
        "main",
        base,
        "merge_conflict",
        f"evidence-{target}",
        f"upstream-{target}",
        pr_open=True,
        pr_unmerged=True,
        local_route=True,
        owner_allowed=True,
        conflict_eligible=True,
    )
    assert store.offer_pr_repair(offer, admission, LocalReviewRepairStore(tmp_path / f"review-{target}.sqlite3")) is not None
    claim = store.claim(offer.job_id, "process:999999:gone")
    assert claim is not None and claim.acquired
    return claim


@pytest.mark.parametrize("kind", ["ci", "conflict"])
@pytest.mark.parametrize("dead_owner", [False, True])
def test_unentered_retry_prepares_new_workspace(tmp_path: Path, _use_real_commands, kind: str, dead_owner: bool) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = (admitted if kind == "ci" else admitted_conflict)(store, tmp_path, head, base)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    original = producer.prepare(claim, PRRepairSource(repo))
    if dead_owner:
        assert store.release_dead_owner_claim(store.get(claim.record.job_id), "dead unentered owner")
    else:
        assert store.release_unentered_claim(claim, "fresh authority unavailable")
    retry = store.claim(claim.record.job_id, "new-runner")
    assert retry is not None and retry.acquired
    assert retry.record.execution_incarnation != claim.record.execution_incarnation
    assert retry.record.workspace_path == ""
    assert not store.release_unentered_claim(claim, "stale release")
    producer.cleanup_released_workspaces()
    prepared = producer.prepare(retry, PRRepairSource(repo))
    assert prepared.workspace != original.workspace
    assert not original.workspace.parent.exists()
    assert git(prepared.workspace, "rev-parse", "HEAD") == head
    bound = store.get(claim.record.job_id)
    assert bound is not None and bound.workspace_path == str(prepared.workspace)
    assert bound.source_commit == head
    assert bound.provider_entered is False
    losing_claim = store.claim(claim.record.job_id, "other-runner")
    assert losing_claim is not None and not losing_claim.acquired
    assert losing_claim.record == bound
    assert store.mark_provider_entered(retry)
    assert not store.release_unentered_claim(retry, "entered work must stay retained")
    assert store.get(claim.record.job_id).workspace_path == str(prepared.workspace)
    producer.cleanup_released_workspaces()
    assert prepared.workspace.is_dir()


def test_unentered_cleanup_retries_after_restart_and_rebinding(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    original = producer.prepare(claim, PRRepairSource(repo))
    assert store.release_unentered_claim(claim, "authority unavailable")

    def fail_removal(_path):
        raise PermissionError("temporarily busy")

    with monkeypatch.context() as scoped:
        scoped.setattr("auto_coder.local_job_workspace_cleanup.shutil.rmtree", fail_removal)
        result = reclaim_unentered_workspaces(store, pr_root=tmp_path / "workspaces")
    assert len(result) == 1 and result[0].decision == "failed"
    assert original.workspace.is_dir()
    retry = store.claim(claim.record.job_id, "new-runner")
    assert retry is not None and retry.acquired
    prepared = producer.prepare(retry, PRRepairSource(repo))

    result = reclaim_finalized_workspaces(LocalJobStore(store.path), pr_root=tmp_path / "workspaces")
    assert len(result) == 1 and result[0].decision == "reclaimed"
    assert not original.workspace.parent.exists()
    assert prepared.workspace.is_dir()
    assert store.get(claim.record.job_id).workspace_path == str(prepared.workspace)
    assert reclaim_unentered_workspaces(store, pr_root=tmp_path / "workspaces") == ()


def test_manually_removed_unentered_workspace_is_checkpointed_without_recreation(tmp_path: Path, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    assert store.release_unentered_claim(claim, "authority unavailable")
    shutil.rmtree(prepared.workspace.parent)
    result = reclaim_finalized_workspaces(LocalJobStore(store.path), pr_root=tmp_path / "workspaces")
    assert len(result) == 1 and result[0].decision == "reclaimed"
    assert not prepared.workspace.parent.exists()
    assert store.discover_unentered_workspace_reclamations() == ()


@pytest.mark.parametrize("tamper", ["symlink", "replacement", "unknown_sibling"])
def test_unentered_cleanup_preserves_changed_ownership(tmp_path: Path, tamper: str, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    assert store.release_unentered_claim(claim, "authority unavailable")
    target = prepared.workspace.parent
    if tamper == "unknown_sibling":
        marker = target / "unknown"
        marker.write_text("preserve")
    else:
        moved = tmp_path / "moved"
        target.rename(moved)
        if tamper == "symlink":
            target.symlink_to(moved, target_is_directory=True)
        else:
            target.mkdir()
        marker = target / "important"
        marker.write_text("preserve")

    result = reclaim_unentered_workspaces(store, pr_root=tmp_path / "workspaces")
    assert len(result) == 1 and result[0].decision == "failed"
    assert marker.read_text() == "preserve"


def test_unentered_cleanup_survives_filesystem_device_renumbering(tmp_path: Path, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    assert store.release_unentered_claim(claim, "authority unavailable")
    target = prepared.workspace.parent
    observed_device = target.stat().st_dev
    # A reboot can renumber the block device (e.g. 259:4 -> 259:2) while inodes persist.
    with store._connect() as connection:
        connection.execute("UPDATE local_job_workspace_reclamations SET device=? WHERE job_id=?", (observed_device + 2, claim.record.job_id))

    result = reclaim_unentered_workspaces(store, pr_root=tmp_path / "workspaces")

    assert [(item.decision, item.reason) for item in result] == [("reclaimed", "positively unentered incarnation released")]
    assert not target.exists()
    assert store.discover_unentered_workspace_reclamations() == ()


def test_unentered_cleanup_rejects_renumbered_device_with_different_inode(tmp_path: Path, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    assert store.release_unentered_claim(claim, "authority unavailable")
    target = prepared.workspace.parent
    observed = target.stat()
    with store._connect() as connection:
        connection.execute("UPDATE local_job_workspace_reclamations SET device=?, inode=? WHERE job_id=?", (observed.st_dev + 2, observed.st_ino + 1, claim.record.job_id))

    result = reclaim_unentered_workspaces(store, pr_root=tmp_path / "workspaces")

    assert [(item.decision, item.reason) for item in result] == [("failed", "unentered workspace identity or ownership changed")]
    assert prepared.workspace.is_dir()


def test_controller_result_prevents_unentered_release_and_cleanup(tmp_path: Path, _use_real_commands) -> None:
    _repo, store, claim, producer, prepared, manifest = _conflict_checkpoint(tmp_path, target=21)
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest.to_json(), executor_token=claim.record.runner_owner)
    assert artifact is not None
    assert not store.release_unentered_claim(claim, "must retain controller result")
    assert not store.release_dead_owner_claim(store.get(claim.record.job_id), "must retain controller result")
    producer.cleanup_released_workspaces()
    assert prepared.workspace.is_dir()
    assert store.get_execution_result_artifact(claim.record.job_id, claim.record.execution_incarnation) == artifact


def test_exact_remote_workspace_ignores_dirty_checkout_and_records_modes(tmp_path: Path, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    (repo / "dirty.txt").write_text("not authorized\n")
    git(repo, "update-ref", "refs/remotes/origin/feature", base)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    peer = tmp_path / "peer"
    peer.mkdir()
    (peer / "unchanged").write_text("peer\n")

    prepared = producer.prepare(claim, PRRepairSource(repo))
    assert git(prepared.workspace, "rev-parse", "HEAD") == head
    assert not (prepared.workspace / "dirty.txt").exists()
    (prepared.workspace / "feature.txt").unlink()
    executable = prepared.workspace / "tool.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    os.symlink("tool.sh", prepared.workspace / "tool-link")
    invocation = EditingInvocationEvidence("codex", "codex", "gpt-test", "local-ci-repair")
    manifest = producer.capture_result(
        claim,
        prepared,
        InvocationOutcome.COMPLETED,
        "ACTION_SUMMARY: repaired",
        editing_invocations=(invocation,),
    )
    changed = dict(manifest.changed_files)
    assert changed["feature.txt"] is None
    assert changed["tool.sh"] is not None and changed["tool.sh"].mode == 0o755
    assert changed["tool-link"] is not None and changed["tool-link"].symlink
    assert (repo / "dirty.txt").read_text() == "not authorized\n"
    assert (peer / "unchanged").read_text() == "peer\n"

    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest.to_json(), executor_token="runner")
    assert artifact is not None
    verified = producer.revalidate_result(store.get(claim.record.job_id), artifact)  # type: ignore[arg-type]
    assert verified.artifact_id == artifact.artifact_id
    assert verified.manifest.editing_invocations == (invocation,)
    executable.write_text("tampered\n")
    with pytest.raises(PRRepairWorkspaceError, match="files were altered"):
        producer.revalidate_result(store.get(claim.record.job_id), artifact)  # type: ignore[arg-type]


def test_overlapping_real_slot_coordination_preserves_pr_repair_result_identity(tmp_path: Path, monkeypatch, _use_real_commands) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime))
    repositories = [ImplementationSlotRepository("owner/repo", 1, tmp_path / name / "slots.json") for name in ("first-coordinator", "second-coordinator")]
    owners = [ImplementationOwner("issue", number) for number in (2579, 2580)]
    original_mkdir = Path.mkdir
    establishment_entered = {owner.number: threading.Event() for owner in owners}
    release_establishment = {owner.number: threading.Event() for owner in owners}
    establishment_complete = {owner.number: threading.Event() for owner in owners}
    intercepted_threads = set()
    failures = []

    def controlled_mkdir(path, *args, **kwargs):
        thread = threading.current_thread()
        if thread.name.startswith("slot-") and thread.name not in intercepted_threads and path.parent == runtime / "locks":
            intercepted_threads.add(thread.name)
            owner_number = int(thread.name.removeprefix("slot-"))
            establishment_entered[owner_number].set()
            assert release_establishment[owner_number].wait(timeout=2)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", controlled_mkdir)

    def coordinate(repository, owner):
        try:
            execution_id = repository.start_execution(owner)
            assert execution_id is not None
            repository.finish_execution(owner, execution_id)
            establishment_complete[owner.number].set()
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=coordinate, args=pair, name=f"slot-{pair[1].number}") for pair in zip(repositories, owners)]
    previous_umask = os.umask(0o022)
    try:
        threads[0].start()
        assert establishment_entered[owners[0].number].wait(timeout=2)
        threads[1].start()
        assert establishment_entered[owners[1].number].wait(timeout=2)
        release_establishment[owners[0].number].set()
        assert establishment_complete[owners[0].number].wait(timeout=2)
        release_establishment[owners[1].number].set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert failures == []
        for slot_repository, owner in zip(repositories, owners):
            with slot_repository.serialize(owner):
                pass

        repo, head, base = repository(tmp_path)
        store = LocalJobStore(tmp_path / "jobs.sqlite3")
        claim = admitted(store, tmp_path, head, base, target=2579)
        producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
        prepared = producer.prepare(claim, PRRepairSource(repo))
        (prepared.workspace / "feature.txt").write_text("coordinated repair\n")
        manifest = producer.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "ACTION_SUMMARY: repaired")
        artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest.to_json(), executor_token="runner")

        assert artifact is not None
        assert manifest.head_sha == head
        assert manifest.base_sha == base
        assert dict(prepared.source_files)["feature.txt"].mode == 0o644
        assert dict(manifest.changed_files)["feature.txt"].mode == 0o644  # type: ignore[union-attr]
        reopened = LocalJobStore(store.path)
        verified = PRRepairWorkspaceProducer(reopened, tmp_path / "workspaces").revalidate_result(reopened.get(claim.record.job_id), artifact)  # type: ignore[arg-type]
        assert verified.artifact_id == artifact.artifact_id
    finally:
        for release in release_establishment.values():
            release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
        os.umask(previous_umask)


def test_revalidation_recomputes_changed_identities_from_retained_h(tmp_path: Path, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    (prepared.workspace / "feature.txt").write_text("edited\n")
    manifest = producer.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "ACTION_SUMMARY: repaired")
    forged = replace(manifest, changed_files=())
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, forged.to_json(), executor_token="runner")
    assert artifact is not None

    with pytest.raises(PRRepairWorkspaceError, match="changed-file identities are invalid"):
        producer.revalidate_result(store.get(claim.record.job_id), artifact)  # type: ignore[arg-type]


def test_head_ref_advancement_refuses_preparation_before_provider_entry(tmp_path: Path, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    git(repo, "switch", "feature")
    (repo / "advance.txt").write_text("feature")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "advance feature")
    git(repo, "push", "origin", "feature")

    with pytest.raises(PRRepairWorkspaceError, match="no longer resolves"):
        PRRepairWorkspaceProducer(store, tmp_path / "workspaces").prepare(claim, PRRepairSource(repo))
    retained = store.get(claim.record.job_id)
    assert retained is not None and not retained.provider_entered and not retained.workspace_path
    assert not (tmp_path / "workspaces" / claim.record.job_id / claim.record.execution_incarnation).exists()


@pytest.mark.parametrize("admit", [admitted, admitted_conflict])
def test_moving_base_branch_prepares_exact_retained_commit(tmp_path: Path, _use_real_commands, admit) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admit(store, tmp_path, head, base)
    git(repo, "switch", "main")
    (repo / "base.txt").write_text("advanced base\n")
    git(repo, "commit", "-am", "advance main")
    advanced = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "main")

    prepared = PRRepairWorkspaceProducer(store, tmp_path / "workspaces").prepare(claim, PRRepairSource(repo))

    assert advanced != base
    assert git(prepared.workspace, "rev-parse", "refs/remotes/authorized/pr-base") == base
    assert git(prepared.workspace, "rev-parse", "HEAD") == head
    assert (prepared.workspace / "base.txt").read_text() == "base\n"
    assert prepared.authority.base_identity == base
    retained = store.get(claim.record.job_id)
    assert retained is not None and retained.workspace_path == str(prepared.workspace)
    assert not retained.provider_entered
    assert git(repo, "rev-parse", "HEAD") == advanced


def test_unavailable_retained_base_refuses_preparation(tmp_path: Path, _use_real_commands) -> None:
    repo, head, _base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, "f" * 40)
    with pytest.raises(PRRepairWorkspaceError):
        PRRepairWorkspaceProducer(store, tmp_path / "workspaces").prepare(claim, PRRepairSource(repo))
    retained = store.get(claim.record.job_id)
    assert retained is not None and not retained.provider_entered and not retained.workspace_path
    assert not (tmp_path / "workspaces" / claim.record.job_id / claim.record.execution_incarnation).exists()


def test_reopened_store_retains_complete_pr_source_authority(tmp_path: Path, _use_real_commands) -> None:
    _repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)

    retained = LocalJobStore(store.path).get_pr_repair_claim(claim.record.job_id)
    assert retained is not None
    assert (retained.head_ref, retained.head_sha, retained.base_ref, retained.base_identity) == ("feature", head, "main", base)


def test_exact_unentered_controller_artifact_can_be_adopted_without_provider_entry(tmp_path: Path, _use_real_commands) -> None:
    repo, head, _base = repository(tmp_path)
    git(repo, "switch", "main")
    (repo / "base.txt").write_text("advanced base\n")
    git(repo, "commit", "-am", "advance base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "main")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted_conflict(store, tmp_path, head, base)
    prepared = PRRepairWorkspaceProducer(store, tmp_path / "workspaces").prepare(claim, PRRepairSource(repo))
    # The conflict executor, not the producer, creates the real merge snapshot.
    git(prepared.workspace, "config", "user.email", "test@example.com")
    git(prepared.workspace, "config", "user.name", "Test")
    subprocess.run(["git", "merge", "--no-commit", base], cwd=prepared.workspace, check=True, capture_output=True)
    manifest = PRRepairWorkspaceProducer(store, tmp_path / "workspaces").capture_result(claim, prepared, InvocationOutcome.COMPLETED, "", completion_source="controller_verified_merge")
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest.to_json(), executor_token=claim.record.runner_owner)
    assert artifact is not None
    assert store.get_execution_result_artifact(claim.record.job_id, claim.record.execution_incarnation) == artifact
    assert store.adopt_controller_result(claim.record, artifact)
    retained = store.get(claim.record.job_id)
    assert retained is not None and not retained.provider_entered and retained.result_reference == artifact.artifact_id


def _conflict_checkpoint(tmp_path: Path, *, target: int):
    repo, head, _base = repository(tmp_path)
    git(repo, "switch", "main")
    (repo / "base.txt").write_text("advanced base\n")
    git(repo, "commit", "-am", "advance base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "main")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted_conflict(store, tmp_path, head, base, target=target)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    git(prepared.workspace, "config", "user.email", "test@example.com")
    git(prepared.workspace, "config", "user.name", "Test")
    subprocess.run(["git", "merge", "--no-commit", base], cwd=prepared.workspace, check=True, capture_output=True)
    manifest = producer.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "", completion_source="controller_verified_merge")
    return repo, store, claim, producer, prepared, manifest


def test_restart_recovers_exact_unentered_conflict_artifact_without_repeating_work(tmp_path: Path, _use_real_commands) -> None:
    repo, store, claim, _producer, prepared, manifest = _conflict_checkpoint(tmp_path, target=9)
    peer = tmp_path / "peer"
    peer.mkdir()
    (peer / "sentinel").write_text("unchanged\n")
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest.to_json(), executor_token=claim.record.runner_owner)
    assert artifact is not None  # interruption occurs before record_result
    merge_calls = 0

    class RecoveryAdapter:
        def validate_controller_completion(self, job, candidate):  # type: ignore[no-untyped-def]
            assert job.execution_incarnation == claim.record.execution_incarnation
            assert candidate.artifact_id == artifact.artifact_id
            return PRRepairWorkspaceProducer(reopened, tmp_path / "workspaces").revalidate_result(job, candidate)

        def authorize_provider_entry(self, _job):  # type: ignore[no-untyped-def]
            raise AssertionError("recovery must not authorize a new provider")

        def invoke(self, _job):  # type: ignore[no-untyped-def]
            nonlocal merge_calls
            merge_calls += 1
            raise AssertionError("recovery must not repeat conflict work")

    reopened = LocalJobStore(store.path)
    runner = LocalJobRunner(
        reopened,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.PR_MERGE_CONFLICT: RecoveryAdapter()},
    )
    runner.recover_unsettled()
    runner.close()

    recovered = reopened.get(claim.record.job_id)
    assert recovered is not None
    assert recovered.execution_incarnation == claim.record.execution_incarnation
    assert recovered.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert recovered.result_reference == artifact.artifact_id
    assert reopened.get_execution_result_artifact(recovered.job_id, recovered.execution_incarnation) == artifact
    assert manifest.head_sha == git(prepared.workspace, "rev-parse", "HEAD")
    assert dict(manifest.workspace_files)["base.txt"].checksum != dict(prepared.source_files)["base.txt"].checksum
    assert merge_calls == 0
    assert (repo / "base.txt").read_text() == "advanced base\n"
    assert (peer / "sentinel").read_text() == "unchanged\n"


def test_restart_before_artifact_releases_unentered_claim_but_invalid_evidence_is_retained(tmp_path: Path, _use_real_commands, monkeypatch) -> None:
    _repo, store, claim, producer, _prepared, manifest = _conflict_checkpoint(tmp_path, target=10)
    reopened = LocalJobStore(store.path)

    class RejectingAdapter:
        def validate_controller_completion(self, job, artifact):  # type: ignore[no-untyped-def]
            try:
                producer.revalidate_result(job, artifact)
            except PRRepairWorkspaceError:
                return False
            return True

        def authorize_provider_entry(self, _job):  # type: ignore[no-untyped-def]
            return False

        def invoke(self, _job):  # type: ignore[no-untyped-def]
            raise AssertionError("not invoked")

    runner = LocalJobRunner(reopened, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.PR_MERGE_CONFLICT: RejectingAdapter()})
    runner.recover_unsettled()
    released = reopened.get(claim.record.job_id)
    assert released is not None and released.state is LocalJobState.PENDING and released.execution_incarnation == ""
    runner.close()

    # A second execution with a present but invalid artifact remains attributable.
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE local_jobs SET state='running', execution_incarnation=?, runner_owner=? WHERE job_id=?", (claim.record.execution_incarnation, claim.record.runner_owner, claim.record.job_id))
    restarted_claim = replace(claim, record=replace(claim.record, state=LocalJobState.RUNNING))
    invalid = store.persist_result_artifact(restarted_claim, InvocationOutcome.COMPLETED, replace(manifest, head_sha="0" * 40).to_json(), executor_token=claim.record.runner_owner)
    assert invalid is not None
    runner = LocalJobRunner(reopened, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.PR_MERGE_CONFLICT: RejectingAdapter()})
    runner.recover_unsettled()
    retained = reopened.get(claim.record.job_id)
    assert retained is not None and retained.state is LocalJobState.RUNNING

    monkeypatch.setattr(reopened, "get_execution_result_artifact", lambda *_args: (_ for _ in ()).throw(RuntimeError("unreadable")))
    runner.recover_unsettled()
    assert reopened.get(claim.record.job_id).state is LocalJobState.RUNNING  # type: ignore[union-attr]
    runner.close()


def test_pre_ref_provenance_claim_migration_is_idempotent_and_fail_closed(tmp_path: Path) -> None:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    old_claim = admitted(store, tmp_path, "a" * 40, "b" * 40, target=11)
    old_artifact = store.persist_result_artifact(old_claim, InvocationOutcome.INTERRUPTED, "legacy-output", executor_token="runner")
    assert old_artifact is not None

    issue_offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 12, "legacy-issue", "codex", "implement")
    identity = IssueAttemptIdentity("owner", "repo", 12, "legacy-issue")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    assert guard.reserve(identity, CandidateHandoff("codex", "local")).admitted
    assert store.offer_issue(issue_offer, identity, guard) is not None

    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE old_claims AS SELECT job_id, repository, pr_number, head_sha, base_identity, repair_kind, " "source_evidence_identity, upstream_incarnation, failed_check_identities_json, created_at FROM local_pr_repair_claims")
        connection.execute("DROP TABLE local_pr_repair_claims")
        connection.execute("ALTER TABLE old_claims RENAME TO local_pr_repair_claims")

    migrated = LocalJobStore(store.path)
    assert migrated.get_pr_repair_claim(old_claim.record.job_id) is None
    assert migrated.get(old_claim.record.job_id) == store.get(old_claim.record.job_id)
    assert migrated.get_execution_result_artifact(old_claim.record.job_id, old_claim.record.execution_incarnation) == old_artifact
    assert migrated.get(issue_offer.job_id) is not None
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT head_sha, base_identity, head_repository, head_ref, base_repository, base_ref FROM local_pr_repair_claims WHERE job_id=?", (old_claim.record.job_id,)).fetchone()
    assert row == ("a" * 40, "b" * 40, "", "", "", "")
    assert LocalJobStore(store.path).get_pr_repair_claim(old_claim.record.job_id) is None

    new_claim = admitted(migrated, tmp_path, "c" * 40, "d" * 40, target=13)
    retained = LocalJobStore(store.path).get_pr_repair_claim(new_claim.record.job_id)
    assert retained is not None and (retained.head_ref, retained.base_ref) == ("feature", "main")


def test_revalidation_distinguishes_absent_merge_head_from_malformed_lineage(tmp_path: Path, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base, target=14)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    (prepared.workspace / "feature.txt").write_text("edited\n")
    manifest = producer.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "ACTION_SUMMARY: edit")
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest.to_json(), executor_token="runner")
    assert artifact is not None and manifest.merge_head == ""

    reopened = LocalJobStore(store.path)
    assert PRRepairWorkspaceProducer(reopened, tmp_path / "workspaces").revalidate_result(reopened.get(claim.record.job_id), artifact)  # type: ignore[arg-type]
    (prepared.workspace / ".git" / "MERGE_HEAD").write_text("not-an-object\n")
    with pytest.raises(PRRepairWorkspaceError, match="Needed a single revision|expected a commit|unknown revision"):
        PRRepairWorkspaceProducer(reopened, tmp_path / "workspaces").revalidate_result(reopened.get(claim.record.job_id), artifact)  # type: ignore[arg-type]


def test_empty_unentered_model_completion_is_indeterminate_and_not_recovered(tmp_path: Path, _use_real_commands) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted_conflict(store, tmp_path, head, base, target=15)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    with pytest.raises(PRRepairWorkspaceError, match="model result has no final output"):
        producer.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "")

    # Persist the exact counterexample as if written by the pre-fix producer.
    valid_shape = producer.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "ACTION_SUMMARY: old producer")
    empty_model = replace(valid_shape, observed_output="")
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, empty_model.to_json(), executor_token=claim.record.runner_owner)
    assert artifact is not None

    class ValidationAdapter:
        def validate_controller_completion(self, job, candidate):  # type: ignore[no-untyped-def]
            try:
                PRRepairWorkspaceProducer(reopened, tmp_path / "workspaces").revalidate_result(job, candidate)
            except PRRepairWorkspaceError:
                return False
            return True

        def authorize_provider_entry(self, _job):  # type: ignore[no-untyped-def]
            return False

        def invoke(self, _job):  # type: ignore[no-untyped-def]
            raise AssertionError("indeterminate evidence must not run or recover")

    reopened = LocalJobStore(store.path)
    with pytest.raises(PRRepairWorkspaceError, match="authority does not match"):
        producer.revalidate_result(reopened.get(claim.record.job_id), artifact)  # type: ignore[arg-type]
    runner = LocalJobRunner(reopened, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.PR_MERGE_CONFLICT: ValidationAdapter()})
    runner.recover_unsettled()
    retained = reopened.get(claim.record.job_id)
    assert retained is not None and retained.state is LocalJobState.RUNNING and not retained.result_reference
    runner.close()


@pytest.mark.parametrize("tamper", ["alter", "delete"])
def test_maintained_fixture_in_disposable_named_directory_is_retained_and_revalidated(tmp_path: Path, _use_real_commands, tamper: str) -> None:
    repo, _head, base = repository(tmp_path)
    fixture_dir = repo / "tests" / "fixtures" / "test-results"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "existing.json").write_text('{"existing":true}\n')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "tracked fixture directory")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "feature")
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base, target=16)
    producer = PRRepairWorkspaceProducer(store, tmp_path / "workspaces")
    prepared = producer.prepare(claim, PRRepairSource(repo))
    new_fixture = prepared.workspace / "tests" / "fixtures" / "test-results" / "new.json"
    contents = b'{"new":true}\n'
    new_fixture.write_bytes(contents)
    (prepared.workspace / "feature.txt").write_text("ordinary edit\n")
    manifest = producer.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "ACTION_SUMMARY: fixture")
    expected = hashlib.sha256(contents).hexdigest()
    assert dict(manifest.changed_files)["tests/fixtures/test-results/new.json"].checksum == expected  # type: ignore[union-attr]
    assert dict(manifest.workspace_files)["tests/fixtures/test-results/new.json"].checksum == expected
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, manifest.to_json(), executor_token="runner")
    assert artifact is not None

    if tamper == "alter":
        new_fixture.write_text('{"tampered":true}\n')
    else:
        new_fixture.unlink()
    with pytest.raises(PRRepairWorkspaceError, match="files were altered"):
        PRRepairWorkspaceProducer(LocalJobStore(store.path), tmp_path / "workspaces").revalidate_result(store.get(claim.record.job_id), artifact)  # type: ignore[arg-type]
