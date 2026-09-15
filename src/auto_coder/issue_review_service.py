"""Production execution owner for the Issue Review worker lane.

``IssueReviewService`` binds the lane mechanics in
:class:`IssueReviewWorker` to the authoritative validation lifecycles:

* admission refresh recomputes routing classification from authoritative
  snapshots before any semantic review (REQ-003, REQ-012 through REQ-014);
* every exact-current identity is decided through the production
  specification or decomposition lifecycle, which enforces terminal-reuse
  authority, evidence availability, and the strict analyzer result contract
  internally (REQ-004, REQ-005, REQ-015, REQ-017 through REQ-019, REQ-022);
* BLOCKED decisions receive category-specific remediation authorization and
  publication effects with fresh-current authority, and genuine
  ``REISSUE_REQUIRED`` stops are established before handoff (REQ-006,
  REQ-016, REQ-020, REQ-021);
* completion durably requests routing reevaluation without dispatching
  implementation (REQ-007).

The service never acquires implementation slots, selects providers, creates
implementation tasks, or dispatches implementation (REQ-001). It runs
semantic review on its own ``ValidationScheduler`` instance so
implementation-slot occupancy cannot consume Review capacity (REQ-009).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from .decomposition_analyzer import DecompositionIssue
from .decomposition_validation_lifecycle import DecompositionDecision, DecompositionIdentity, DecompositionValidationLifecycle
from .issue_review_worker import FreshReviewView, IssueReviewWorker
from .issue_stage_routing import REVIEW_STAGE, IssueStageRoutingStore, PendingLaneItem
from .requirement_contract import NormativeIssueManifest
from .specification_analyzer import IndividualRelationshipContext
from .specification_validation_lifecycle import SpecificationValidationLifecycle, ValidationDecision
from .validation_scheduler import ValidationScheduler

TERMINAL_VERDICTS = ("READY", "BLOCKED")

ReviewDecision = Union[ValidationDecision, DecompositionDecision]


@dataclass(frozen=True)
class IndividualReviewDescriptor:
    """One exact-current individual validation identity with its inputs."""

    kind: str = "individual"
    number: int = 0
    title: str = ""
    body: str = ""
    role: str = "standalone"
    parent_number: Optional[int] = None
    identity_key: str = ""
    manifest: Optional[NormativeIssueManifest] = None
    relationship: Optional[IndividualRelationshipContext] = None


@dataclass(frozen=True)
class DecompositionReviewDescriptor:
    """One exact-current decomposition identity with its family inputs."""

    kind: str = "decomposition"
    parent_number: int = 0
    identity_key: str = ""
    identity: Optional[DecompositionIdentity] = None
    parent_issue: Optional[DecompositionIssue] = None
    child_issues: tuple[DecompositionIssue, ...] = ()


ReviewDescriptor = Union[IndividualReviewDescriptor, DecompositionReviewDescriptor]


@dataclass(frozen=True)
class ReconciledReviewTarget:
    """Authoritative refresh of one Review target for one lane attempt."""

    number: int
    generation: str
    descriptors: tuple[ReviewDescriptor, ...] = ()
    reevaluate: Optional[Callable[[], None]] = None


@dataclass
class LaneItemOutcome:
    """Result of one production lane attempt with its durable decisions."""

    target_number: int
    generation: str
    status: str
    decisions: dict[str, ReviewDecision] = field(default_factory=dict)
    handed_off: bool = False
    applied_identity_keys: set[str] = field(default_factory=set)


class IssueReviewService:
    """Execute Review-lane work through the production validation lifecycles."""

    def __init__(
        self,
        routing: IssueStageRoutingStore,
        scheduler: ValidationScheduler,
        repository: str,
        specification_factory: Callable[[], SpecificationValidationLifecycle],
        decomposition_factory: Callable[[], DecompositionValidationLifecycle],
        reconcile: Callable[..., Optional[ReconciledReviewTarget]],
        describe: Callable[[int], Optional[tuple[ReviewDescriptor, ...]]],
        github_provider: Callable[[], Any],
        fetch_set: Callable[[int], Optional[tuple[dict[str, object], list[dict[str, object]]]]],
        trace_job: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._worker = IssueReviewWorker(routing, scheduler)
        self._repository = repository
        self._specification_factory = specification_factory
        self._decomposition_factory = decomposition_factory
        self._reconcile = reconcile
        self._describe = describe
        self._github_provider = github_provider
        self._fetch_set = fetch_set
        self._trace_job = trace_job or (lambda _repo, _number, _stage, _label, _facts, fn: fn())

    def _spec(self) -> SpecificationValidationLifecycle:
        return self._specification_factory()

    def _decomp(self) -> DecompositionValidationLifecycle:
        return self._decomposition_factory()

    def pump(self, origin: str, max_items: int = 8) -> list[LaneItemOutcome]:
        """Run pending Review work in priority/FIFO order; never dispatches implementation."""
        outcomes: list[LaneItemOutcome] = []
        for _ in range(max(0, max_items)):
            item = self._worker.next_work(self._repository)
            if item is None:
                break
            outcomes.append(self._run_pending(item, origin))
        return outcomes

    def pump_target(self, target_number: int, origin: str, snapshot: Optional[dict[str, object]] = None) -> Optional[LaneItemOutcome]:
        """Admit and review one target through the Review lane; observe-only for callers.

        Callers pass the admission snapshot they already hold so the lane
        reviews exactly the submission the caller is gating; a concurrent
        edit is then detected by the caller's own freshness comparison
        instead of racing the lane's internal reads.
        """
        reconciled = self._reconcile(target_number, snapshot)
        if reconciled is None:
            return None
        item = self._worker._routing.get(self._repository, REVIEW_STAGE, reconciled.number)  # pylint: disable=protected-access
        if item is not None and item.generation == reconciled.generation:
            return self._run_pending(item, origin, reconciled)
        return self._recover_completed_handoff(reconciled)

    def _recover_completed_handoff(self, reconciled: ReconciledReviewTarget) -> Optional[LaneItemOutcome]:
        """Reconstruct a lost handoff from durable terminal decisions without re-review.

        After a crash between decision persistence and the routing wake, the
        pending item is gone but every enabled identity is durably terminal.
        Effects re-apply idempotently and the reevaluation wake is
        re-issued; the reviewer backend is never invoked.
        """
        decisions: dict[str, ReviewDecision] = {}
        for descriptor in reconciled.descriptors:
            stored = self._stored_terminal(descriptor)
            if stored is None:
                return None
            decisions[descriptor.identity_key] = stored
        if not decisions:
            return None
        cell = {descriptor.identity_key: descriptor for descriptor in reconciled.descriptors}
        if not self._apply_generation_effects(cell, decisions):
            return LaneItemOutcome(reconciled.number, reconciled.generation, "error", decisions, False)
        if reconciled.reevaluate is not None:
            reconciled.reevaluate()
        applied = {key for key, decision in decisions.items() if decision.verdict == "BLOCKED"}
        return LaneItemOutcome(reconciled.number, reconciled.generation, "completed", decisions, True, applied)

    def _stored_terminal(self, descriptor: ReviewDescriptor) -> Optional[ReviewDecision]:
        """Reuse a durable terminal decision only with re-established authority.

        The lifecycle re-validates evidence, baselines, and Objective anchors
        before returning the stored decision, so unavailable or inconsistent
        authority becomes retryable ERROR instead of unsafe reuse (REQ-019).
        No reviewer-backend invocation occurs for an authorized terminal.
        """
        if isinstance(descriptor, DecompositionReviewDescriptor):
            if descriptor.identity is None or descriptor.parent_issue is None:
                return None
            stored = self._decomp().store.get(descriptor.identity)
            if stored is None or stored.verdict not in TERMINAL_VERDICTS:
                return None
            decided = self._decomp().decide(descriptor.identity, descriptor.parent_issue, descriptor.child_issues)
            if not isinstance(decided, DecompositionDecision) or decided.identity.key != descriptor.identity_key:
                return None
            return decided if decided.verdict in TERMINAL_VERDICTS else None
        assert descriptor.manifest is not None
        individual_identity = self._spec().identity(descriptor.number, descriptor.title, descriptor.body, descriptor.relationship)
        individual_stored = self._spec().store.get(individual_identity)
        if individual_stored is None or individual_stored.verdict not in TERMINAL_VERDICTS:
            return None
        individual_decided = self._spec().decide(descriptor.manifest, descriptor.title, descriptor.body, descriptor.relationship)
        if not isinstance(individual_decided, ValidationDecision) or individual_decided.identity.key != descriptor.identity_key:
            return None
        return individual_decided if individual_decided.verdict in TERMINAL_VERDICTS else None

    def _run_pending(self, item: PendingLaneItem, origin: str, reconciled: Optional[ReconciledReviewTarget] = None) -> LaneItemOutcome:
        if not self._worker._routing.begin(item):  # pylint: disable=protected-access
            return LaneItemOutcome(item.target_number, item.generation, "deferred")
        if reconciled is None:
            reconciled = self._reconcile(item.target_number)
        cell: dict[str, ReviewDescriptor] = {}
        decisions: dict[str, ReviewDecision] = {}
        current = reconciled is not None and reconciled.generation == item.generation and reconciled.descriptors
        if current:
            assert reconciled is not None
            for descriptor in reconciled.descriptors:
                cell[descriptor.identity_key] = descriptor

        def refresh(_item: PendingLaneItem) -> Optional[FreshReviewView]:
            if not current or not cell:
                return None
            return FreshReviewView(True, True, tuple(cell))

        def review_identity(identity_key: str) -> str:
            decision = self._decide(cell[identity_key], origin)
            decisions[identity_key] = decision
            return decision.verdict if decision.verdict in TERMINAL_VERDICTS else "ERROR"

        def pre_handoff(_item: PendingLaneItem, _verdicts: dict[str, str]) -> bool:
            return self._apply_generation_effects(cell, decisions)

        def on_routing_request(_item: PendingLaneItem) -> None:
            if reconciled is not None and reconciled.reevaluate is not None:
                reconciled.reevaluate()

        outcome = self._worker.run_item(
            item,
            refresh=refresh,
            lookup_terminal=lambda _key: None,
            review_identity=review_identity,
            persist=lambda _key, _verdict: None,
            pre_handoff=pre_handoff,
            on_routing_request=on_routing_request,
        )
        applied = {key for key, decision in decisions.items() if decision.verdict == "BLOCKED"} if outcome.status == "completed" else set()
        return LaneItemOutcome(item.target_number, item.generation, outcome.status, decisions, outcome.handed_off, applied)

    def _decide(self, descriptor: ReviewDescriptor, origin: str) -> ReviewDecision:
        if isinstance(descriptor, DecompositionReviewDescriptor):
            assert descriptor.identity is not None and descriptor.parent_issue is not None
            return self._trace_job(
                self._repository,
                descriptor.parent_number,
                "issue.decomposition-validation-job",
                f"issue#{descriptor.parent_number} decomposition validation job",
                {"parent_number": descriptor.parent_number, "caller_origin": origin},
                lambda: self._decomp().decide(descriptor.identity, descriptor.parent_issue, descriptor.child_issues),
            )
        assert descriptor.manifest is not None
        return self._trace_job(
            self._repository,
            descriptor.number,
            "issue.individual-validation-job",
            f"issue#{descriptor.number} individual validation job",
            {"issue_number": descriptor.number, "review_kind": "individual", "validation_identity": descriptor.identity_key, "caller_origin": origin},
            lambda: self._spec().decide(descriptor.manifest, descriptor.title, descriptor.body, descriptor.relationship),
        )

    def _apply_generation_effects(self, cell: dict[str, ReviewDescriptor], decisions: dict[str, ReviewDecision]) -> bool:
        """Apply BLOCKED effects with fresh-current authority; gate genuine reissue stops.

        Effect-transport failures are contained here so the generation stays
        retryable without another semantic review; observing gates still see
        the durable BLOCKED decision and report the failure themselves.
        """
        from .logger_config import get_logger

        logger = get_logger(__name__)
        for identity_key, decision in decisions.items():
            if decision.verdict != "BLOCKED":
                continue
            descriptor = cell.get(identity_key)
            if descriptor is None:
                return False
            try:
                if isinstance(descriptor, DecompositionReviewDescriptor):
                    if not isinstance(decision, DecompositionDecision):
                        return False
                    error = self._apply_decomposition_blocked(descriptor, decision)
                else:
                    if not isinstance(decision, ValidationDecision):
                        return False
                    error = self._apply_individual_blocked(descriptor, decision, cell, decisions)
            except Exception:
                logger.opt(exception=True).debug("Review-lane BLOCKED effects failed for identity {}; deferring", identity_key)
                return False
            if error:
                return False
            if decision.remediation == "REISSUE_REQUIRED" and not self._reissue_stop_established(descriptor):
                return False
        return True

    def _apply_individual_blocked(
        self,
        descriptor: IndividualReviewDescriptor,
        decision: ValidationDecision,
        cell: dict[str, ReviewDescriptor],
        decisions: dict[str, ReviewDecision],
    ) -> Optional[str]:
        lifecycle = self._spec()
        github = self._github_provider()

        def is_current() -> bool:
            return self._individual_is_current(descriptor, decision, cell, decisions)

        lifecycle.authorize_automatic_repair(decision, is_current, lambda: None)
        if descriptor.role == "child" and descriptor.parent_number is not None:
            return lifecycle.apply_inherited_blocked(github, decision, is_current)
        return lifecycle.apply_blocked(github, decision, is_current)

    def _apply_decomposition_blocked(
        self,
        descriptor: DecompositionReviewDescriptor,
        decision: DecompositionDecision,
    ) -> Optional[str]:
        lifecycle = self._decomp()
        lifecycle.authorize_automatic_repair(decision, lambda: self._decomposition_is_current(descriptor, decision), lambda: None)
        return lifecycle.apply_blocked(self._github_provider(), decision, self._fetch_set)

    def _individual_is_current(
        self,
        descriptor: IndividualReviewDescriptor,
        decision: ValidationDecision,
        cell: dict[str, ReviewDescriptor],
        decisions: dict[str, ReviewDecision],
    ) -> bool:
        """Re-establish exact-current authority from a fresh reconciled target."""
        anchor = descriptor.parent_number if descriptor.role == "child" and descriptor.parent_number is not None else descriptor.number
        described = self._describe(anchor)
        if described is None:
            return False
        current_keys = {item.identity_key for item in described}
        if descriptor.identity_key not in current_keys:
            return False
        if descriptor.role != "child":
            return True
        for item in described:
            if not isinstance(item, IndividualReviewDescriptor):
                continue
            sibling = decisions.get(item.identity_key)
            if not isinstance(sibling, ValidationDecision) or sibling.identity.key != item.identity_key:
                return False
        stored = self._spec().store.get(decision.identity)
        return stored is not None and stored.verdict == "BLOCKED"

    def _decomposition_is_current(self, descriptor: DecompositionReviewDescriptor, decision: DecompositionDecision) -> bool:
        fetched = self._fetch_set(descriptor.parent_number)
        return fetched is not None and self._decomp().identity(fetched[0], fetched[1]) == decision.identity

    def _reissue_stop_established(self, descriptor: ReviewDescriptor) -> bool:
        if isinstance(descriptor, DecompositionReviewDescriptor):
            return self._decomp().is_reissue_required(descriptor.parent_number)
        assert isinstance(descriptor, IndividualReviewDescriptor)
        return self._spec().is_reissue_required(descriptor.number)


def build_individual_descriptor(
    lifecycle: SpecificationValidationLifecycle,
    number: int,
    title: str,
    body: str,
    manifest: NormativeIssueManifest,
    relationship: Optional[IndividualRelationshipContext] = None,
    role: str = "standalone",
    parent_number: Optional[int] = None,
) -> IndividualReviewDescriptor:
    """Build an individual descriptor whose key matches the lifecycle identity."""
    return IndividualReviewDescriptor(
        number=number,
        title=title,
        body=body,
        role=role,
        parent_number=parent_number,
        identity_key=lifecycle.identity(number, title, body, relationship).key,
        manifest=manifest,
        relationship=relationship,
    )


def build_decomposition_descriptor(
    lifecycle: DecompositionValidationLifecycle,
    parent_number: int,
    parent: dict[str, object],
    children: list[dict[str, object]],
    parent_issue: DecompositionIssue,
    child_issues: list[DecompositionIssue],
) -> DecompositionReviewDescriptor:
    """Build a decomposition descriptor whose key matches the lifecycle identity."""
    identity = lifecycle.identity(parent, children)
    return DecompositionReviewDescriptor(
        parent_number=parent_number,
        identity_key=identity.key,
        identity=identity,
        parent_issue=parent_issue,
        child_issues=tuple(child_issues),
    )
