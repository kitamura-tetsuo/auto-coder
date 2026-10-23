"""Bounded orchestration for resuming durable pull-request review work.

The service deliberately owns only the pre-admission and saved-result phases.  The
caller continues to own reviewer admission, its :class:`PRReviewRuntime`, final
gates, and merge effects.  Durable state and side effects are supplied as typed
ports so this module does not import the (much larger) PR processor or replace any
authoritative producer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .accepted_finding_bridge import AcceptedFindingProjection
from .automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from .ordinary_closure_evidence import ClosureEvidenceRecord


class ActionSink(Protocol):
    """The action carrier used by PR coordination, including deferral facts."""

    quota_deferred: bool
    retry_not_before: Optional[float]

    def append(self, value: str) -> None: ...


@dataclass(frozen=True)
class RetainedClosureResumption:
    """Outcome of reconciling retained closure evidence without a model call."""

    ordinary_pass_retained: bool = False
    retained_record: Optional[ClosureEvidenceRecord] = None
    revalidate: bool = False
    unavailable: bool = False
    reason: str = ""


@dataclass(frozen=True)
class SavedReviewAssessment:
    """Whether a saved same-head review headline can be consumed unchanged."""

    action: str = "REUSE"
    reason: str = ""
    projection: Optional[AcceptedFindingProjection] = None
    recovery_key: str = ""


@dataclass(frozen=True)
class SavedReviewRequest:
    """Target and caller-owned carriers for one synchronous resumption phase."""

    github_client: object
    repository: str
    pr_data: Mapping[str, object]
    head_sha: str
    force: bool
    config: AutomationConfig
    actions: Any
    processing_status: Optional[ProcessedPRResult] = None


@dataclass(frozen=True)
class SavedReviewResumeResult:
    """Pre-admission result; it is never merge authorization."""

    retained_closure: Optional[RetainedClosureResumption] = None
    stop_processing: bool = False


@dataclass(frozen=True)
class SavedReviewReplayResult:
    """Saved non-pass feedback plus the explicit independent-review signal."""

    feedback: Sequence[str] = ()
    requires_reassessment: bool = False


class SavedReviewPorts(Protocol):
    """Late-bound access to existing authoritative producers and effects."""

    def resolve_two_tier(self, github_client: object, repository: str, pr_data: Mapping[str, object]) -> Any: ...

    def resume_retained_closure(self, repository: str, pr_number: int, head_sha: str, inputs: Any, github_client: object) -> RetainedClosureResumption: ...

    def resume_ordinary_correction(self, request: SavedReviewRequest, inputs: Any) -> bool: ...

    def repair_strong_findings(self, request: SavedReviewRequest, inputs: Any) -> bool: ...

    def record_identity_deferral(self, pr_number: int, head_sha: str, reason: str) -> None: ...

    def record_closure_deferral(self, pr_number: int, head_sha: str, reason: str) -> None: ...

    def assess_saved(self, request: SavedReviewRequest, saved_status: Optional[str], claimed_review_threads: Sequence[object]) -> SavedReviewAssessment: ...

    def replay_saved(self, request: SavedReviewRequest, report: str, projection: Optional[AcceptedFindingProjection]) -> SavedReviewReplayResult: ...


class SavedReviewResumptionService:
    """Synchronously resume saved review work without acquiring merge authority."""

    def __init__(self, ports: SavedReviewPorts, *, clock: Callable[[], float] = time.time) -> None:
        self._ports = ports
        self._clock = clock

    def resume_before_admission(self, request: SavedReviewRequest) -> SavedReviewResumeResult:
        pr_number = int(str(request.pr_data["number"]))
        try:
            inputs = self._ports.resolve_two_tier(request.github_client, request.repository, request.pr_data)
        except Exception as exc:
            reason = str(exc)
            request.actions.append(f"Skipping merge for PR #{pr_number}: strong-audit identity could not be resolved: {reason}")
            self._ports.record_identity_deferral(pr_number, request.head_sha, reason)
            return SavedReviewResumeResult(stop_processing=True)

        resumption: Optional[RetainedClosureResumption] = None
        if inputs is not None:
            try:
                resumption = self._ports.resume_retained_closure(request.repository, pr_number, request.head_sha, inputs, request.github_client)
            except Exception as exc:
                resumption = RetainedClosureResumption(unavailable=True, reason=f"the owning review state could not be read: {exc}")

        if resumption is not None and resumption.unavailable and not request.force:
            request.actions.append(f"Retained closure evidence for PR #{pr_number} is pending: {resumption.reason}; " "no reviewer was admitted and the same evidence will be resumed when it is observable again")
            self._ports.record_closure_deferral(pr_number, request.head_sha, resumption.reason)
            deadline = self._clock() + 60.0
            request.actions.quota_deferred = True
            request.actions.retry_not_before = deadline
            if request.processing_status is not None:
                request.processing_status.error = None
                request.processing_status.outcome = PRProcessingOutcome.DEFERRED
                request.processing_status.retry_not_before = deadline
            return SavedReviewResumeResult(resumption, True)

        if resumption is not None and resumption.ordinary_pass_retained:
            return SavedReviewResumeResult(resumption, False)
        if self._ports.resume_ordinary_correction(request, inputs):
            return SavedReviewResumeResult(stop_processing=True)
        if inputs is not None and self._ports.repair_strong_findings(request, inputs):
            return SavedReviewResumeResult(stop_processing=True)
        return SavedReviewResumeResult(resumption, False)

    def assess(self, request: SavedReviewRequest, *, saved_status: Optional[str], claimed_review_threads: Sequence[object]) -> SavedReviewAssessment:
        return self._ports.assess_saved(request, saved_status, claimed_review_threads)

    def replay_nonpass(self, request: SavedReviewRequest, *, report: str, projection: Optional[AcceptedFindingProjection]) -> SavedReviewReplayResult:
        return self._ports.replay_saved(request, report, projection)
