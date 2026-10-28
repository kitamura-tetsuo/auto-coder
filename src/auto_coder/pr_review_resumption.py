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
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .accepted_finding_bridge import AcceptedFindingProjection, AcceptedFindingRecord
from .automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from .effective_decision_application import RetainedDecision
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
    discussion_changed: bool = False
    discussion_unavailable: bool = False
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

    def repair_strong_findings(self, request: SavedReviewRequest, inputs: Any) -> bool: ...

    def record_identity_deferral(self, pr_number: int, head_sha: str, reason: str) -> None: ...

    def record_closure_deferral(self, pr_number: int, head_sha: str, reason: str) -> None: ...


class SavedReviewResumptionService:
    """Synchronously resume saved review work without acquiring merge authority."""

    def __init__(self, ports: Any, *, clock: Callable[[], float] = time.time) -> None:
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

        if resumption is not None and resumption.unavailable and (not request.force or resumption.discussion_unavailable):
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

        if resumption is not None and (resumption.ordinary_pass_retained or resumption.discussion_changed):
            return SavedReviewResumeResult(resumption, False)
        if _resume_saved_ordinary_correction(
            self._ports,
            request.github_client,
            request.repository,
            ProcessedPRResult(pr_data=dict(request.pr_data)),
            inputs,
            request.actions,
            request.processing_status,
            request.config,
            force=request.force,
        ):
            return SavedReviewResumeResult(stop_processing=True)
        if inputs is not None and self._ports.repair_strong_findings(request, inputs):
            return SavedReviewResumeResult(stop_processing=True)
        return SavedReviewResumeResult(resumption, False)

    def assess(self, request: SavedReviewRequest, *, saved_status: Optional[str], claimed_review_threads: Sequence[object]) -> SavedReviewAssessment:
        return assess_saved_review_state(
            self._ports,
            request.repository,
            dict(request.pr_data),
            request.head_sha,
            saved_status,
            claimed_review_threads,
            request.force,
            request.github_client,
            request.config,
        )

    def replay_nonpass(self, request: SavedReviewRequest, *, report: str, projection: Optional[AcceptedFindingProjection]) -> SavedReviewReplayResult:
        feedback, reassess = replay_saved_nonpass_review(
            self._ports,
            request.github_client,
            request.repository,
            dict(request.pr_data),
            request.head_sha,
            report,
            projection,
            request.config,
        )
        return SavedReviewReplayResult(feedback, reassess)


