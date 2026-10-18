"""Bounded execution boundary for PR base updates and conflict repairs.

The service deliberately owns no provider or Git implementation.  Those remain
with the existing producers, while this boundary carries the immutable target
identity and returns their typed facts to PR orchestration without interpreting
display actions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from .pr_repair_dispatch import PRRepairResult, RepairDisposition


@dataclass(frozen=True)
class BaseUpdateExecutionRequest:
    repository: str
    pr_number: int
    expected_head: str
    base_ref: str
    origin: str

    def __post_init__(self) -> None:
        if not self.repository or self.pr_number <= 0 or not self.base_ref:
            raise ValueError("repository, positive PR number and base ref are required")


@dataclass(frozen=True)
class BaseUpdateExecutionResult:
    repair: PRRepairResult
    actions: tuple[str, ...]
    quota_deferred: bool = False
    retry_not_before: float | None = None


class BaseUpdateConflictExecutionService:
    """Compose already-selected base-update/conflict operation producers."""

    def execute_base_update(
        self,
        request: BaseUpdateExecutionRequest,
        operation: Callable[[], BaseUpdateExecutionResult],
    ) -> BaseUpdateExecutionResult:
        produced = operation()
        repair = self._bind_target(request, produced.repair, produced.actions)
        return replace(produced, repair=repair)

    def execute_conflict_repair(
        self,
        request: BaseUpdateExecutionRequest,
        operation: Callable[[], PRRepairResult],
    ) -> PRRepairResult:
        """Execute one route selected by the caller; never introduce fallback."""
        produced = operation()
        return self._bind_target(request, produced, produced.actions)

    @staticmethod
    def _bind_target(
        request: BaseUpdateExecutionRequest,
        repair: PRRepairResult,
        actions: tuple[str, ...],
    ) -> PRRepairResult:
        # A producer may enrich these facts, but it may not silently retarget an
        # operation selected for another repository, PR, base, or observed head.
        identities = (
            (repair.repository, request.repository, "repository"),
            (repair.pr_number, request.pr_number, "PR"),
            (repair.input_head, request.expected_head, "head"),
            (repair.base_ref, request.base_ref, "base"),
        )
        mismatch = next((name for actual, expected, name in identities if actual and expected and actual != expected), "")
        if mismatch:
            reason = f"base-update producer returned a mismatched {mismatch} identity"
            return PRRepairResult(
                RepairDisposition.FAILED,
                actions,
                reason=reason,
                repository=request.repository,
                pr_number=request.pr_number,
                input_head=request.expected_head,
                base_ref=request.base_ref,
            )
        return replace(
            repair,
            actions=actions,
            repository=request.repository,
            pr_number=request.pr_number,
            input_head=request.expected_head,
            base_ref=request.base_ref,
        )
