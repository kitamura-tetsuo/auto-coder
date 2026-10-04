"""Effective review decisions applied against real stores and a controlled GitHub transport.

Accepted findings come from the production Strong acceptance path, the ordinary
review is the real ``run_adversarial_validation`` parse with a controlled model
response, and native review publication goes through the real
``GitHubAppReviewer`` over a scripted HTTP transport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
import pytest

from auto_coder.accepted_finding_bridge import OUTCOME_STILL_VALID_OBSERVED, AcceptedFindingBridge, AcceptedFindingProjection, OrdinaryDisposition
from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.adversarial_validator import AdversarialValidationResult, RequirementCoverageEntry
from auto_coder.effective_decision_application import (
    HANDOFF_DISPATCHED,
    HANDOFF_WAITING,
    WAIT_ALLOWANCE_EXHAUSTED,
    WAIT_INDETERMINATE,
    WAIT_QUOTA,
    WAIT_ROUTE_UNAVAILABLE,
    ApprovalAuthority,
    DecisionRetentionError,
    EffectiveDecisionStore,
    apply_effective_decision,
    build_retained_record,
    classify_repair_handoff,
    closure_ready,
    derive_application,
    evidence_revision,
    raw_ordinary_clear,
    saved_pass_is_clearance,
)
from auto_coder.effective_review_decision import EffectiveNextAction, derive_effective_review_decision
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppConfig
from auto_coder.pr_processor import CloudReviewRepairResult
from auto_coder.review_thread_validation import ClaimedReviewThread
from tests.test_accepted_finding_bridge import (  # noqa: F401  (env is a shared fixture)
    CONTRACT,
    REPO,
    Env,
    _close,
    _commit,
    _only,
    accept_strong,
    env,
    finding_json,
    published_roots,
    run_ordinary,
    save_empty_session,
)


def still_valid_thread(thread_id: str, root_comment_id: int, body: str) -> ClaimedReviewThread:
    return ClaimedReviewThread(thread_id=thread_id, root_comment_database_id=root_comment_id, original_finding=body, blocker_ids=(), concern_ids=())


def ordinary_pass_response(thread_id: str, status: str = "STILL_VALID", evidence: str = "src/state.py:40 still drops state") -> str:
    return json.dumps(
        {
            "result": "PASS",
            "summary": "All requirement coverage is verified.",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "The guard enforces the requirement."}],
            "findings": [],
            "test_oracle_gaps": [],
            "thread_dispositions": [{"thread_id": thread_id, "status": status, "rationale": "Re-inspected the current head.", "evidence": evidence}],
        }
    )


# -- transport --------------------------------------------------------------


class RouterClient:
    """Scripted GitHub API: answers by route and records every outbound request."""

    def __init__(self, head_sha: str) -> None:
        self.head_sha = head_sha
        self.calls: list[tuple[str, str, Optional[dict[str, Any]]]] = []
        self.review_failure: Optional[Exception] = None
        self.before_review: Callable[[], None] = lambda: None
        self.review_records: list[dict[str, Any]] = []
        self.review_roots: dict[int, list[dict[str, Any]]] = {}
        self.next_root_id = 9000

    @property
    def posted_reviews(self) -> list[dict[str, Any]]:
        return [payload for method, path, payload in self.calls if method == "POST" and path.endswith("/reviews") and payload is not None]

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = urlparse(url).path
        payload = kwargs.get("json")
        if method == "POST" and path.endswith("/reviews"):
            self.before_review()  # a crash here means the request was never transmitted
        self.calls.append((method, path, payload))
        request = httpx.Request(method, url)

        def reply(data: object, status: int = 200) -> httpx.Response:
            return httpx.Response(status, json=data, request=request)

        if path == "/app":
            return reply({"slug": "auto-coder-reviewer", "id": 4765828})
        if path.endswith("/installation"):
            return reply({"id": 77})
        if path.endswith("/access_tokens"):
            return reply({"token": "t", "expires_at": "2099-01-01T00:00:00Z"}, 201)
        if method == "GET" and path.endswith(f"/pulls/{self._pr(path)}"):
            return reply({"head": {"sha": self.head_sha}})
        if method == "GET" and path.endswith("/files"):
            return reply([{"filename": "src/state.py", "patch": "@@ -39,1 +39,2 @@\n state = True\n+guard = True"}])
        if method == "GET" and path.endswith("/reviews"):
            return reply(self.review_records)
        if method == "GET" and path.endswith("/comments"):
            review_id = int(path.split("/")[-2]) if "/reviews/" in path else 0
            return reply(self.review_roots.get(review_id, []))
        if method == "POST" and path.endswith("/reviews"):
            if self.review_failure is not None:
                raise self.review_failure
            review_id = 4242 + len(self.review_records)
            self.review_records.append({"id": review_id, **payload, "user": {"login": "auto-coder-reviewer[bot]"}})
            self.review_roots[review_id] = [{"id": self.next_root_id + index, "body": comment["body"], "user": {"login": "auto-coder-reviewer[bot]"}} for index, comment in enumerate(payload.get("comments", []))]
            self.next_root_id += len(self.review_roots[review_id])
            return reply({"id": review_id}, 200)
        return reply([])

    @staticmethod
    def _pr(path: str) -> str:
        parts = path.rstrip("/").split("/")
        return parts[-1] if parts[-2:-1] == ["pulls"] else ""


def reviewer_for(tmp: Path, monkeypatch: pytest.MonkeyPatch, client: RouterClient) -> GitHubAppReviewer:
    key = tmp / "reviewer.pem"
    key.write_text("fake", encoding="utf-8")
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *a, **k: "jwt")
    return GitHubAppReviewer(ReviewerAppConfig("4765828", "client", key), api_url="https://api.github.test", client=client, clock=lambda: 1000.0)


def passing_result(attempt_sequence: int = 1) -> AdversarialValidationResult:
    return AdversarialValidationResult(
        result="PASS",
        summary="All requirement coverage is verified.",
        requirement_coverage=[RequirementCoverageEntry(requirement_id="REQ-001", status="VERIFIED", evidence="The guard enforces the requirement.")],
        attempt_id=f"attempt-{attempt_sequence}",
        attempt_sequence=attempt_sequence,
    )


def authority_for(env: Env, pr: int, *, raw: AdversarialValidationResult, bridge: Optional[AcceptedFindingBridge] = None, attempts: Optional[AdversarialValidationAttemptRepository] = None, sequence: int = 0) -> tuple[ApprovalAuthority, AcceptedFindingProjection]:
    bridge = bridge or env.bridge()
    target = env.target(pr)
    projection = bridge.project(target)
    application = derive_application(raw, projection)
    attempts = attempts or AdversarialValidationAttemptRepository(REPO)
    return (
        ApprovalAuthority(bridge=bridge, target=target, observe_roots=lambda: None, raw_result=raw, decision=application.decision, attempts=attempts, attempt_sequence=sequence, head_sha=env.head),
        projection,
    )


# -- REQ-001 / REQ-004: raw PASS cannot survive an upheld accepted finding ---


@pytest.mark.parametrize("pr", [5438, 2917])
@pytest.mark.parametrize("gap", [True, False])
def test_raw_pass_with_upheld_accepted_finding_becomes_actionable_and_references_existing_blocker(env: Env, monkeypatch: pytest.MonkeyPatch, pr: int, gap: bool) -> None:
    """Strong finding accepted, root published, ordinary model says PASS + STILL_VALID (any TOG form)."""
    save_empty_session(env, pr)
    inputs = accept_strong(env, pr, [finding_json("finding-a", gap=gap)])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    observation = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 5391105725})
    record = _only(env.bridge().project(env.target(pr), observation), "finding-a")
    root_body = observation.roots[0].body

    result, _ = run_ordinary(env, pr, ordinary_pass_response("PRRT_5438"), claimed=(still_valid_thread("PRRT_5438", 5391105725, root_body),), bridge=env.bridge())
    projection = result.accepted_finding_projection
    assert projection is not None
    observed = _only(projection, "finding-a")
    assert [outcome.outcome for outcome in observed.current_observations] == [OUTCOME_STILL_VALID_OBSERVED]

    application = derive_application(result, projection)
    expected = "NEEDS_TESTS" if gap else "NEEDS_FIX"
    assert application.decision.status == expected and not application.decision.approval_eligible
    assert application.decision.blocker_ids == (record.canonical_blocker_id,)
    assert application.decision.corrections[0].qualified_requirements == ("#2401/REQ-001",)
    assert application.decision.corrections[0].source_identity == record.source_identity
    effective = application.result
    assert effective.result == expected and effective.raw_model_result == result.result
    assert not effective.is_pass and not effective.allows_auto_merge
    if gap:
        assert [item.gap_id for item in effective.open_test_oracle_gaps] == [record.known_gap_id] and not effective.findings
    else:
        assert [item.finding_identity for item in effective.findings] == [record.source_identity]
        assert effective.findings[0].correction_identity == record.canonical_blocker_id
    # the model's verdict is untouched evidence; applying a decision never mutates it
    assert result.result == "PASS" or result.result == "NEEDS_TESTS"
    assert len(env.ledger.get_snapshot("https://api.github.com", REPO, pr).blockers) == 1


def test_decision_application_does_not_mutate_its_input_and_is_stable_for_rederivation() -> None:
    raw = passing_result()
    projection = AcceptedFindingProjection(complete=False)
    decision = derive_effective_review_decision(raw, projection)
    effective = apply_effective_decision(raw, decision, projection)
    assert effective.result == "BLOCKED" and effective.raw_model_result == "PASS" and raw.result == "PASS" and raw.raw_model_result == ""
    again = apply_effective_decision(effective, derive_effective_review_decision(raw, projection), projection)
    assert again.raw_model_result == "PASS"
    assert not raw_ordinary_clear(effective)
    assert raw_ordinary_clear(raw)


# -- REQ-003 / REQ-010: pre-send authority ----------------------------------


def test_new_accepted_finding_after_derivation_refuses_approval_before_any_request(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 4101
    save_empty_session(env, pr)
    raw = passing_result()
    authority, projection = authority_for(env, pr, raw=raw)
    assert projection.complete and authority() == ""  # no accepted state: approval is eligible

    accept_strong(env, pr, [finding_json("finding-new")])  # another participant commits a relevant blocker
    reason = authority()
    assert "BLOCKED" in reason or "NEEDS" in reason or "changed" in reason

    client = RouterClient(env.head)
    reviewer = reviewer_for(env.tmp, monkeypatch, client)
    result = reviewer.publish(REPO, pr, env.head, raw, approval_authority=authority)
    assert not result.success and result.policy_refusal and result.event == "APPROVE"
    assert client.posted_reviews == []  # the old APPROVE never reached the endpoint


def test_refusal_is_distinguished_from_transport_failure_and_non_approval_is_not_gated(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 4102
    calls: list[str] = []

    def refuse() -> str:
        calls.append("checked")
        return "refused by policy"

    client = RouterClient(env.head)
    reviewer = reviewer_for(env.tmp, monkeypatch, client)
    raw = passing_result()
    refused = reviewer.publish(REPO, pr, env.head, raw, approval_authority=refuse)
    assert refused.policy_refusal and refused.reason == "refused by policy" and client.posted_reviews == []

    request = httpx.Request("POST", "https://api.github.test/x")
    client.review_failure = httpx.ConnectError("connection lost", request=request)
    failed = reviewer.publish(REPO, pr, env.head, raw, approval_authority=lambda: "")
    assert not failed.success and not failed.policy_refusal and len(client.posted_reviews) == 1  # the request was transmitted, so its outcome is ambiguous

    client.review_failure = None
    comment_result = AdversarialValidationResult(result="BLOCKED", summary="Reconciliation required", attempt_id="a", attempt_sequence=1)
    published = reviewer.publish(REPO, pr, env.head, comment_result, approval_authority=refuse)
    assert published.success and published.event == "COMMENT" and calls == ["checked"]  # only APPROVE consults the authority

    approved = reviewer.publish(REPO, pr, env.head, raw, approval_authority=lambda: "")
    assert approved.success and approved.event == "APPROVE" and [post["event"] for post in client.posted_reviews] == ["APPROVE", "COMMENT", "APPROVE"]


def test_unavailable_authority_check_fails_closed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> str:
        raise RuntimeError("store offline")

    client = RouterClient(env.head)
    result = reviewer_for(env.tmp, monkeypatch, client).publish(REPO, 4103, env.head, passing_result(), approval_authority=broken)
    assert result.policy_refusal and "could not be confirmed" in result.reason and client.posted_reviews == []


def test_newer_registered_attempt_removes_old_decisions_authority_and_fresh_read_cannot_restore_it(env: Env) -> None:
    pr = 4104
    attempts = AdversarialValidationAttemptRepository(REPO)
    old = attempts.start(pr, env.head)
    authority, _ = authority_for(env, pr, raw=passing_result(old.sequence), attempts=attempts, sequence=old.sequence)
    assert authority() == ""
    newer = attempts.start(pr, env.head)  # pending or even failed later: registration alone supersedes
    assert newer.sequence > old.sequence
    assert "newer applicable validation attempt" in authority()
    attempts.finish(newer.attempt_id, "ERROR")
    assert "newer applicable validation attempt" in authority()  # a failed newer attempt does not hand authority back


def test_unreadable_accepted_state_refuses_approval(env: Env) -> None:
    pr = 4105
    raw = passing_result()
    bridge = env.bridge()
    authority, _ = authority_for(env, pr, raw=raw, bridge=bridge)
    assert authority() == ""
    env.cycle.storage_path.parent.mkdir(parents=True, exist_ok=True)
    env.cycle.storage_path.write_text("{ not json", encoding="utf-8")
    assert "unavailable" in authority() or "incomplete" in authority()


def test_open_accepted_finding_without_current_adjudication_prevents_approval(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 4106
    save_empty_session(env, pr)
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    observation = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 31})
    raw = passing_result()
    bridge = env.bridge()
    authority = ApprovalAuthority(
        bridge=bridge, target=env.target(pr), observe_roots=lambda: observation, raw_result=raw, decision=derive_effective_review_decision(raw, bridge.project(env.target(pr), observation)), attempts=AdversarialValidationAttemptRepository(REPO), attempt_sequence=0, head_sha=env.head
    )
    reason = authority()
    assert "NEEDS_TESTS" in reason  # an open accepted finding is never approval-eligible


# -- REQ-002 / REQ-007: saved verdict handling and retained reconciliation ---


def test_saved_pass_is_clearance_only_for_readable_closed_state(env: Env) -> None:
    pr = 4201
    assert saved_pass_is_clearance(env.bridge().project(env.target(pr)))  # successful empty read
    assert not saved_pass_is_clearance(AcceptedFindingProjection(complete=False))
    save_empty_session(env, pr)
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    open_projection = env.bridge().project(env.target(pr))
    assert not saved_pass_is_clearance(open_projection)
    h2 = _commit(env.worktree, "repair")
    _close(env, pr, inputs, h2, {"finding-a": "FIXED"})
    assert saved_pass_is_clearance(env.bridge().project(env.target(pr, h2)))


def test_evidence_revision_ignores_head_adjudication_but_tracks_authority_and_association(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 4202
    save_empty_session(env, pr)
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    bridge = env.bridge()
    first = evidence_revision(bridge.project(env.target(pr)))
    assert evidence_revision(bridge.project(env.target(pr))) == first  # idempotent reads share a revision
    assert evidence_revision(bridge.project(env.target(pr, _commit(env.worktree, "other-head")))) == first

    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    observation = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 777})
    associated = evidence_revision(bridge.project(env.target(pr), observation))
    assert associated != first  # new exact association evidence makes processing eligible again
    assert evidence_revision(AcceptedFindingProjection(complete=False)) != first


def test_store_retains_decision_attempts_and_fails_closed_on_unconfirmed_write(env: Env) -> None:
    pr = 4301
    store = EffectiveDecisionStore(REPO)
    assert store.load(pr) is None
    incomplete = AcceptedFindingProjection(complete=False, target=env.target(pr))
    raw = passing_result()
    decision = derive_effective_review_decision(raw, incomplete)
    assert decision.next_action is EffectiveNextAction.RECONCILIATION
    record = build_retained_record(decision, incomplete, env.head, env.base)
    stored = store.retain(record)
    assert stored.reconciliation_attempts == [record.evidence_revision] and stored.is_reconciliation_wait
    assert store.retain(record).reconciliation_attempts == [record.evidence_revision]  # one attempt per revision

    reopened = EffectiveDecisionStore(REPO).load(pr)
    assert reopened is not None and reopened.status == "BLOCKED" and reopened.head_sha == env.head and reopened.wait_reason

    next_head = build_retained_record(decision, incomplete, "f" * 40, env.base)
    assert store.retain(next_head).reconciliation_attempts == [next_head.evidence_revision]  # a new target starts a new attempt budget

    store.storage_path.write_text("[not-a-mapping]", encoding="utf-8")
    with pytest.raises(DecisionRetentionError):
        store.load(pr)
    with pytest.raises(DecisionRetentionError):
        store.retain(record)


def test_store_write_failure_is_reported_not_swallowed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EffectiveDecisionStore(REPO)
    record = build_retained_record(derive_effective_review_decision(passing_result(), AcceptedFindingProjection(complete=True, target=env.target(1))), AcceptedFindingProjection(complete=True, target=env.target(1)), env.head, env.base)

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(EffectiveDecisionStore, "_write", fail_replace)
    with pytest.raises(DecisionRetentionError, match="could not be retained"):
        store.retain(record)
    assert EffectiveDecisionStore(REPO).load(1) is None  # nothing durable, so no dependent effect may run


# -- REQ-005 / REQ-006: handoff classification and closure eligibility ------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CloudReviewRepairResult(["Sent adversarial NEEDS_FIX report to codex-cloud task 't' for PR #1"], delivered=True), (HANDOFF_DISPATCHED, "")),
        (CloudReviewRepairResult(["Skipped duplicate adversarial feedback to jules for PR #1: all actionable feedback was already delivered"], delivered=True), (HANDOFF_DISPATCHED, "")),
        (CloudReviewRepairResult(["Local review correction for PR #1 is awaiting_validation: x"], deferred=True, route_disposition="LOCAL_EXECUTION", local_phase="awaiting_validation"), (HANDOFF_DISPATCHED, "")),
        (CloudReviewRepairResult(["Local review repair was not admitted"], route_disposition="LOCAL_EXECUTION", local_phase="not_admitted"), (HANDOFF_WAITING, WAIT_ROUTE_UNAVAILABLE)),
        (CloudReviewRepairResult(["not delivered: no origin"], wait_reason=WAIT_ROUTE_UNAVAILABLE), (HANDOFF_WAITING, WAIT_ROUTE_UNAVAILABLE)),
        (CloudReviewRepairResult(["not delivered: exhausted"], wait_reason=WAIT_ALLOWANCE_EXHAUSTED), (HANDOFF_WAITING, WAIT_ALLOWANCE_EXHAUSTED)),
        (CloudReviewRepairResult(["duplicate suppressed"], wait_reason=WAIT_INDETERMINATE), (HANDOFF_WAITING, WAIT_INDETERMINATE)),
        (["Adversarial feedback was not delivered for PR #1: automatic repair allowance is exhausted for open blocker(s): b"], (HANDOFF_WAITING, WAIT_ALLOWANCE_EXHAUSTED)),
    ],
)
def test_handoff_states_are_distinct_and_never_conflate_waiting_with_delivery(result: list[str], expected: tuple[str, str]) -> None:
    assert classify_repair_handoff(result) == expected


def test_quota_deferral_is_a_distinct_wait() -> None:
    from auto_coder.pr_processor import PRActionList

    deferred = PRActionList(["DEFERRED Claude adversarial feedback for PR #1 until 100"])
    deferred.quota_deferred = True
    assert classify_repair_handoff(deferred) == (HANDOFF_WAITING, WAIT_QUOTA)


def test_closure_is_ready_without_effective_pass_only_when_every_outstanding_finding_is_unupheld(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = 4401
    save_empty_session(env, pr)
    inputs = accept_strong(env, pr, [finding_json("finding-a")])
    strong = env.cycle.snapshot(pr).accepted_strong_round
    assert strong is not None
    inputs.gate.state.acknowledge_publication(pr, strong.round_id)
    observation = published_roots(env, pr, monkeypatch, root_ids={"finding-a": 41})
    bridge = env.bridge()
    projection = bridge.project(env.target(pr), observation)
    assert not closure_ready(projection, env.head)  # audited head: no corrective generation yet
    assert closure_ready(projection, env.head, "local:completed_no_change")  # supported no-change completion
    h2 = _commit(env.worktree, "repair-head")
    repaired = bridge.project(env.target(pr, h2), observation)
    assert closure_ready(repaired, h2)
    upheld = bridge.project(env.target(pr, h2), observation, [OrdinaryDisposition(status="STILL_VALID", rationale="r", evidence="src/state.py:40 still drops state", root_comment_id=41)])
    assert not closure_ready(upheld, h2)
    assert not closure_ready(AcceptedFindingProjection(complete=False), h2)