# Saved-decision policy lives here; ports expose authoritative producers without transferring ownership.
def _resume_saved_ordinary_correction(ports, github_client: Any, repo_name: str, pr: ProcessedPRResult, inputs: Optional[Any], actions: List[str], processing_status: Optional[ProcessedPRResult], config: AutomationConfig, force: bool = False) -> bool:
    """Resume retained ordinary correction before reviewer admission, including force.

    The normalized producing result survives missing closure data and interrupted
    publication. Reacquired target, attempt and accepted-source authority govern
    every replay; neither a GitHub headline nor an audit capture supplies evidence.
    """
    pr_data = pr.pr_data
    pr_number = int(pr_data["number"])
    head_sha = str((pr_data.get("head") or {}).get("sha") or "")
    base_sha = str((pr_data.get("base") or {}).get("sha") or "")
    try:
        store = ports.EffectiveDecisionStore(repo_name)
        retained = store.load(pr_number)
        if retained is not None and retained.head_sha == head_sha and (retained.status == "PASS") and (retained.next_action == ports.EffectiveNextAction.NONE.value):
            return False
        attempts = ports.AdversarialValidationAttemptRepository(repo_name)
        ordinary = retained.ordinary_correction if retained is not None and retained.head_sha == head_sha else None
        if ordinary is None and inputs is None:
            return False
        status, error = ports._get_published_adversarial_validation_status(github_client, repo_name, pr_number, head_sha)
        if error or status not in {None, "PASS", "BLOCKED", "NEEDS_FIX", "NEEDS_TESTS"}:
            return False
        legacy = None
        if ordinary is None and status == "BLOCKED" and (inputs is not None):
            evidence = inputs.gate.closure_evidence()
            published_sequence = attempts.latest_published_sequence(pr_number, head_sha)
            legacy = next((item for item in evidence.evidence.for_pr(pr_number) if item.attempt_sequence == published_sequence and item.head_sha == head_sha and (item.ordinary.result in {"NEEDS_FIX", "NEEDS_TESTS"})), None)
            if legacy is not None:
                ordinary = ports.OrdinaryCorrectionEvidence(
                    repository=legacy.repository,
                    pr_number=legacy.pr_number,
                    head_sha=legacy.head_sha,
                    base_sha=legacy.base_sha,
                    contract_identity=legacy.contract_identity,
                    policy_identity=legacy.policy_identity,
                    open_epoch=legacy.open_epoch,
                    attempt_id=legacy.attempt_id,
                    attempt_sequence=legacy.attempt_sequence,
                    strong_round_id=legacy.round_id,
                    audited_head_sha=legacy.audited_head_sha,
                    finding_set_revision=legacy.finding_set_revision,
                    requires_strong_policy=True,
                    payload=legacy.ordinary.payload,
                )
        if ordinary is None:
            return False
        raw = ordinary.restore()
        if raw.result not in {"PASS", "NEEDS_FIX", "NEEDS_TESTS"}:
            return False
        bridge, target = ports._accepted_state_inputs(repo_name, pr_data, head_sha)
        contract_identity = inputs.contract.identity if inputs is not None else ordinary.contract_identity
        policy_identity = inputs.policy.identity if inputs is not None else ordinary.policy_identity
        target = ports.replace(target, contract_identity=contract_identity, policy_identity=policy_identity)
        with attempts.serialized_transition(), bridge.cycle.serialized_transition():
            snapshot = bridge.cycle.snapshot(pr_number)
            strong = snapshot.accepted_strong_round
            stale = snapshot.closed or snapshot.active_claim is not None or snapshot.requires_new_strong_round
            stale = stale or (ordinary.repository, ordinary.pr_number, ordinary.head_sha, ordinary.base_sha, ordinary.contract_identity, ordinary.policy_identity, ordinary.open_epoch, ordinary.strong_round_id, ordinary.audited_head_sha, ordinary.finding_set_revision) != (
                repo_name,
                pr_number,
                head_sha,
                base_sha,
                contract_identity,
                policy_identity,
                snapshot.open_epoch,
                strong.round_id if strong is not None else "",
                strong.head_sha if strong is not None else "",
                snapshot.finding_set_revision,
            )
            if attempts.latest_sequence(pr_number, head_sha) > ordinary.attempt_sequence:
                if force:
                    return False
                raise RuntimeError("a newer ordinary attempt supersedes the retained correction")
            if stale:
                return False
            registered = attempts.get(ordinary.attempt_id)
            if registered is None or (registered.sequence, registered.pr_number, registered.head_sha) != (ordinary.attempt_sequence, pr_number, head_sha):
                raise RuntimeError("the producing ordinary attempt is not registered for this target")
            if ports._ordinary_target_binding(github_client, repo_name, pr_number, ordinary, config) != (head_sha, base_sha, contract_identity, policy_identity):
                raise RuntimeError("the PR target or contract changed before saved correction recovery")
            state = ports._get_claimed_review_thread_state(github_client, repo_name, pr_number, config=config)
            if state.lookup_error:
                raise RuntimeError(state.lookup_error)
            threads = ports._authentic_root_threads(repo_name, state)
            projection = ports.project_accepted_findings(bridge, target, threads)
            sources = tuple(sorted((item.source_identity for item in projection.records)))
            if legacy is not None:
                ordinary = ports.replace(ordinary, accepted_sources=sources)
            elif ordinary.accepted_sources != sources:
                return False
            completion = ports._corrective_completion_marker(repo_name, pr_data, head_sha, github_client)
            if completion and ports._closure_review_due(repo_name, pr_number, head_sha, projection, completion):
                return False
            raw.accepted_finding_projection = ports.project_accepted_findings(bridge, target, threads, ports.accepted_finding_dispositions(raw, threads, projection, head_sha))
            application = ports._derive_effective_application(repo_name, pr_data, head_sha, raw, threads, github_client)
            if application is None or application.decision.next_action not in {ports.EffectiveNextAction.IMPLEMENTATION_REPAIR, ports.EffectiveNextAction.FOCUSED_TEST_REPAIR}:
                return False
            if retained is not None and (not retained.requires_handoff) and (not (application.decision.ordinary_findings or application.decision.ordinary_gaps)):
                return False
            ports._retain_effective_decision(repo_name, pr_data, head_sha, application, ordinary_evidence=ordinary)
            published = attempts.latest_published_sequence(pr_number, head_sha) == ordinary.attempt_sequence
            if not published:
                recovery = ports.recover_pending_adversarial_publications(repo_name, pr_number)
                if not recovery.success:
                    raise RuntimeError(f"retained review publication is pending: {recovery.reason}")
                published, publication_error = ports._reconcile_failed_adversarial_publication(github_client, repo_name, pr_number, head_sha, application.result)
                if not published:
                    if publication_error:
                        raise RuntimeError(publication_error)
                    publication = ports._publish_effective_review(repo_name, pr_number, head_sha, application.result, None)
                    if not publication.success:
                        published, publication_error = ports._reconcile_failed_adversarial_publication(github_client, repo_name, pr_number, head_sha, application.result)
                        if not published:
                            raise RuntimeError(f"retained review publication is unconfirmed: {publication_error or publication.reason}")
                    recovery = ports.recover_pending_adversarial_publications(repo_name, pr_number)
                    if not recovery.success:
                        raise RuntimeError(f"retained review roots are pending: {recovery.reason}")
                attempts.mark_published(ordinary.attempt_id)
            store.update(pr_number, publication=ports.PUBLICATION_CONFIRMED)
            if ports._ordinary_target_binding(github_client, repo_name, pr_number, ordinary, config) != (head_sha, base_sha, contract_identity, policy_identity):
                raise RuntimeError("the PR target or contract changed before saved correction handoff")
            actions.append(f"Resuming {application.decision.status} correction for PR #{pr_number} from retained ordinary attempt {ordinary.attempt_sequence}; no new adversarial validation was started")
            ports.record_reused(
                ports.PrAdversarialReviewTarget(repository=repo_name, pr_number=pr_number, head_sha=head_sha),
                policy_identity=ports._pr_adversarial_policy_identity(config, True),
                source_review_id=None,
                native_verdict=raw.result,
                attempt_id=ordinary.attempt_id,
                attempt_sequence=ordinary.attempt_sequence,
            )
            result = application.result
            feedback, facts = ports._dispatch_effective_correction(
                github_client,
                repo_name,
                pr_data,
                head_sha,
                ports.format_adversarial_review_summary(result, head_sha, attached_finding_count=0, attached_test_oracle_gap_count=0),
                result,
                application,
                config,
                [ports.format_adversarial_finding_comment(finding) for finding in result.findings] + [ports.format_test_oracle_gap_comment(gap) for gap in result.open_test_oracle_gaps],
                authority_fenced=True,
            )
            actions.extend(feedback)
            if isinstance(actions, ports.PRReviewCoordinationResult):
                actions.record_disposition(facts["review_disposition"])
            ports._record_pr_stage(
                pr_number,
                "pr.repair-delegation",
                f"pr#{pr_number} repair delegation",
                ports._adversarial_repair_outcome(feedback, processing_status),
                {"effect": "adversarial-feedback-replay", "examined_head": head_sha, "source_attempt_sequence": ordinary.attempt_sequence, "additional_model_execution": False, **facts},
            )
            return True
    except ports._OrdinaryCorrectionInputsChanged:
        return False
    except ports.DecisionRetentionError as exc:
        actions.append(f"Saved ordinary correction for PR #{pr_number} could not be retained or read: {exc}; no dependent effect was emitted")
        if isinstance(actions, ports.PRReviewCoordinationResult):
            actions.record_disposition(ports.ReviewDisposition.OPERATIONAL_FAILURE)
        ports._record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", ports.Outcome.FAILED, {"examined_head": head_sha, "reason": str(exc), "review_disposition": ports.ReviewDisposition.OPERATIONAL_FAILURE.value})
        if processing_status is not None:
            processing_status.outcome = ports.PRProcessingOutcome.FAILED
            processing_status.error = str(exc)
        return True
    except Exception as exc:
        ports._record_review_wait(repo_name, pr_number, head_sha, actions, processing_status, f"saved ordinary correction could not be recovered: {exc}")
        return True


