"""Typed boundary for effects that repair a pull request.

The adapter deliberately does not choose a provider.  Its caller must retain the
authoritative origin selected by the existing PR routing code, while the
operation continues to own its durable claims, receipts, Git work and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class RepairKind(str, Enum):
    CI_FAILURE = "ci_failure"
    MERGE_CONFLICT = "merge_conflict"


class RepairOrigin(str, Enum):
    CODEX_CLOUD = "codex-cloud"
    CLAUDE_ROUTINE = "claude-routine"
    JULES = "jules"
    LOCAL = "local"
    UNKNOWN = "unknown"


class RepairDisposition(str, Enum):
    NO_UPDATE_NEEDED = "no_update_needed"
    QUALITY_REFUSED = "quality_refused"
    REFUSED = "refused"
    DEFERRED = "deferred"
    ACCEPTED_OR_UNCERTAIN = "accepted_or_uncertain"
    FAILED = "failed"
    APPLIED = "applied"


class RepairFactState(str, Enum):
    AVAILABLE = "available"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PRRepairRequest:
    repository: str
    pr_number: int
    head_sha: str
    kind: RepairKind
    origin: RepairOrigin
    owner_identity: str = ""
    local_origin: str = ""

    def __post_init__(self) -> None:
        if not self.repository or self.pr_number <= 0 or not self.head_sha:
            raise ValueError("repository, positive PR number and head SHA are required")
        if self.origin in {RepairOrigin.CODEX_CLOUD, RepairOrigin.CLAUDE_ROUTINE, RepairOrigin.JULES} and not self.owner_identity:
            raise ValueError("cloud repairs require their existing owner identity")
        if self.origin is RepairOrigin.LOCAL and not self.local_origin:
            raise ValueError("local repairs require their existing execution origin")


@dataclass(frozen=True)
class PRRepairResult:
    disposition: RepairDisposition
    actions: tuple[str, ...] = ()
    reason: str = ""
    attempt_started: bool | None = None
    receipt_state: RepairFactState = RepairFactState.UNKNOWN
    receipt: str = ""
    resulting_head_state: RepairFactState = RepairFactState.UNKNOWN
    resulting_head: str = ""
    synchronization_state: RepairFactState = RepairFactState.UNKNOWN
    synchronized: bool = False
    retry_not_before: float | None = None
    repository: str = ""
    pr_number: int = 0
    input_head: str = ""
    base_ref: str = ""

    @property
    def stops_processing(self) -> bool:
        return self.disposition in {
            RepairDisposition.DEFERRED,
            RepairDisposition.ACCEPTED_OR_UNCERTAIN,
            RepairDisposition.APPLIED,
        }


RepairOperation = Callable[[PRRepairRequest], PRRepairResult]


def dispatch_pr_repair(request: PRRepairRequest, operation: RepairOperation) -> PRRepairResult:
    """Invoke one already-selected repair owner without adding fallback routing."""
    return operation(request)
