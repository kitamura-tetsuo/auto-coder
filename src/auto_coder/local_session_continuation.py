"""Generation-safe admission for retained local provider sessions."""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .local_execution_boundary import EvidenceStatus, LocalBoundaryEvidence
from .worktree_utils import LocalWorkspaceBinding


class LocalContinuationError(RuntimeError):
    """Raised before provider submission when continuation is not authoritative."""


@dataclass(frozen=True)
class LiveRootReuseDecision:
    """The result-lifecycle authority's decision for one predecessor generation."""

    invocation_id: str
    workspace: str
    predecessor_turn_id: str
    caller_checkpoint: str
    allowed: bool
    decision_id: str
    authority_id: str = ""


class LocalResultLifecycleAuthority:
    """Controller-owned producer and consumer of generation reuse authority."""

    def __init__(self) -> None:
        self._authority_id = uuid.uuid4().hex
        self._decisions: dict[str, LiveRootReuseDecision] = {}
        self._generations: dict[str, LocalBoundaryEvidence] = {}
        self._lock = threading.Lock()

    def record_generation(self, evidence: LocalBoundaryEvidence) -> None:
        with self._lock:
            self._generations[evidence.turn_id] = evidence

    def authorize_reuse(self, session: "RetainedLocalSession", *, caller_checkpoint: str) -> LiveRootReuseDecision:
        """Issue one decision after the lifecycle owner has made reuse safe."""
        decision = LiveRootReuseDecision(
            invocation_id=session.binding.invocation_id,
            workspace=str(session.binding.workspace.resolve()),
            predecessor_turn_id=session.predecessor.turn_id,
            caller_checkpoint=caller_checkpoint,
            allowed=True,
            decision_id=uuid.uuid4().hex,
            authority_id=self._authority_id,
        )
        with self._lock:
            if self._generations.get(session.predecessor.turn_id) != session.predecessor:
                raise LocalContinuationError("result lifecycle does not own the predecessor generation")
            self._decisions[decision.decision_id] = decision
        return decision

    def consume(self, decision: LiveRootReuseDecision) -> bool:
        with self._lock:
            if decision.authority_id != self._authority_id or self._decisions.get(decision.decision_id) != decision:
                return False
            del self._decisions[decision.decision_id]
            return True


@dataclass
class RetainedLocalSession:
    """Controller-owned association between a provider session and private root."""

    backend_name: str
    provider_session_id: str
    binding: LocalWorkspaceBinding
    predecessor: LocalBoundaryEvidence
    caller_identity: str
    reuse_decision: Optional[LiveRootReuseDecision] = None
    active_turn_id: Optional[str] = None
    disposed: bool = False
    workspace_identity: Optional[tuple[int, int]] = None
    workspace_fd: Optional[int] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def authorize_reuse(self, decision: LiveRootReuseDecision) -> None:
        """Attach an exact, generation-specific decision; never infer one."""
        with self._lock:
            if self.disposed:
                raise LocalContinuationError("retained local workspace has been disposed")
            if decision.invocation_id != self.binding.invocation_id or decision.workspace != str(self.binding.workspace.resolve()) or decision.predecessor_turn_id != self.predecessor.turn_id:
                raise LocalContinuationError("live-root reuse decision does not match the retained generation")
            if decision.caller_checkpoint != self.binding.file_snapshot_checksum:
                raise LocalContinuationError("live-root reuse decision has the wrong caller checkpoint")
            self.reuse_decision = decision

    def admit(self, *, backend_name: str, session_id: str, caller_identity: str, lifecycle: LocalResultLifecycleAuthority) -> LocalWorkspaceBinding:
        """Atomically consume positive predecessor evidence for one new turn."""
        with self._lock:
            if self.disposed or not self.binding.workspace.is_dir():
                raise LocalContinuationError("retained local workspace is no longer available")
            workspace_stat = self.binding.workspace.stat()
            current_identity = (workspace_stat.st_dev, workspace_stat.st_ino)
            retained_identity = None if self.workspace_fd is None else os.fstat(self.workspace_fd)
            if retained_identity is not None:
                retained_identity_tuple = (retained_identity.st_dev, retained_identity.st_ino)
            else:
                retained_identity_tuple = None
            if self.workspace_identity is None or retained_identity_tuple != self.workspace_identity or current_identity != self.workspace_identity:
                raise LocalContinuationError("retained local workspace identity changed")
            if self.active_turn_id is not None:
                raise LocalContinuationError("another turn is already active for this local session")
            if backend_name != self.backend_name or session_id != self.provider_session_id or caller_identity != self.caller_identity:
                raise LocalContinuationError("provider session or controller ownership does not match the retained binding")
            if self.predecessor.writer_completion is not EvidenceStatus.ESTABLISHED:
                raise LocalContinuationError("predecessor writers are not positively settled")
            decision = self.reuse_decision
            if decision is None or not decision.allowed:
                raise LocalContinuationError("positive live-root reuse permission is required")
            if decision.invocation_id != self.binding.invocation_id or decision.workspace != str(self.binding.workspace.resolve()) or decision.predecessor_turn_id != self.predecessor.turn_id:
                raise LocalContinuationError("live-root reuse permission is stale")
            if decision.caller_checkpoint != self.binding.file_snapshot_checksum:
                raise LocalContinuationError("live-root reuse permission has a stale caller checkpoint")
            if not lifecycle.consume(decision):
                raise LocalContinuationError("live-root reuse permission lacks lifecycle authority")
            self.active_turn_id = decision.decision_id
            self.reuse_decision = None
            return self.binding

    def finish(self, evidence: LocalBoundaryEvidence) -> None:
        with self._lock:
            if self.active_turn_id is None:
                raise LocalContinuationError("no admitted continuation turn is active")
            if evidence.invocation_id != self.binding.invocation_id or evidence.provider_session_id != self.provider_session_id:
                raise LocalContinuationError("continued turn evidence does not match its retained session")
            self.predecessor = evidence
            self.active_turn_id = None

    def advance_binding(self, binding: LocalWorkspaceBinding) -> None:
        """Install the controller-produced post-handoff checkpoint."""
        with self._lock:
            if binding.invocation_id != self.binding.invocation_id or binding.workspace.resolve() != self.binding.workspace.resolve():
                raise LocalContinuationError("advanced checkpoint changed the retained binding identity")
            self.binding = binding

    def fail(self) -> None:
        with self._lock:
            self.active_turn_id = None

    def dispose(self) -> None:
        with self._lock:
            if self.active_turn_id is not None:
                raise LocalContinuationError("cannot release a retained session while its turn is active")
            self.disposed = True
            self.reuse_decision = None
            if self.workspace_fd is not None:
                os.close(self.workspace_fd)
                self.workspace_fd = None
        self.binding.ownership.release_session()
