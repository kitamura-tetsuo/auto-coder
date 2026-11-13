"""Codex completion policy is explicit and never fabricates descendant evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from src.auto_coder.backend_manager import _dispose_local_sessions
from src.auto_coder.local_execution_boundary import EvidenceStatus, LocalBoundaryError, LocalExecutionBoundary
from src.auto_coder.local_session_continuation import LocalContinuationError
from tests.test_local_execution_boundary import _binding
from tests.test_local_session_continuation import _authority, _session


@pytest.mark.parametrize("editable", [False, True])
def test_codex_direct_completion_allows_result_without_descendant_claim(tmp_path: Path, editable: bool) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "codex", editable=editable)
    invocation = boundary.binding.invocation_id
    boundary.record_backend_success(invocation)
    boundary.record_filesystem_enforcement(invocation, EvidenceStatus.ESTABLISHED)
    boundary.record_violation_observation(invocation)
    assert not boundary.evidence().lifecycle_complete
    assert not boundary.evidence().confined_result_authorized
    boundary.record_direct_process_completion(invocation)
    evidence = boundary.close()
    assert evidence.direct_process_completion is EvidenceStatus.ESTABLISHED
    assert evidence.writer_completion is EvidenceStatus.UNKNOWN
    assert evidence.lifecycle_complete
    assert evidence.confined_result_authorized
    assert evidence.handoff_authorized is editable


@pytest.mark.parametrize("backend", ["opencode", "muse", "codex-cloud"])
def test_direct_completion_does_not_relax_other_backends(tmp_path: Path, backend: str) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), backend, editable=True)
    invocation = boundary.binding.invocation_id
    boundary.record_backend_success(invocation)
    boundary.record_filesystem_enforcement(invocation, EvidenceStatus.ESTABLISHED)
    boundary.record_violation_observation(invocation)
    boundary.record_direct_process_completion(invocation)
    assert not boundary.evidence().lifecycle_complete
    assert not boundary.evidence().handoff_authorized
    assert not boundary.evidence().confined_result_authorized
    boundary.record_writer_completion(invocation)
    assert boundary.evidence().lifecycle_complete
    assert boundary.evidence().handoff_authorized


def test_direct_completion_requires_matching_live_invocation(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "codex", editable=True)
    with pytest.raises(LocalBoundaryError, match="different"):
        boundary.record_direct_process_completion("other")
    assert boundary.evidence().direct_process_completion is EvidenceStatus.UNKNOWN
    boundary.close()
    with pytest.raises(LocalBoundaryError, match="closed"):
        boundary.record_direct_process_completion(boundary.binding.invocation_id)


@pytest.mark.parametrize("missing", ["backend", "filesystem", "observation"])
def test_direct_completion_does_not_replace_other_required_evidence(tmp_path: Path, missing: str) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "codex", editable=True)
    invocation = boundary.binding.invocation_id
    boundary.record_direct_process_completion(invocation)
    if missing != "backend":
        boundary.record_backend_success(invocation)
    if missing != "filesystem":
        boundary.record_filesystem_enforcement(invocation, EvidenceStatus.ESTABLISHED)
    if missing != "observation":
        boundary.record_violation_observation(invocation)
    assert boundary.evidence().lifecycle_complete
    assert not boundary.evidence().confined_result_authorized
    if missing != "filesystem":
        assert not boundary.evidence().handoff_authorized


def test_codex_continuation_requires_direct_exit_and_one_use_permission(tmp_path: Path) -> None:
    session = _session(tmp_path, writers=EvidenceStatus.UNKNOWN)
    session.backend_name = "codex"
    session.predecessor = replace(session.predecessor, backend_type="codex")
    authority = _authority(session)
    kwargs = dict(backend_name="codex", session_id=session.provider_session_id, caller_identity=session.caller_identity, caller_checkpoint=session.binding.file_snapshot_checksum, lifecycle=authority)
    with pytest.raises(LocalContinuationError, match="completion"):
        session.admit(**kwargs)
    session.predecessor = replace(session.predecessor, direct_process_completion=EvidenceStatus.ESTABLISHED)
    authority.record_generation(session.predecessor)
    with pytest.raises(LocalContinuationError, match="positive live-root reuse"):
        session.admit(**kwargs)
    session.authorize_reuse(authority.authorize_reuse(session, caller_checkpoint=session.binding.file_snapshot_checksum))
    assert session.admit(**kwargs) is session.binding
    assert session.predecessor.writer_completion is EvidenceStatus.UNKNOWN
    with pytest.raises(LocalContinuationError, match="already active"):
        session.admit(**kwargs)
    session.fail()
    session.dispose()


@pytest.mark.parametrize("backend,expected_disposed", [("codex", True), ("opencode", False)])
def test_manager_disposal_uses_backend_specific_completion(tmp_path: Path, backend: str, expected_disposed: bool) -> None:
    session = _session(tmp_path, writers=EvidenceStatus.UNKNOWN)
    session.predecessor = replace(session.predecessor, backend_type=backend, direct_process_completion=EvidenceStatus.ESTABLISHED)
    sessions = {session.provider_session_id: session}
    _dispose_local_sessions(sessions)
    assert session.disposed is expected_disposed
    assert (not sessions) is expected_disposed
    if not expected_disposed:
        session.dispose()


def test_codex_root_controller_refused_before_provider_launch(tmp_path: Path) -> None:
    from unittest.mock import patch

    from src.auto_coder.invocation_process_supervisor import InvocationLaunch, InvocationOutcome, InvocationProcessSupervisor

    request = InvocationLaunch("root-refused", "codex", "no-edit", tmp_path, (), "/bin/true")
    with patch("src.auto_coder.invocation_process_supervisor.os.geteuid", return_value=0):
        result = InvocationProcessSupervisor().run(request)
    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert result.returncode is None
    assert result.detail == "local Codex requires a non-root controller identity"