def _saved_repair_has_current_evidence(ports, retained: Optional[RetainedDecision], head_sha: str, outstanding: Sequence[AcceptedFindingRecord]) -> bool:
    """Whether a retained decision accounts for every outstanding accepted finding at this head.

    The effective decision derives a repair for an open accepted finding only from current-target
    evidence, so its retained record is the durable proof that a saved violation is current.
    Addressed findings awaiting lifecycle closure are accounted for separately and are not repairs.
    """
    repair_actions = {ports.EffectiveNextAction.IMPLEMENTATION_REPAIR.value, ports.EffectiveNextAction.FOCUSED_TEST_REPAIR.value}
    return retained is not None and retained.head_sha == head_sha and (retained.next_action in repair_actions) and ({record.source_identity for record in outstanding} <= set(retained.source_identities + retained.pending_closure_source_identities + retained.pending_reconciliation_source_identities))


def assess_saved_review_state(ports, repo_name: str, pr_data: Dict[str, Any], head_sha: str, saved_status: Optional[str], claimed_review_threads: Sequence[Any], force: bool, github_client: Any = None, config: Optional[AutomationConfig] = None) -> SavedReviewAssessment:
    """Re-acquire accepted-finding applicability before consuming a saved verdict.

    A saved ordinary PASS (including a legacy contradictory one at this SHA) is
    clearance only while the accepted state is readable and closed for this
    target. Retained reconciliation work is resumed when its authority or
    association evidence has changed, and otherwise waits without repeating
    identical model or repair work.
    """
    if saved_status not in {"PASS", "BLOCKED", "NEEDS_FIX", "NEEDS_TESTS"}:
        return SavedReviewAssessment()
    try:
        bridge, target = ports._accepted_state_inputs(repo_name, pr_data, head_sha)
        inputs = None
        if github_client is not None:
            inputs = ports._two_tier_gate_inputs(github_client, repo_name, pr_data)
            if inputs is not None:
                target = ports.replace(target, contract_identity=inputs.contract.identity, policy_identity=inputs.policy.identity)
        projection = ports.project_accepted_findings(bridge, target, claimed_review_threads)
        retained = ports.EffectiveDecisionStore(repo_name).load(target.pr_number)
        changed_ordinary_inputs = ""
        if github_client is not None and inputs is None and (retained is not None) and (retained.ordinary_correction is not None) and (not retained.ordinary_correction.requires_strong_policy) and (retained.status != "PASS" or retained.next_action != ports.EffectiveNextAction.NONE.value):
            metadata = ports._strict_pull_request_target(github_client, repo_name, target.pr_number)
            if (metadata["head"]["sha"], metadata["base"]["sha"]) != (target.head_sha, target.base_sha):
                return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "the live PR target changed during saved-decision assessment", projection)
            current = ports._current_ordinary_correction_inputs(github_client, repo_name, metadata, config or AutomationConfig())
            supplied = retained.ordinary_correction.restore().correction_reference_context
            if supplied is None or (supplied.validation_snapshot, supplied.requirement_manifest_identity) != (current.validation_snapshot, current.requirement_manifest_identity):
                changed_ordinary_inputs = current.validation_snapshot
    except (ports.DecisionRetentionError, RuntimeError, ValueError, OSError) as exc:
        return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, f"retained effective-decision state is unreadable: {exc}")
    revision = ports.evidence_revision(projection)
    if changed_ordinary_inputs:
        if not ports.closure_projection_readable(projection):
            return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "changed ordinary inputs cannot be reassessed while accepted authority is unreadable or ambiguous", projection)
        recovery_key = "ordinary-correction-v1:" + ports.hashlib.sha256(f"{revision}:{changed_ordinary_inputs}".encode()).hexdigest()
        if not force and retained is not None and (recovery_key in retained.reconciliation_attempts):
            return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "ordinary recovery was already admitted for these changed validation inputs", projection)
        return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "changed ordinary Requirements or missing invocation binding require one fresh decision", projection, "" if force else recovery_key)
    if saved_status == "BLOCKED" and retained is not None and (retained.head_sha == head_sha) and (retained.ordinary_correction is not None) and (retained.next_action == ports.EffectiveNextAction.OPERATIONAL_WAIT.value):
        recovery_key = "ordinary-correction-v1:" + revision
        if force or (ports.closure_projection_readable(projection) and revision != retained.evidence_revision and (recovery_key not in retained.reconciliation_attempts)):
            return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "new authority permits one reassessment of the unfinished ordinary result", projection, "" if force else recovery_key)
        return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "the retained ordinary result is unfinished and authorizes no corrective thread replay", projection)
    if saved_status == "BLOCKED" and (retained is None or (retained.head_sha == head_sha and retained.ordinary_correction is None)):
        if not ports.closure_projection_readable(projection):
            return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "legacy ordinary evidence cannot be recovered while required authority is unreadable or ambiguous", projection)
        recovery_key = "ordinary-correction-v1:" + revision
        if not force and retained is not None and (recovery_key in retained.reconciliation_attempts):
            return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "the versioned legacy ordinary recovery was already admitted for this target and evidence", projection)
        return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "legacy review has no reusable normalized ordinary evidence; one versioned recovery is required", projection, "" if force else recovery_key)
    resumable = retained is not None and retained.head_sha == head_sha and (retained.next_action in {ports.EffectiveNextAction.RECONCILIATION.value, ports.EffectiveNextAction.CLOSURE_ACCEPTANCE.value})
    if retained is not None and resumable and (revision in retained.reconciliation_attempts) and (not force):
        return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "waiting for new authority or association evidence; the focused reconciliation for this evidence revision was already attempted", projection)
    if saved_status == "PASS" and (not ports.saved_pass_is_clearance(projection)):
        return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "a saved PASS is not clearance while accepted findings are open or their state is unavailable", projection)
    if saved_status == "PASS" and claimed_review_threads:
        return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "a saved PASS still has unresolved review threads requiring independent closure", projection)
    if saved_status in {"NEEDS_FIX", "NEEDS_TESTS"} and retained is not None and (retained.ordinary_correction is not None):
        if not ports.closure_projection_readable(projection):
            return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "retained ordinary correction authority is unreadable or ambiguous", projection)
        ordinary = retained.ordinary_correction
        binding_changed = (ordinary.head_sha, ordinary.base_sha, ordinary.contract_identity, ordinary.policy_identity, ordinary.open_epoch, ordinary.finding_set_revision, ordinary.accepted_sources) != (
            target.head_sha,
            target.base_sha,
            target.contract_identity,
            target.policy_identity,
            projection.open_epoch,
            projection.finding_set_revision,
            tuple(sorted((item.source_identity for item in projection.records))),
        )
        if binding_changed and (not force):
            recovery_key = "ordinary-correction-v1:" + revision
            if recovery_key in retained.reconciliation_attempts:
                return SavedReviewAssessment(ports.SAVED_REVIEW_WAIT, "fresh ordinary recovery was already admitted for this changed target and evidence", projection)
            return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "changed ordinary correction authority requires one fresh current-target decision", projection, recovery_key)
        return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "retained ordinary correction requires a new current-target decision", projection)
    if saved_status == "BLOCKED" and resumable:
        return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "retained reconciliation work has new authority or association evidence", projection)
    if saved_status == "BLOCKED" and retained is not None and (retained.head_sha == head_sha) and (retained.next_action == ports.EffectiveNextAction.CLOSURE_ACCEPTANCE.value):
        key = ports.closure_attempt_key(head_sha, projection.finding_set_revision, retained.handoff_generation)
        marker = ports._corrective_completion_marker(repo_name, pr_data, head_sha, github_client)
        if key not in retained.closure_attempts and ports.closure_ready(projection, head_sha, marker):
            return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "a pending closure acceptance has not completed and may now be attempted", projection)
    outstanding = ports.outstanding_records(projection)
    if saved_status in {"NEEDS_FIX", "NEEDS_TESTS"} and projection.complete and outstanding and (not _saved_repair_has_current_evidence(ports, retained, head_sha, outstanding)):
        return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "the saved nonpassing review is not backed by a retained current-evidence decision for the open accepted findings", projection)
    if saved_status in {"NEEDS_FIX", "NEEDS_TESTS"} and projection.complete and (not outstanding) and (retained is not None) and (retained.head_sha == head_sha) and retained.source_identities:
        return SavedReviewAssessment(ports.SAVED_REVIEW_REVALIDATE, "every accepted correction behind the saved verdict is now closed by its lifecycle owner", projection)
    return SavedReviewAssessment(ports.SAVED_REVIEW_REUSE, "", projection)


