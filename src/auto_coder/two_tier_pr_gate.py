"""Production-facing authorization boundary for two-tier PR review.

This small adapter deliberately keeps GitHub/model transports outside the durable
state machine.  ``pr_processor`` can feed it ordinary results and effects while
every merge origin uses the same final authorization check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .pr_review_cycle import (
    CompletionRecord,
    ContractSnapshot,
    PrReviewCycleRepository,
    RoundProvenance,
    StrongPolicyIdentity,
)


@dataclass(frozen=True)
class TwoTierGateDiagnostic:
    phase: str
    backend: str
    audited_head: str
    current_head: str
    waiting_reason: str
    outstanding_finding_ids: Tuple[str, ...]
    completion_basis: str


class TwoTierPrGate:
    """Record ordinary convergence and fail closed at the merge boundary."""

    def __init__(self, repository: str, state: Optional[PrReviewCycleRepository] = None) -> None:
        self.repository = repository
        self.state = state or PrReviewCycleRepository(repository)

    def ordinary_pass(
        self,
        pr_number: int,
        head_sha: str,
        base_sha: str,
        contract: ContractSnapshot,
    ) -> None:
        self.state.record_ordinary_pass(
            pr_number,
            RoundProvenance(head_sha=head_sha, base_sha=base_sha),
            contract,
        )

    def authorize_merge(
        self,
        pr_number: int,
        *,
        current_head_sha: str,
        current_base_sha: str,
        current_contract: ContractSnapshot,
        current_policy: StrongPolicyIdentity,
    ) -> bool:
        """Confirm all snapshot identities immediately before a merge mutation."""
        return (
            self.reusable_completion(
                pr_number,
                current_head_sha=current_head_sha,
                current_base_sha=current_base_sha,
                current_contract=current_contract,
                current_policy=current_policy,
            )
            is not None
        )

    def reusable_completion(
        self,
        pr_number: int,
        *,
        current_head_sha: str,
        current_base_sha: str,
        current_contract: ContractSnapshot,
        current_policy: StrongPolicyIdentity,
    ) -> Optional[CompletionRecord]:
        """Return completion that satisfies the current strong-audit target."""
        snapshot = self.state.snapshot(pr_number)
        completion = snapshot.completion
        if not (
            completion
            and not snapshot.closed
            and snapshot.active_claim is None
            and not snapshot.requires_new_strong_round
            and completion.open_epoch == snapshot.open_epoch
            and completion.head_sha == current_head_sha
            and completion.base_sha == current_base_sha
            and completion.contract_identity == current_contract.identity
            and completion.policy_identity == current_policy.identity
            and not snapshot.open_findings
        ):
            return None
        return completion

    def diagnostic(self, pr_number: int, *, current_head_sha: str, backend: str) -> TwoTierGateDiagnostic:
        snapshot = self.state.snapshot(pr_number)
        strong_round = snapshot.accepted_strong_round
        completion = snapshot.completion
        return TwoTierGateDiagnostic(
            phase=snapshot.phase,
            backend=backend,
            audited_head=strong_round.head_sha if strong_round else "",
            current_head=current_head_sha,
            waiting_reason=snapshot.waiting_reason,
            outstanding_finding_ids=tuple(item.finding_id for item in snapshot.open_findings),
            completion_basis=completion.basis if completion else "",
        )
