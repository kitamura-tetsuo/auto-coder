import os
from pathlib import Path

import pytest

from src.auto_coder.local_execution_boundary import BackendOutcome, EvidenceStatus, LocalBoundaryEvidence
from src.auto_coder.local_session_continuation import LiveRootReuseDecision, LocalContinuationError, LocalResultLifecycleAuthority, RetainedLocalSession
from src.auto_coder.worktree_utils import LocalWorkspaceBinding, LocalWorkspaceOwnership


def _session(tmp_path: Path, *, writers: EvidenceStatus = EvidenceStatus.ESTABLISHED) -> RetainedLocalSession:
    workspace = tmp_path / "private"
    workspace.mkdir(parents=True)
    binding = LocalWorkspaceBinding(
        invocation_id="inv-1",
        caller_root=tmp_path / "caller",
        caller_git_dir=tmp_path / "caller/.git",
        caller_common_dir=tmp_path / "caller/.git",
        initial_head="refs/heads/main",
        initial_commit="abc",
        index_checksum="index",
        file_snapshot_checksum="checkpoint-0",
        workspace=workspace,
        ownership=LocalWorkspaceOwnership(),
    )
    evidence = LocalBoundaryEvidence(
        boundary_id="boundary-1",
        turn_id="turn-1",
        invocation_id="inv-1",
        backend_type="opencode",
        workspace=workspace,
        editable=True,
        provider_session_id="session-1",
        backend_outcome=BackendOutcome.SUCCEEDED,
        filesystem_enforcement=EvidenceStatus.ESTABLISHED,
        publication_enforcement=EvidenceStatus.ESTABLISHED,
        writer_completion=writers,
        violation_observation=EvidenceStatus.ESTABLISHED,
        policy_violation=False,
        failure=None,
    )
    identity = (workspace.stat().st_dev, workspace.stat().st_ino)
    return RetainedLocalSession("opencode", "session-1", binding, evidence, str(tmp_path / "caller"), workspace_identity=identity, workspace_fd=os.open(workspace, os.O_RDONLY))


def _decision(session: RetainedLocalSession, *, allowed: bool = True, turn: str = "turn-1") -> LiveRootReuseDecision:
    return LiveRootReuseDecision("inv-1", str(session.binding.workspace.resolve()), turn, session.binding.file_snapshot_checksum, allowed, "decision-1")


def _authority(session: RetainedLocalSession) -> LocalResultLifecycleAuthority:
    authority = LocalResultLifecycleAuthority()
    authority.record_generation(session.predecessor)
    return authority


def test_continuation_requires_positive_exact_generation_permission(tmp_path: Path) -> None:
    session = _session(tmp_path)
    authority = _authority(session)

    with pytest.raises(LocalContinuationError, match="positive live-root reuse"):
        session.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "caller"), caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=authority)

    session.authorize_reuse(_decision(session, allowed=False))
    with pytest.raises(LocalContinuationError, match="positive live-root reuse"):
        session.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "caller"), caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=authority)


def test_stale_decision_and_unsettled_writers_cannot_submit(tmp_path: Path) -> None:
    session = _session(tmp_path)
    with pytest.raises(LocalContinuationError, match="does not match"):
        session.authorize_reuse(_decision(session, turn="another-turn"))
    wrong_checkpoint = LiveRootReuseDecision("inv-1", str(session.binding.workspace.resolve()), "turn-1", "unrelated", True, "decision-2")
    with pytest.raises(LocalContinuationError, match="wrong caller checkpoint"):
        session.authorize_reuse(wrong_checkpoint)

    unsettled = _session(tmp_path / "other", writers=EvidenceStatus.UNKNOWN)
    unsettled.authorize_reuse(_decision(unsettled))
    with pytest.raises(LocalContinuationError, match="writers"):
        unsettled.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "other/caller"), caller_checkpoint=unsettled.binding.file_snapshot_checksum, lifecycle=_authority(unsettled))


def test_exact_binding_admits_once_and_current_turn_replaces_predecessor(tmp_path: Path) -> None:
    session = _session(tmp_path)
    authority = _authority(session)
    session.authorize_reuse(authority.authorize_reuse(session, caller_checkpoint=session.binding.file_snapshot_checksum))
    assert session.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "caller"), caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=authority) is session.binding
    with pytest.raises(LocalContinuationError, match="already active"):
        session.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "caller"), caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=authority)

    current = LocalBoundaryEvidence(**{**session.predecessor.__dict__, "boundary_id": "boundary-2", "turn_id": "turn-2"})
    session.finish(current)
    assert session.predecessor.turn_id == "turn-2"
    with pytest.raises(LocalContinuationError, match="positive live-root reuse"):
        session.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "caller"), caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=authority)


def test_same_path_recreated_while_retained_is_not_the_binding(tmp_path: Path) -> None:
    session = _session(tmp_path)
    authority = _authority(session)
    session.authorize_reuse(authority.authorize_reuse(session, caller_checkpoint=session.binding.file_snapshot_checksum))
    session.binding.workspace.rmdir()
    session.binding.workspace.mkdir()
    with pytest.raises(LocalContinuationError, match="identity changed"):
        session.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "caller"), caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=authority)


def test_caller_constructed_decision_lacks_lifecycle_authority(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.authorize_reuse(_decision(session))
    with pytest.raises(LocalContinuationError, match="lacks lifecycle authority"):
        session.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "caller"), caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=_authority(session))


def test_active_turn_cannot_be_disposed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    authority = _authority(session)
    session.authorize_reuse(authority.authorize_reuse(session, caller_checkpoint=session.binding.file_snapshot_checksum))
    session.admit(backend_name="opencode", session_id="session-1", caller_identity=str(tmp_path / "caller"), caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=authority)
    with pytest.raises(LocalContinuationError, match="turn is active"):
        session.dispose()


def test_failed_retained_turn_keeps_current_unsettled_writer_evidence(tmp_path: Path) -> None:
    from dataclasses import replace

    session = _session(tmp_path)
    session.binding.ownership.retain_session()
    session.binding.ownership.release_execution()
    session.binding.ownership.release_handoff()
    deleted = []
    session.binding.ownership.retain_until_released(lambda: deleted.append(session.binding.workspace))
    session.binding.ownership.begin_execution()
    failure = replace(session.predecessor, turn_id="failed-current-turn", backend_outcome=BackendOutcome.FAILED, writer_completion=EvidenceStatus.UNKNOWN)
    session.fail(failure)
    assert session.predecessor == failure
    assert session.active_turn_id is None
    assert session.binding.ownership.execution_released is False
    session.dispose()
    assert deleted == []
    assert session.binding.workspace.is_dir()
    session.binding.ownership.release_execution()
    assert deleted == [session.binding.workspace]
    assert session.binding.workspace.is_dir()
