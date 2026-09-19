from __future__ import annotations

import threading

import pytest

from auto_coder.issue_review_rerun import IssueReviewRerunOperation, IssueReviewRerunStore, ReviewSubject
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle


def test_atomic_idempotent_request_binding_and_overlap_supersession(tmp_path):
    store = IssueReviewRerunStore(tmp_path / "reruns.sqlite3")
    first = ReviewSubject("Owner/Repo", "individual", 10)
    overlap = ReviewSubject("owner/repo", "decomposition", 20)
    unrelated = ReviewSubject("other/repo", "individual", 10)

    accepted = store.accept("request-a", [overlap, first, unrelated])
    assert [(item.subject.key, item.state) for item in accepted] == [
        ("other/repo:individual:10", "pending"),
        ("owner/repo:decomposition:20", "pending"),
        ("owner/repo:individual:10", "pending"),
    ]
    assert store.accept("request-a", [unrelated, first, overlap]) == accepted
    with pytest.raises(ValueError, match="different subject set"):
        store.accept("request-a", [first])

    second = store.accept("request-b", [overlap])
    assert second[0].state == "pending"
    old_states = {item.subject.key: item.state for item in store.status("request-a")}
    assert old_states[overlap.key] == "superseded"
    assert old_states[first.key] == old_states[unrelated.key] == "pending"
    assert store.authority(first)[1:] == ("request-a", "pending")
    assert store.authority(overlap)[1:] == ("request-b", "pending")


def test_status_requires_current_fresh_terminal_and_records_source(tmp_path):
    store = IssueReviewRerunStore(tmp_path / "reruns.sqlite3")
    subject = ReviewSubject("owner/repo", "individual", 12)
    authority = store.accept("request", [subject])[0].authority

    assert store.defer(subject, authority, "readiness label is absent")
    assert store.status("request")[0].reason == "readiness label is absent"
    assert not store.satisfy(subject, authority + 1, "decision", "model")
    assert store.satisfy(subject, authority, "decision", "local-only")
    status = store.status("request")[0]
    assert (status.state, status.decision_reference, status.evaluation_source) == (
        "satisfied",
        "decision",
        "local-only",
    )


def test_accept_materializes_review_work_or_records_concrete_deferral(tmp_path):
    store = IssueReviewRerunStore(tmp_path / "reruns.sqlite3")
    operation = IssueReviewRerunOperation(store)
    runnable = ReviewSubject("owner/repo", "individual", 12)
    deferred = ReviewSubject("owner/repo", "decomposition", 20)
    admitted = []

    statuses = operation.accept(
        "request",
        [runnable, deferred],
        lambda subject: admitted.append(subject.key) or ("parent readiness is absent" if subject == deferred else None),
    )

    assert admitted == [deferred.key, runnable.key]
    by_subject = {status.subject.key: status for status in statuses}
    assert by_subject[runnable.key].state == "pending"
    assert by_subject[runnable.key].reason is None
    assert by_subject[deferred.key].state == "deferred"
    assert by_subject[deferred.key].reason == "parent readiness is absent"


def test_recovery_reconstructs_crash_after_accept_before_wake(tmp_path):
    path = tmp_path / "reruns.sqlite3"
    subject = ReviewSubject("owner/repo", "individual", 42)
    IssueReviewRerunStore(path).accept("request", [subject])
    admitted = []

    recovered = IssueReviewRerunOperation(IssueReviewRerunStore(path)).recover("owner/repo", lambda current: admitted.append(current.key) or None)

    assert admitted == [subject.key]
    assert [(status.request_id, status.state, status.reason) for status in recovered] == [("request", "pending", None)]


def test_running_pre_request_review_cannot_restore_or_satisfy_authority(tmp_path):
    body = "## Objective\nPreserve the value.\n\n## Requirements\nREQ-001: Return the value."
    manifest = build_normative_issue_manifest(42, "Title", body)
    entered = threading.Event()
    release = threading.Event()

    def analyzer(_manifest, _body):
        entered.set()
        assert release.wait(5)
        return SpecificationAnalysisResult("READY")

    lifecycle = SpecificationValidationLifecycle("owner/repo", "route", tmp_path / "decisions.json", analyzer)
    result = []
    thread = threading.Thread(target=lambda: result.append(lifecycle.decide(manifest, "Title", body)))
    thread.start()
    assert entered.wait(5)

    subject = ReviewSubject("owner/repo", "individual", 42)
    lifecycle.reruns.accept("rerun-42", [subject])
    release.set()
    thread.join(5)

    assert result[0].verdict == "ERROR"
    assert "revoked" in (result[0].remediation_reason or "")
    assert lifecycle.store.get(lifecycle.identity(42, "Title", body)) is None
    assert lifecycle.reruns.status("rerun-42")[0].state == "pending"

    fresh = lifecycle.decide(manifest, "Title", body)
    assert fresh.verdict == "READY"
    assert fresh.rerun_request_id == "rerun-42"
    assert lifecycle.reruns.status("rerun-42")[0].state == "satisfied"
