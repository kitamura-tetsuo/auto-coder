from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobStore, PRRepairAdmission
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
