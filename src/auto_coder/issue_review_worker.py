"""Dedicated priority-ordered Review worker lane for Issue specification review.

The worker consumes only review-needed work from
:class:`IssueStageRoutingStore` in priority/FIFO order and never acquires
implementation slots, selects providers, creates implementation tasks, or
dispatches implementation. Semantic review executes through the injected
``review_identity`` callable under an independent
:class:`ValidationScheduler` capacity boundary, so full implementation
capacity cannot starve review progress.

Durable terminal decisions (READY/BLOCKED) are persisted before the
routing/reevaluation wake, so a crash between persistence and wake is
recoverable from durable state without another semantic review. ERROR is
never persisted as terminal and stays retryable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .issue_stage_routing import REVIEW_STAGE, IssueStageRoutingStore, PendingLaneItem
from .validation_scheduler import ValidationJob, ValidationScheduler

TERMINAL_VERDICTS = ("READY", "BLOCKED")
RETRYABLE_VERDICT = "ERROR"


@dataclass(frozen=True)
class FreshReviewView:
    """Authoritative refresh of one queued review item before semantic review."""

    current: bool
    eligible: bool
    remaining_identity_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewOutcome:
    """Result of one worker step; never carries implementation dispatch."""

    target_number: int
    generation: str
    status: str  # completed | deferred | stale | error
    verdicts: dict[str, str] = field(default_factory=dict)
    backend_calls: int = 0
    handed_off: bool = False


class IssueReviewWorker:
    """Priority-ordered review producer without an implementation path."""

    def __init__(self, routing: IssueStageRoutingStore, scheduler: ValidationScheduler) -> None:
        self._routing = routing
        self._scheduler = scheduler

    def next_work(self, repository: str) -> Optional[PendingLaneItem]:
        """Return the highest-priority, oldest-arrival pending review item."""
        pending = self._routing.pending(repository, REVIEW_STAGE)
        return pending[0] if pending else None

    def recover(self, repository: str) -> None:
        """Make pre-completion review attempts retryable at the same arrival."""
        self._routing.recover(repository)

    def run_item(
        self,
        item: PendingLaneItem,
        *,
        refresh: Callable[[PendingLaneItem], Optional[FreshReviewView]],
        lookup_terminal: Callable[[str], Optional[str]],
        review_identity: Callable[[str], str],
        persist: Callable[[str, str], None],
        pre_handoff: Optional[Callable[[PendingLaneItem, dict[str, str]], bool]] = None,
        on_routing_request: Callable[[PendingLaneItem], None],
    ) -> ReviewOutcome:
        """Run one claimed review item to durable completion or deferral."""
        view = refresh(item)
        if view is None or not view.current or not view.eligible:
            self._routing.remove_generation(item.repository, REVIEW_STAGE, item.target_number, item.generation)
            return ReviewOutcome(item.target_number, item.generation, "stale")
        view = refresh(item)
        if view is None or not view.current or not view.eligible:
            self._routing.remove_generation(item.repository, REVIEW_STAGE, item.target_number, item.generation)
            return ReviewOutcome(item.target_number, item.generation, "stale")
        remaining = tuple(view.remaining_identity_keys) or tuple(item.remaining_identity_keys)
        remaining = tuple(dict.fromkeys(remaining))
        verdicts: dict[str, str] = {}
        backend_calls = 0
        jobs: dict[str, ValidationJob[str]] = {}
        for identity_key in remaining:
            stored = lookup_terminal(identity_key)
            if stored in TERMINAL_VERDICTS:
                verdicts[identity_key] = stored
                continue

            def _review(key: str = identity_key) -> str:
                return review_identity(key)

            jobs[identity_key] = self._scheduler.submit(identity_key, _review)
        for identity_key, job in jobs.items():
            try:
                verdict = job.result()
            except Exception:
                self._routing.defer(item)
                return ReviewOutcome(item.target_number, item.generation, "error", dict(verdicts), backend_calls)
            if verdict not in (*TERMINAL_VERDICTS, RETRYABLE_VERDICT):
                verdict = RETRYABLE_VERDICT
            if verdict in TERMINAL_VERDICTS:
                persist(identity_key, verdict)
                backend_calls += 1
                verdicts[identity_key] = verdict
            else:
                self._routing.defer(item)
                return ReviewOutcome(item.target_number, item.generation, "error", dict(verdicts), backend_calls)
        if pre_handoff is not None and not pre_handoff(item, dict(verdicts)):
            self._routing.defer(item)
            return ReviewOutcome(item.target_number, item.generation, "error", dict(verdicts), backend_calls)
        on_routing_request(item)
        self._routing.remove_generation(item.repository, REVIEW_STAGE, item.target_number, item.generation)
        return ReviewOutcome(item.target_number, item.generation, "completed", verdicts, backend_calls, True)

    def run_one(
        self,
        repository: str,
        *,
        refresh: Callable[[PendingLaneItem], Optional[FreshReviewView]],
        lookup_terminal: Callable[[str], Optional[str]],
        review_identity: Callable[[str], str],
        persist: Callable[[str, str], None],
        pre_handoff: Optional[Callable[[PendingLaneItem, dict[str, str]], bool]] = None,
        on_routing_request: Callable[[PendingLaneItem], None],
    ) -> Optional[ReviewOutcome]:
        """Run the next pending review item to durable completion or deferral."""
        item = self.next_work(repository)
        if item is None:
            return None
        if not self._routing.begin(item):
            return ReviewOutcome(item.target_number, item.generation, "deferred")
        return self.run_item(
            item,
            refresh=refresh,
            lookup_terminal=lookup_terminal,
            review_identity=review_identity,
            persist=persist,
            pre_handoff=pre_handoff,
            on_routing_request=on_routing_request,
        )
