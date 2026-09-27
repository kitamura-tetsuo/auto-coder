"""Controller-owned state for a finite local backend invocation.

This module deliberately contains no provider policy.  It records facts established
by an enforcing launcher and provides the common promotion gate used by the backend
manager.  A model response cannot manufacture or alter this state.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, Optional

from .worktree_utils import LocalWorkspaceBinding


class LocalBoundaryError(RuntimeError):
    """Raised when a local invocation cannot safely start or be promoted."""


@dataclass(frozen=True)
class LocalBoundaryEvidence:
    """Immutable handoff evidence produced after the writer lifetime settles."""

    boundary_id: str
    invocation_id: str
    backend_type: str
    workspace: Path
    editable: bool
    policy_violation: bool
    writers_settled: bool
    failure: Optional[str]

    @property
    def promotable(self) -> bool:
        return not self.policy_violation and self.writers_settled and self.failure is None


@dataclass
class LocalExecutionBoundary:
    """Invocation-local authority and completion state owned by the controller."""

    binding: LocalWorkspaceBinding
    backend_type: str
    editable: bool
    boundary_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _policy_violation: bool = field(default=False, init=False, repr=False)
    _writers_settled: bool = field(default=False, init=False, repr=False)
    _failure: Optional[str] = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.binding.workspace.resolve() in {
            self.binding.caller_root.resolve(),
            self.binding.caller_git_dir.resolve(),
            self.binding.caller_common_dir.resolve(),
        }:
            raise LocalBoundaryError("local execution workspace aliases caller-owned Git state")

    def report_policy_violation(self, reason: str) -> None:
        """Make the invocation permanently ineligible for promotion."""
        detail = reason.strip() or "local execution policy was violated"
        with self._lock:
            self._policy_violation = True
            self._failure = detail

    def report_failure(self, reason: str) -> None:
        with self._lock:
            self._failure = reason.strip() or "local execution boundary failed"

    def settle_writers(self) -> None:
        """Record that the enforcing launcher reaped its complete process tree."""
        with self._lock:
            self._writers_settled = True

    def evidence(self) -> LocalBoundaryEvidence:
        with self._lock:
            return LocalBoundaryEvidence(
                boundary_id=self.boundary_id,
                invocation_id=self.binding.invocation_id,
                backend_type=self.backend_type,
                workspace=self.binding.workspace,
                editable=self.editable,
                policy_violation=self._policy_violation,
                writers_settled=self._writers_settled,
                failure=self._failure,
            )

    def require_promotable(self) -> LocalBoundaryEvidence:
        evidence = self.evidence()
        if not evidence.promotable:
            if not evidence.writers_settled:
                raise LocalBoundaryError("local backend writer lifetime is not settled")
            raise LocalBoundaryError(evidence.failure or "local backend result is not promotable")
        return evidence


_CURRENT_BOUNDARY: contextvars.ContextVar[Optional[LocalExecutionBoundary]] = contextvars.ContextVar("auto_coder_local_execution_boundary", default=None)


def get_current_local_execution_boundary() -> Optional[LocalExecutionBoundary]:
    """Return controller state for the current provider call, if local."""
    return _CURRENT_BOUNDARY.get()


@contextlib.contextmanager
def bind_local_execution_boundary(
    binding: LocalWorkspaceBinding,
    *,
    backend_type: str,
    editable: bool,
) -> Generator[LocalExecutionBoundary, None, None]:
    """Bind one local invocation; nested/reused boundaries are forbidden."""
    if _CURRENT_BOUNDARY.get() is not None:
        raise LocalBoundaryError("a local execution boundary is already active")
    boundary = LocalExecutionBoundary(binding=binding, backend_type=backend_type, editable=editable)
    token = _CURRENT_BOUNDARY.set(boundary)
    try:
        yield boundary
    finally:
        _CURRENT_BOUNDARY.reset(token)