def replay_saved_nonpass_review(ports, github_client: Any, repo_name: str, pr_data: Dict[str, Any], head_sha: str, report: str, projection: Optional[AcceptedFindingProjection], config: AutomationConfig) -> Tuple[List[str], bool]:
    """Resume the outstanding work of a saved NEEDS_FIX/NEEDS_TESTS review.

    Once a delivered correction has completed (a new head, a supported local
    no-change completion, or changed provider activity) the finding is
    independently reassessed rather than replayed: the bool asks the caller to
    re-derive the decision, which reaches bounded closure on the ordinary
    session's own coverage. Otherwise the correction is routed through the
    originating route using the already-associated roots and canonical identities.
    """
    pr_number = int(pr_data["number"])
    records = ports.outstanding_records(projection) if projection is not None else ()
    if projection is not None and records:
        marker = ports._corrective_completion_marker(repo_name, pr_data, head_sha, github_client)
        if marker and ports._closure_review_due(repo_name, pr_number, head_sha, projection, marker):
            return ([f"The corrective request for PR #{pr_number} completed ({marker.split(':')[0]}); independently reassessing the accepted finding"], True)
    retained = ports.EffectiveDecisionStore(repo_name).load(pr_number)
    pending_sources = set(retained.pending_closure_source_identities + retained.pending_reconciliation_source_identities) if retained is not None and retained.head_sha == head_sha else set()
    pending_roots = {root for record in records if record.source_identity in pending_sources for root in record.root_comment_ids}
    records = tuple((record for record in records if record.source_identity not in pending_sources))
    bodies, blocker_ids = ports._compose_actionable_feedback(github_client, repo_name, pr_number, records)
    if not bodies and github_client is not None:
        try:
            replay_threads = github_client.get_pr_review_threads_strict(repo_name, pr_number)
            reviewer_identity = ports.resolve_reviewer_app_identity(repo_name)
        except Exception as exc:
            ports.logger.warning(f"Could not read review threads to check replayability for PR #{pr_number}: {exc}")
        else:
            attached = tuple(
                (
                    thread.comments[0].body
                    for thread in replay_threads
                    if not thread.is_resolved
                    and thread.comments
                    and (thread.comments[0].database_id not in pending_roots)
                    and thread.comments[0].body.startswith(ports._ADVERSARIAL_THREAD_HEADINGS)
                    and reviewer_identity.matches_login(thread.comments[0].author_login)
                    and (ports._adversarial_feedback_belongs_to_report(thread.comments[0].body, report, thread.id) or f"#### `{thread.id}`:" not in report)
                )
            )
            if attached:
                bodies = attached
            else:
                return ([f"Saved non-passing review for PR #{pr_number} has no actionable unresolved feedback to deliver; revalidating the current head"], True)
    result = ports._send_adversarial_validation_feedback_to_cloud_task(repo_name, pr_data, head_sha, report, github_client, bodies, config=config, canonical_blocker_ids=blocker_ids)
    ports._record_handoff_state(repo_name, pr_data, github_client, result)
    if projection is not None and records and (getattr(result, "local_phase", "") == "completed_no_change") and ports._closure_review_due(repo_name, pr_number, head_sha, projection, "local:completed_no_change"):
        return ([*result, f"A supported no-change completion was reported for PR #{pr_number}; independently reassessing the accepted finding"], True)
    return (result, False)
