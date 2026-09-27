"""Truthful controller-owned evidence for a finite local backend invocation."""

from __future__ import annotations

import contextlib
import contextvars
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Generator, Optional

from .worktree_utils import LocalWorkspaceBinding


class LocalBoundaryError(RuntimeError):
    """Raised when local evidence is contradictory or incomplete."""


class EvidenceStatus(str, Enum):
    """State of a fact that requires an authoritative producer."""

    UNKNOWN = "unknown"
    ESTABLISHED = "established"
    FAILED = "failed"


class BackendOutcome(str, Enum):
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class LocalBoundaryEvidence:
    """Immutable, non-transferable snapshot of one invocation's facts."""

    boundary_id: str
    invocation_id: str
    backend_type: str
    workspace: Path
    editable: bool
    backend_outcome: BackendOutcome
    filesystem_enforcement: EvidenceStatus
    publication_enforcement: EvidenceStatus
    writer_completion: EvidenceStatus
    violation_observation: EvidenceStatus
    policy_violation: bool
    failure: Optional[str]

    @property
    def confined_result_authorized(self) -> bool:
        return (
            self.backend_outcome is BackendOutcome.SUCCEEDED
            and self.filesystem_enforcement is EvidenceStatus.ESTABLISHED
            and self.publication_enforcement is EvidenceStatus.ESTABLISHED
            and self.writer_completion is EvidenceStatus.ESTABLISHED
            and self.violation_observation is EvidenceStatus.ESTABLISHED
            and not self.policy_violation
            and self.failure is None
        )

    @property
    def promotable(self) -> bool:
        """Compatibility spelling for confined-result authorization."""
        return self.confined_result_authorized


@dataclass
class LocalExecutionBoundary:
    """Invocation-local evidence record; it does not implement enforcement."""

    binding: LocalWorkspaceBinding
    backend_type: str
    editable: bool
    boundary_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _backend_outcome: BackendOutcome = field(default=BackendOutcome.UNKNOWN, init=False, repr=False)
    _filesystem_enforcement: EvidenceStatus = field(default=EvidenceStatus.UNKNOWN, init=False, repr=False)
    _publication_enforcement: EvidenceStatus = field(default=EvidenceStatus.UNKNOWN, init=False, repr=False)
    _writer_completion: EvidenceStatus = field(default=EvidenceStatus.UNKNOWN, init=False, repr=False)
    _violation_observation: EvidenceStatus = field(default=EvidenceStatus.UNKNOWN, init=False, repr=False)
    _policy_violation: bool = field(default=False, init=False, repr=False)
    _failure: Optional[str] = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.binding.workspace.resolve() in {
            self.binding.caller_root.resolve(),
            self.binding.caller_git_dir.resolve(),
            self.binding.caller_common_dir.resolve(),
        }:
            raise LocalBoundaryError("local execution workspace aliases caller-owned Git state")

    def _accept(self, invocation_id: str) -> None:
        if invocation_id != self.binding.invocation_id:
            raise LocalBoundaryError("evidence belongs to a different local invocation")
        if self._closed:
            raise LocalBoundaryError("local invocation evidence is closed")

    def record_backend_success(self, invocation_id: str) -> None:
        with self._lock:
            self._accept(invocation_id)
            if self._backend_outcome is not BackendOutcome.FAILED:
                self._backend_outcome = BackendOutcome.SUCCEEDED

    def record_backend_failure(self, invocation_id: str, reason: str) -> None:
        with self._lock:
            self._accept(invocation_id)
            self._backend_outcome = BackendOutcome.FAILED
            self._failure = self._failure or reason.strip() or "local backend execution failed"

    def record_enforcement(
        self,
        invocation_id: str,
        *,
        filesystem: EvidenceStatus,
        publication: EvidenceStatus,
        writers: EvidenceStatus,
        violations_observed: EvidenceStatus,
    ) -> None:
        """Consume facts from future trusted runtime producers for this invocation."""
        with self._lock:
            self._accept(invocation_id)
            self._filesystem_enforcement = filesystem
            self._publication_enforcement = publication
            self._writer_completion = writers
            self._violation_observation = violations_observed
            if EvidenceStatus.FAILED in (filesystem, publication, writers, violations_observed):
                self._failure = self._failure or "local execution enforcement failed"

    def record_writer_completion(self, invocation_id: str) -> None:
        """Accept positive settlement only from an invocation supervisor."""
        with self._lock:
            self._accept(invocation_id)
            self._writer_completion = EvidenceStatus.ESTABLISHED

    def report_policy_violation(self, invocation_id: str, reason: str) -> None:
        with self._lock:
            self._accept(invocation_id)
            self._policy_violation = True
            self._violation_observation = EvidenceStatus.FAILED
            self._failure = self._failure or reason.strip() or "local execution policy was violated"

    def close(self) -> LocalBoundaryEvidence:
        with self._lock:
            self._closed = True
            return self._evidence_unlocked()

    def _evidence_unlocked(self) -> LocalBoundaryEvidence:
        return LocalBoundaryEvidence(
            boundary_id=self.boundary_id,
            invocation_id=self.binding.invocation_id,
            backend_type=self.backend_type,
            workspace=self.binding.workspace,
            editable=self.editable,
            backend_outcome=self._backend_outcome,
            filesystem_enforcement=self._filesystem_enforcement,
            publication_enforcement=self._publication_enforcement,
            writer_completion=self._writer_completion,
            violation_observation=self._violation_observation,
            policy_violation=self._policy_violation,
            failure=self._failure,
        )

    def evidence(self) -> LocalBoundaryEvidence:
        with self._lock:
            return self._evidence_unlocked()

    def require_confined_result(self, invocation_id: str, *, for_edit_promotion: bool = False) -> LocalBoundaryEvidence:
        with self._lock:
            if invocation_id != self.binding.invocation_id:
                raise LocalBoundaryError("evidence belongs to a different local invocation")
            evidence = self._evidence_unlocked()
        if for_edit_promotion and not evidence.editable:
            raise LocalBoundaryError("no-edit evidence cannot authorize edit promotion")
        if not evidence.confined_result_authorized:
            raise LocalBoundaryError("local execution lacks complete confined-result evidence")
        return evidence

    def require_promotable(self) -> LocalBoundaryEvidence:
        return self.require_confined_result(self.binding.invocation_id)


_CURRENT_BOUNDARY: contextvars.ContextVar[Optional[LocalExecutionBoundary]] = contextvars.ContextVar("auto_coder_local_execution_boundary", default=None)


def get_current_local_execution_boundary() -> Optional[LocalExecutionBoundary]:
    return _CURRENT_BOUNDARY.get()


@contextlib.contextmanager
def bind_local_execution_boundary(binding: LocalWorkspaceBinding, *, backend_type: str, editable: bool) -> Generator[LocalExecutionBoundary, None, None]:
    if _CURRENT_BOUNDARY.get() is not None:
        raise LocalBoundaryError("a local execution boundary is already active")
    boundary = LocalExecutionBoundary(binding=binding, backend_type=backend_type, editable=editable)
    token = _CURRENT_BOUNDARY.set(boundary)
    try:
        yield boundary
    finally:
        boundary.close()
        _CURRENT_BOUNDARY.reset(token)
