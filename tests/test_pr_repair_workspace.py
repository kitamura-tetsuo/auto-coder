from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore, PRRepairAdmission
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.local_review_repair import LocalReviewRepairStore
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
    manifest = producer.capture_result(claim, prepared, InvocationOutcome.COMPLETED, "ACTION_SUMMARY: repaired")
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
    executable.write_text("tampered\n")
    with pytest.raises(PRRepairWorkspaceError, match="files were altered"):
        producer.revalidate_result(store.get(claim.record.job_id), artifact)  # type: ignore[arg-type]


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


@pytest.mark.parametrize("advanced_ref", ["feature", "main"])
def test_ref_advancement_refuses_preparation_before_provider_entry(tmp_path: Path, _use_real_commands, advanced_ref: str) -> None:
    repo, head, base = repository(tmp_path)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    claim = admitted(store, tmp_path, head, base)
    git(repo, "switch", advanced_ref)
    (repo / "advance.txt").write_text(advanced_ref)
    git(repo, "add", ".")
    git(repo, "commit", "-m", f"advance {advanced_ref}")
    git(repo, "push", "origin", advanced_ref)

    with pytest.raises(PRRepairWorkspaceError, match="no longer resolves"):
        PRRepairWorkspaceProducer(store, tmp_path / "workspaces").prepare(claim, PRRepairSource(repo))
    retained = store.get(claim.record.job_id)
    assert retained is not None and not retained.provider_entered and not retained.workspace_path


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
