from __future__ import annotations

import shutil
from pathlib import Path

from auto_coder.issue_job_finalizer import IssueJobFinalizer
from auto_coder.local_job_handoff import WorkspaceReclamationPhase
from auto_coder.local_job_workspace_cleanup import (
    DEFAULT_RETENTION_SECONDS,
    reclaim_finalized_workspaces,
    workspace_retention_seconds,
)

from .test_issue_job_finalizer import _completed_job


def _published(tmp_path: Path, number: int = 2583):
    _source, _remote, store, slots, checkpoint = _completed_job(tmp_path, number)
    existing = {"number": 91, "head": {"ref": checkpoint.work_branch}, "body": f"Closes #{number}"}
    finalizer = IssueJobFinalizer(store, slots, lambda *_: existing, lambda *_: None)
    assert finalizer.finalize(checkpoint.job_id).disposition == "published"
    return store, checkpoint, finalizer


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
