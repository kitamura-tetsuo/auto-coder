"""Read-only, durable evidence collection for Codex slot retirement.

Unlike :mod:`codex_observation`, this adapter is deliberately a many-run
snapshot.  It never invokes an attribution resolver (which may establish a
binding) and never converts a failed enumeration into an empty result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional

from .cloud_run import CloudRun, CloudRunRepository
from .cloud_task_client_base import CloudTaskState
from .codex_observation import ObservationBinding, execution_evidence
from .codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository, closes_issue, task_ids_from_text
from .codex_wham_client import CodexWhamClient, WhamTask, WhamTurn
from .codex_work_accounting import CodexWorkAccounting, CodexWorkOperation, CodexWorkSnapshot, WorkAccountingStatus
from .implementation_retirement import ImplementationPRObservation, PRTerminalState
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotSnapshot
from .util.gh_cache import GitHubClient


class CodexEvidenceState(str, Enum):
    TERMINAL = "terminal"
    TERMINAL_FAILED = "terminal_failed"
    TERMINAL_CANCELLED = "terminal_cancelled"
    ACTIVE_RUNNING = "active_running"
    ACTIVE_QUEUED = "active_queued"
    ACTIVE_PAUSED = "active_paused"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CandidateSourceProvenance:
    source: str
    consistency_identity: str
    complete: bool


@dataclass(frozen=True)
class CodexTaskRetirementEvidence:
    task_id: str
    attempt: int
    state: CodexEvidenceState
    assistant_turn_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class OperationSettlementCertificate:
    logical_operation_id: str
    task_id: str
    assistant_turn_id: str
    matched_request_id: str


@dataclass(frozen=True)
class CodexRetirementObservation:
    repository: str
    owner: ImplementationOwner
    incarnation: str
    activity_revision: int
    accounting_revision: int
    task_evidence: tuple[CodexTaskRetirementEvidence, ...]
    implementation_prs: tuple[ImplementationPRObservation, ...]
    settlement_certificates: tuple[OperationSettlementCertificate, ...]
    provenance: tuple[CandidateSourceProvenance, ...]
    incomplete_reasons: tuple[str, ...] = ()

    @property
    def conclusive(self) -> bool:
        terminal = {CodexEvidenceState.TERMINAL, CodexEvidenceState.TERMINAL_FAILED, CodexEvidenceState.TERMINAL_CANCELLED}
        return bool(not self.incomplete_reasons and self.implementation_prs and self.task_evidence and all(item.is_terminal for item in self.implementation_prs) and all(item.state in terminal for item in self.task_evidence))


def _token(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _runs_projection(runs: list[CloudRun]) -> list[dict[str, object]]:
    return [run.to_dict() for run in sorted(runs, key=lambda item: (item.attempt, item.task_id))]


def _task_state(task: Optional[WhamTask], binding: ObservationBinding) -> CodexTaskRetirementEvidence:
    evidence = execution_evidence(task, binding)
    states = {
        CloudTaskState.COMPLETED: CodexEvidenceState.TERMINAL,
        CloudTaskState.FAILED: CodexEvidenceState.TERMINAL_FAILED,
        CloudTaskState.CANCELLED: CodexEvidenceState.TERMINAL_CANCELLED,
        CloudTaskState.RUNNING: CodexEvidenceState.ACTIVE_RUNNING,
        CloudTaskState.QUEUED: CodexEvidenceState.ACTIVE_QUEUED,
        CloudTaskState.PAUSED: CodexEvidenceState.ACTIVE_PAUSED,
    }
    state = states.get(evidence.state, CodexEvidenceState.UNKNOWN)
    reason = "" if state is not CodexEvidenceState.UNKNOWN else "missing, contradictory, unsupported, or wrong-identity current-turn evidence"
    return CodexTaskRetirementEvidence(binding.task_id, binding.attempt, state, evidence.assistant_turn_id, reason)


def _request_matches(turn: WhamTurn, request_id: str) -> bool:
    if turn.id == request_id:
        return True
    raw = turn.raw_data
    if not isinstance(raw, dict):
        return False
    inner = raw.get("turn")
    values = inner if isinstance(inner, dict) else raw
    return values.get("request_id") == request_id or values.get("source_request_id") == request_id


def _settlement(operation: CodexWorkOperation, task: Optional[WhamTask], turns: list[WhamTurn]) -> Optional[OperationSettlementCertificate]:
    if operation.task_id is None or task is None or not operation.accepted or task.id != operation.task_id:
        return None
    current = task.current_assistant_turn
    if current is None or not current.id or current.status.lower() not in {"completed", "failed", "error", "cancelled", "canceled"}:
        return None
    # Initial creation is bound by the accepted task identity. Follow-ups must
    # have a matching user request after their captured assistant baseline and
    # before the terminal assistant turn.
    if operation.causal_baseline is None:
        matched = operation.source_request_id
    else:
        baseline_indexes = [index for index, turn in enumerate(turns) if turn.id == operation.causal_baseline]
        request_indexes = [index for index, turn in enumerate(turns) if _request_matches(turn, operation.source_request_id)]
        terminal_indexes = [index for index, turn in enumerate(turns) if turn.id == current.id]
        if not baseline_indexes or not request_indexes or not terminal_indexes:
            return None
        if not any(base < request < terminal for base in baseline_indexes for request in request_indexes for terminal in terminal_indexes):
            return None
        matched = operation.source_request_id
    if not (operation.publication_complete and operation.tracking_complete):
        return None
    return OperationSettlementCertificate(operation.logical_operation_id, operation.task_id, current.id, matched)


def _pr_state(payload: dict[str, object], expected: int) -> ImplementationPRObservation:
    if payload.get("number") != expected:
        return ImplementationPRObservation(expected, PRTerminalState.UNKNOWN)
    merged = payload.get("merged") is True or payload.get("merged_at") is not None
    state = str(payload.get("state", "")).lower()
    if merged:
        normalized = PRTerminalState.MERGED
    elif state == "closed":
        normalized = PRTerminalState.CLOSED
    elif state == "open":
        normalized = PRTerminalState.OPEN
    else:
        normalized = PRTerminalState.UNKNOWN
    return ImplementationPRObservation(expected, normalized, merged)


def collect_codex_retirement_observation(
    repository: str,
    owner: ImplementationOwner,
    incarnation: str,
    slots: ImplementationSlotRepository,
    accounting_snapshot: CodexWorkSnapshot,
    runs: CloudRunRepository,
    github: GitHubClient,
    *,
    wham: Optional[CodexWhamClient] = None,
    attributions: Optional[CodexPrAttributionRepository] = None,
) -> CodexRetirementObservation:
    """Collect a fail-closed Codex retirement snapshot without writing state."""
    reasons: list[str] = []
    provenance: list[CandidateSourceProvenance] = []
    client = wham or CodexWhamClient()
    registry = attributions or CodexPrAttributionRepository(repository)

    slot_before = slots.snapshot()
    if not isinstance(slot_before, ImplementationSlotSnapshot):
        raise ValueError("implementation slot snapshot unavailable")
    slot_owner = next((item for item in slot_before.owners if item.owner == owner), None)
    if slot_owner is None or slot_owner.incarnation != incarnation:
        raise ValueError("implementation slot binding is absent or stale")
    if accounting_snapshot.repository != repository or accounting_snapshot.owner != owner or accounting_snapshot.incarnation != incarnation:
        reasons.append("work accounting binding mismatch")
    if accounting_snapshot.accounting_status is not WorkAccountingStatus.COMPLETE:
        reasons.append(f"work accounting is {accounting_snapshot.accounting_status.value}")
    provenance.append(CandidateSourceProvenance("slot-membership", _token(asdict(slot_owner)), True))
    provenance.append(CandidateSourceProvenance("work-accounting", _token(asdict(accounting_snapshot)), accounting_snapshot.accounting_status is WorkAccountingStatus.COMPLETE))

    try:
        issue_runs = runs.list_for_issue(owner.number)
        runs_identity = _token(_runs_projection(issue_runs))
        provenance.append(CandidateSourceProvenance("cloud-runs-and-publication-inventory", runs_identity, True))
    except Exception as exc:
        issue_runs = []
        reasons.append(f"CloudRun inventory unavailable: {type(exc).__name__}")
        provenance.append(CandidateSourceProvenance("cloud-runs-and-publication-inventory", "unavailable", False))

    relevant_runs: list[CloudRun] = []
    for run in issue_runs:
        if run.repo_name != repository or run.provider != "codex-cloud" or not run.task_id:
            reasons.append(f"unsupported or conflicting CloudRun attempt {run.attempt}")
            continue
        relevant_runs.append(run)

    tasks: list[CodexTaskRetirementEvidence] = []
    task_payloads: dict[str, Optional[WhamTask]] = {}
    task_turns: dict[str, list[WhamTurn]] = {}
    for run in relevant_runs:
        task = client.get_task(run.task_id)
        task_payloads[run.task_id] = task
        evidence = _task_state(task, ObservationBinding.from_run(run))
        tasks.append(evidence)
        if evidence.state is CodexEvidenceState.UNKNOWN:
            reasons.append(f"task {run.task_id}: {evidence.reason}")
        try:
            task_turns[run.task_id] = client.get_task_turns(run.task_id)
        except Exception as exc:
            task_turns[run.task_id] = []
            reasons.append(f"task history {run.task_id} unavailable: {type(exc).__name__}")

    certificates: list[OperationSettlementCertificate] = []
    for operation in accounting_snapshot.operations:
        certificate = _settlement(operation, task_payloads.get(operation.task_id or ""), task_turns.get(operation.task_id or "", []))
        if certificate is None:
            cancelled_before_delivery = operation.settled and operation.definite_non_delivery and not operation.accepted
            if not cancelled_before_delivery:
                reasons.append(f"operation {operation.logical_operation_id} lacks causal settlement evidence")
        else:
            certificates.append(certificate)

    candidates = set(slot_owner.implementation_prs)
    for run in relevant_runs:
        candidates.update(run.pull_request_numbers)
    attribution_token = "unavailable"
    try:
        origins, attribution_token = registry.snapshot()
        for origin in origins:
            if origin.repository == repository and origin.issue_number == owner.number:
                candidates.add(origin.pr_number)
        provenance.append(CandidateSourceProvenance("verified-pr-attributions", attribution_token, True))
    except Exception as exc:
        reasons.append(f"verified PR attribution inventory unavailable: {type(exc).__name__}")
        provenance.append(CandidateSourceProvenance("verified-pr-attributions", attribution_token, False))
    native_complete = True
    native_identity = "unavailable"
    try:
        connected = github.get_connected_prs(repository, owner.number, strict=True)
        candidates.update(int(number) for number in connected)
        native_identity = _token(sorted(connected))
    except Exception as exc:
        native_complete = False
        reasons.append(f"native PR association discovery incomplete: {type(exc).__name__}")
    provenance.append(CandidateSourceProvenance("native-issue-associations", native_identity, native_complete))

    open_complete = True
    open_identity = "incomplete"
    try:
        open_prs = github.get_open_pull_requests_strict(repository)
        open_identity = _token(open_prs)
        for pr in open_prs:
            number = pr.get("number")
            if not isinstance(number, int):
                open_complete = False
                continue
            attribution = registry.get(number)  # pure read: never resolve/establish here
            verified = attribution.origin
            task_match = bool(task_ids_from_text(pr.get("body")) & {run.task_id for run in relevant_runs})
            head = pr.get("head")
            head_ref = head.get("ref") if isinstance(head, dict) else pr.get("headRefName")
            retained_head = any(run.publication_head_repository == repository and run.publication_head_ref == head_ref for run in relevant_runs)
            if attribution.disposition is AttributionDisposition.VERIFIED and verified is not None and verified.issue_number == owner.number and verified.repository == repository:
                candidates.add(number)
            elif (task_match or retained_head) and closes_issue(pr, repository, owner.number):
                candidates.add(number)
            elif attribution.disposition in {AttributionDisposition.CONFLICT, AttributionDisposition.UNAVAILABLE} and (task_match or retained_head or closes_issue(pr, repository, owner.number)):
                open_complete = False
    except Exception as exc:
        open_complete = False
        reasons.append(f"open PR discovery incomplete: {type(exc).__name__}")
    if not open_complete:
        reasons.append("open PR attribution is incomplete")
    provenance.append(CandidateSourceProvenance("strict-open-pr-discovery", open_identity, open_complete))

    prs: list[ImplementationPRObservation] = []
    for number in sorted(candidates):
        try:
            payload = github.get_pull_request_metadata_strict(repository, number)
            prs.append(_pr_state(payload, number))
            if prs[-1].state is PRTerminalState.UNKNOWN:
                reasons.append(f"PR #{number} strict metadata is unknown")
        except Exception as exc:
            prs.append(ImplementationPRObservation(number, PRTerminalState.UNKNOWN))
            reasons.append(f"PR #{number} strict metadata unavailable: {type(exc).__name__}")

    # Re-read every durable candidate source. A token mismatch makes this
    # result stale; the captured tokens remain available to the guarded user.
    slot_after = slots.snapshot()
    if not isinstance(slot_after, ImplementationSlotSnapshot):
        reasons.append("slot membership revalidation unavailable")
    else:
        current_owner = next((item for item in slot_after.owners if item.owner == owner), None)
        if current_owner is None or _token(asdict(current_owner)) != provenance[0].consistency_identity:
            reasons.append("slot membership changed during collection")
    try:
        if _token(_runs_projection(runs.list_for_issue(owner.number))) != runs_identity:
            reasons.append("CloudRun/publication inventory changed during collection")
    except Exception as exc:
        reasons.append(f"CloudRun inventory revalidation unavailable: {type(exc).__name__}")
    try:
        _origins, current_attribution_token = registry.snapshot()
        if current_attribution_token != attribution_token:
            reasons.append("verified PR attribution inventory changed during collection")
    except Exception as exc:
        reasons.append(f"verified PR attribution revalidation unavailable: {type(exc).__name__}")
    try:
        current_accounting = CodexWorkAccounting(slots).snapshot(owner, incarnation)
        if _token(asdict(current_accounting)) != provenance[1].consistency_identity:
            reasons.append("work accounting changed during collection")
    except Exception as exc:
        reasons.append(f"work accounting revalidation unavailable: {type(exc).__name__}")
    try:
        current_connected = github.get_connected_prs(repository, owner.number, strict=True)
        if _token(sorted(current_connected)) != native_identity:
            reasons.append("native PR associations changed during collection")
    except Exception as exc:
        reasons.append(f"native PR association revalidation unavailable: {type(exc).__name__}")
    try:
        if _token(github.get_open_pull_requests_strict(repository)) != open_identity:
            reasons.append("open PR discovery changed during collection")
    except Exception as exc:
        reasons.append(f"open PR discovery revalidation unavailable: {type(exc).__name__}")

    return CodexRetirementObservation(
        repository,
        owner,
        incarnation,
        slot_owner.activity_revision or 0,
        accounting_snapshot.revision,
        tuple(tasks),
        tuple(prs),
        tuple(certificates),
        tuple(provenance),
        tuple(dict.fromkeys(reasons)),
    )
