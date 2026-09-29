"""Generation-safe admission for retained local provider sessions."""

from __future__ import annotations

import threading
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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def authorize_reuse(self, decision: LiveRootReuseDecision) -> None:
        """Attach an exact, generation-specific decision; never infer one."""
        with self._lock:
            if self.disposed:
                raise LocalContinuationError("retained local workspace has been disposed")
            if decision.invocation_id != self.binding.invocation_id or decision.workspace != str(self.binding.workspace.resolve()) or decision.predecessor_turn_id != self.predecessor.turn_id:
                raise LocalContinuationError("live-root reuse decision does not match the retained generation")
            self.reuse_decision = decision

    def admit(self, *, backend_name: str, session_id: str, caller_identity: str) -> LocalWorkspaceBinding:
        """Atomically consume positive predecessor evidence for one new turn."""
        with self._lock:
            if self.disposed or not self.binding.workspace.is_dir():
                raise LocalContinuationError("retained local workspace is no longer available")
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

    def fail(self) -> None:
        with self._lock:
            self.active_turn_id = None

    def dispose(self) -> None:
        with self._lock:
            self.disposed = True
            self.reuse_decision = None
        self.binding.ownership.release_session()
