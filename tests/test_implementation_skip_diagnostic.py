from __future__ import annotations

from auto_coder.implementation_skip_diagnostic import explain_already_owned
from auto_coder.local_job_handoff import (
    InvocationOutcome,
    LocalIssueJobEvidence,
    LocalJobEffect,
    LocalJobKind,
    LocalJobRecord,
    LocalJobState,
)


class EvidenceStore:
    def __init__(self, evidence: tuple[LocalIssueJobEvidence, ...] = (), *, unavailable: bool = False) -> None:
        self.evidence = evidence
        self.unavailable = unavailable
        self.reads = 0

    def issue_job_evidence(self, repository: str, issue_number: int) -> tuple[LocalIssueJobEvidence, ...]:
        assert repository == "owner/repo"
        assert issue_number == 42
        self.reads += 1
        if self.unavailable:
            raise RuntimeError("broken database")
        return self.evidence


def job(attempt: str, generation: str, outcome: InvocationOutcome, *, execution: str = "execution") -> LocalJobRecord:
    return LocalJobRecord(
        job_id=f"job-{attempt}-123456789",
        kind=LocalJobKind.ISSUE_IMPLEMENTATION,
        repository="owner/repo",
        target_number=42,
        upstream_attempt=attempt,
        upstream_incarnation="dispatch",
        backend_name="codex",
        input_identity="input",
        invocation_input="prompt",
        state=LocalJobState.SETTLED,
        execution_incarnation=f"incarnation-{attempt}",
        invocation_outcome=outcome,
        diagnostic="token=private provider failed",
        owner_generation=generation,
        implementation_execution_id=execution,
    )


def test_failed_attempt_is_separate_from_publication_and_retry_guidance() -> None:
    store = EvidenceStore((LocalIssueJobEvidence(job("attempt-1", "generation", InvocationOutcome.FAILED)),))

    result = explain_already_owned(store, "owner/repo", 42, "generation", "execution")

    assert result.status == "known"
    assert result.current_attempt == "attempt-1"
    assert "duplicate automatic-start suppression" in result.message
    assert "not proof that code was implemented, tests passed, or a PR was published" in result.message
    assert "invocation=failed reason='token=[REDACTED] provider failed'" in result.message
    assert "publication=unknown" in result.message
    assert "--only https://github.com/owner/repo/issues/42 --force --retry" in result.message
    assert "does not guarantee resumption" in result.message
    assert store.reads == 1


def test_completed_local_result_and_confirmed_publication_are_distinct() -> None:
    unpublished = LocalIssueJobEvidence(job("attempt-1", "generation", InvocationOutcome.COMPLETED, execution="old"))
    published_job = job("attempt-2", "generation", InvocationOutcome.COMPLETED, execution="current")
    published = LocalIssueJobEvidence(
        published_job,
        (LocalJobEffect(published_job.job_id, published_job.execution_incarnation, "association", "completed", "77"),),
    )
    store = EvidenceStore((unpublished, published))

    result = explain_already_owned(store, "owner/repo", 42, "generation", "current")

    assert result.current_attempt == "attempt-2"
    assert "publication=unconfirmed (local result only)" in result.message
    assert "publication=confirmed PR #77 (not proof of merge or validation)" in result.message
    history = result.message[result.message.index("History[") :]
    assert history.index("attempt=attempt-1") < history.index("attempt=attempt-2")


def test_multiple_attempts_without_binding_keep_current_unknown_and_exclude_foreign_generation() -> None:
    first = LocalIssueJobEvidence(job("attempt-1", "generation", InvocationOutcome.FAILED, execution="one"))
    second = LocalIssueJobEvidence(job("attempt-2", "generation", InvocationOutcome.COMPLETED, execution="two"))
    foreign = LocalIssueJobEvidence(job("foreign", "other-generation", InvocationOutcome.COMPLETED, execution="three"))

    result = explain_already_owned(EvidenceStore((first, second, foreign)), "owner/repo", 42, "generation")

    assert result.status == "unknown"
    assert result.current_attempt == ""
    assert "multiple attributable attempts have no unambiguous current binding" in result.message
    assert "attempt=attempt-1" in result.message
    assert "attempt=attempt-2" in result.message
    assert "attempt=foreign" not in result.message


def test_unavailable_diagnostic_evidence_preserves_refusal() -> None:
    result = explain_already_owned(EvidenceStore(unavailable=True), "owner/repo", 42, "generation")

    assert result.status == "unavailable"
    assert result.message.startswith("Skipped - duplicate automatic-start suppression")
    assert "Outcome evidence is unavailable" in result.message
