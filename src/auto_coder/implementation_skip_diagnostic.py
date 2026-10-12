"""Read-only explanations for duplicate Implementation-generation starts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .local_job_handoff import InvocationOutcome, LocalIssueJobEvidence, LocalJobEffect, LocalJobState


@dataclass(frozen=True)
class ImplementationSkipDiagnostic:
    message: str
    status: str
    current_attempt: str = ""


class ImplementationEvidenceReader(Protocol):
    def issue_job_evidence(self, repository: str, issue_number: int) -> tuple[LocalIssueJobEvidence, ...]: ...


_SECRET = re.compile(r"(?i)(token|password|secret|authorization|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)")


def _bounded_reason(value: str) -> str:
    compact = " ".join(value.split())
    return _SECRET.sub(r"\1\2[REDACTED]", compact)[:300]


def _effect(evidence: LocalIssueJobEvidence, name: str) -> LocalJobEffect | None:
    return next((effect for effect in evidence.effects if effect.name == name), None)


def _describe(evidence: LocalIssueJobEvidence) -> str:
    job = evidence.job
    identity = f"attempt={job.upstream_attempt} job={job.job_id[:12]}"
    if job.invocation_outcome is None:
        if job.state is LocalJobState.PENDING:
            invocation = "invocation=pending"
        elif job.state is LocalJobState.RUNNING:
            invocation = "invocation=running" if job.provider_entered else "invocation=pending-provider-entry"
        else:
            invocation = "invocation=unknown"
    else:
        invocation = f"invocation={job.invocation_outcome.value}"
        if job.invocation_outcome in {InvocationOutcome.FAILED, InvocationOutcome.INTERRUPTED, InvocationOutcome.CANNOT_FIX} and job.diagnostic:
            invocation += f" reason={_bounded_reason(job.diagnostic)!r}"

    association = _effect(evidence, "association")
    publication = _effect(evidence, "pr")
    if association is not None and association.state == "completed" and association.evidence.isdigit():
        downstream = f"publication=confirmed PR #{association.evidence} (not proof of merge or validation)"
    elif association is not None and association.state == "completed":
        downstream = "publication=inconsistent (confirmed association has no valid PR identity)"
    elif publication is not None:
        downstream = f"publication={publication.state}"
        if publication.evidence:
            downstream += f" detail={_bounded_reason(publication.evidence)!r}"
    elif job.invocation_outcome is InvocationOutcome.COMPLETED:
        downstream = "publication=unconfirmed (local result only)"
    else:
        downstream = "publication=unknown"
    return f"{identity}: {invocation}; {downstream}"


def explain_already_owned(
    store: ImplementationEvidenceReader,
    repository: str,
    issue_number: int,
    generation: str,
    current_execution_id: str = "",
) -> ImplementationSkipDiagnostic:
    """Explain a refusal without changing any admission or job authority."""
    prefix = "Skipped - duplicate automatic-start suppression: this Implementation generation already has a durable " "production start. A historical durable start is not proof that code was implemented, tests passed, or a PR was published."
    retry = f"To request a distinct attempt, use `auto-coder process-issues --only https://github.com/{repository}/issues/{issue_number} " "--force --retry`. This does not guarantee resumption of retained edits/session; readiness, authority, ownership, " "dependency, and provider gates still apply."
    try:
        all_evidence = store.issue_job_evidence(repository, issue_number)
    except RuntimeError:
        return ImplementationSkipDiagnostic(f"{prefix} Outcome evidence is unavailable. {retry}", "unavailable")

    attributable = tuple(item for item in all_evidence if item.job.owner_generation == generation or (current_execution_id and item.job.implementation_execution_id == current_execution_id))
    current = tuple(item for item in attributable if current_execution_id and item.job.implementation_execution_id == current_execution_id)
    if len(current) != 1 and len(attributable) == 1:
        current = attributable

    history = " ".join(f"History[{_describe(item)}]" for item in attributable)
    if len(current) == 1:
        current_text = f"Current[{_describe(current[0])}]"
        status = "known"
        current_attempt = current[0].job.upstream_attempt
    elif len(current) > 1:
        current_text = "Current outcome=inconsistent (the authoritative execution binding identifies multiple jobs)."
        status = "inconsistent"
        current_attempt = ""
    elif len(attributable) > 1:
        current_text = "Current outcome=unknown (multiple attributable attempts have no unambiguous current binding)."
        status = "unknown"
        current_attempt = ""
    else:
        current_text = "Current outcome=unknown (no attributable detailed job evidence)."
        status = "unknown"
        current_attempt = ""

    live = any(item.job.state in {LocalJobState.PENDING, LocalJobState.RUNNING, LocalJobState.RESULT_RECORDED, LocalJobState.DOWNSTREAM_EFFECTS_PENDING} for item in current)
    guidance = "Inspect and reconcile the live/pending owned work before considering a replacement. " if live else ""
    return ImplementationSkipDiagnostic(" ".join(part for part in (prefix, current_text, history, guidance + retry) if part), status, current_attempt)
