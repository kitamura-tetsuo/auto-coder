import json
from dataclasses import replace

import pytest

from auto_coder import pr_processor
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_review_cycle import (
    PUBLICATION_ACKNOWLEDGED,
    ContractSnapshot,
    Finding,
    PrReviewCycleRepository,
    RoundProvenance,
    StrongAuditRound,
    StrongPolicyIdentity,
)
from auto_coder.pr_review_effects import (
    CONFIRMED,
    REJECTED,
    RESERVED,
    UNCERTAIN,
    AcceptedReviewPayload,
    EffectAttempt,
    ReviewEffectExecutor,
    ReviewEffectRepository,
)
from auto_coder.two_tier_pr_gate import TwoTierPrGate


def _payload() -> AcceptedReviewPayload:
    contract = ContractSnapshot(("#2209",), "REQ-001: Preserve the accepted payload.")
    policy = StrongPolicyIdentity("strong-route", '{"model":"reviewer"}', "v1")
    finding = Finding(
        "finding-1",
        "claim-1",
        ("#2209/REQ-002",),
        ("Publish every finding.",),
        "An unanchored finding disappears.",
        "The finding remains portable.",
        "The finding was omitted.",
        "Production publication payload lacks it.",
        "review publication",
        True,
        "Drop findings without a line number.",
        "Only anchored findings are asserted.",
        "A material defect is hidden.",
        "Assert the unanchored provider payload.",
    )
    round_record = StrongAuditRound(
        "round-1",
        3,
        "head-a",
        "base-a",
        contract.identity,
        policy.identity,
        contract,
        policy,
        "claim-1",
        2,
        "reviewer/model",
        "FINDINGS",
        (finding.finding_id,),
        7,
    )
    return AcceptedReviewPayload.strong("owner/repo", 42, round_record, (finding,))


class _Transport:
    def __init__(self, send: EffectAttempt, reconcile: EffectAttempt = EffectAttempt(UNCERTAIN)):
        self.send_result = send
        self.reconcile_result = reconcile
        self.sent = 0
        self.reconciled = 0

    def send(self, operation):
        self.sent += 1
        return self.send_result

    def reconcile(self, operation):
        self.reconciled += 1
        return self.reconcile_result


def test_exact_payload_and_distinct_attempt_identity_are_durable(tmp_path):
    repository = ReviewEffectRepository("owner/repo", tmp_path / "effects.json")
    payload = _payload()
    operation = repository.reserve(payload, "publication", "github-reviewer-app", "worker-a")

    persisted = json.loads(operation.exact_payload)
    assert operation.status == RESERVED
    assert persisted["mode"] == "STRONG_AUDIT"
    assert persisted["round_id"] == "round-1"
    assert persisted["contract_issue_ids"] == ["#2209"]
    assert persisted["requirements_text"] == "REQ-001: Preserve the accepted payload."
    assert persisted["findings"][0]["focused_regression_scenario"] == "Assert the unanchored provider payload."

    different = AcceptedReviewPayload(**{**payload.__dict__, "round_id": "round-2"})
    assert repository.operation_identity(payload, "publication", "github-reviewer-app") != repository.operation_identity(different, "publication", "github-reviewer-app")


def test_rendered_review_exposes_findings_before_collapsed_payload():
    payload = _payload()
    body = pr_processor._render_two_tier_review(payload)
    visible = body.split("<details>", 1)[0]
    assert "### 1. finding-1" in visible
    assert "**Requirements:** #2209/REQ-002" in visible
    assert "**Status:** OPEN" in visible
    for field in (
        "counterexample",
        "expected_behavior",
        "actual_behavior",
        "evidence",
        "affected_boundary",
        "material_consequence",
        "focused_regression_scenario",
        "plausible_incorrect_implementation",
        "why_tests_admit_it",
    ):
        assert getattr(payload.findings[0], field) in visible
    assert payload.canonical_json() in body
    assert f"auto-coder-two-tier-review:v1:{payload.identity}" in body


def test_rendered_closure_shows_disposition_evidence():
    original = _payload()
    finding = replace(original.findings[0], status="FIXED", disposition_evidence="Both paths now enforce the guard.")
    payload = replace(original, mode="ORDINARY_CLOSURE", verdict="CLOSURE", findings=(finding,))
    visible = pr_processor._render_two_tier_review(payload).split("<details>", 1)[0]
    assert "**Status:** FIXED" in visible
    assert "**Disposition evidence:** Both paths now enforce the guard." in visible


def test_contention_allows_only_reservation_owner_to_send(tmp_path):
    repository = ReviewEffectRepository("owner/repo", tmp_path / "effects.json")
    payload = _payload()
    first_transport = _Transport(EffectAttempt(CONFIRMED, "review:99"))
    second_transport = _Transport(EffectAttempt(CONFIRMED, "review:100"))
    first = ReviewEffectExecutor(repository, "worker-a")
    second = ReviewEffectExecutor(repository, "worker-b")

    reservation = repository.reserve(payload, "publication", "github-reviewer-app", "worker-a")
    contested = second.apply(payload, "publication", "github-reviewer-app", second_transport, lambda: True)
    confirmed = first.apply(payload, "publication", "github-reviewer-app", first_transport, lambda: True)

    assert contested.operation_id == reservation.operation_id
    assert contested.status == RESERVED
    assert second_transport.sent == 0
    assert confirmed.status == CONFIRMED
    assert confirmed.receipt == "review:99"
    assert first_transport.sent == 1


def test_uncertain_effect_reconciles_without_resend_and_survives_restart(tmp_path):
    path = tmp_path / "effects.json"
    payload = _payload()
    repository = ReviewEffectRepository("owner/repo", path)
    executor = ReviewEffectExecutor(repository, "stable-worker")
    lost_response = _Transport(EffectAttempt(UNCERTAIN, reason="response lost"))

    uncertain = executor.apply(payload, "repair-handoff", "codex-cloud:task-1", lost_response, lambda: True)
    assert uncertain.status == UNCERTAIN
    assert lost_response.sent == 1

    recovered_repository = ReviewEffectRepository("owner/repo", path)
    recovered = ReviewEffectExecutor(recovered_repository, "stable-worker")
    transport = _Transport(EffectAttempt(REJECTED), EffectAttempt(CONFIRMED, "turn:abc"))
    confirmed = recovered.apply(payload, "repair-handoff", "codex-cloud:task-1", transport, lambda: True)

    assert confirmed.status == CONFIRMED
    assert confirmed.receipt == "turn:abc"
    assert transport.sent == 0
    assert transport.reconciled == 1


def test_stale_authority_rejects_before_mutation_and_positive_rejection_can_retry(tmp_path):
    repository = ReviewEffectRepository("owner/repo", tmp_path / "effects.json")
    payload = _payload()
    executor = ReviewEffectExecutor(repository, "worker-a")
    transport = _Transport(EffectAttempt(CONFIRMED, "must-not-send"))

    rejected = executor.apply(payload, "publication", "github-reviewer-app", transport, lambda: False)
    assert rejected.status == REJECTED
    assert transport.sent == 0

    retry_transport = _Transport(EffectAttempt(CONFIRMED, "review:101"))
    confirmed = executor.apply(payload, "publication", "github-reviewer-app", retry_transport, lambda: True)
    assert confirmed.status == CONFIRMED
    assert retry_transport.sent == 1


@pytest.mark.parametrize("verdict", ["PASS", "FINDINGS"])
def test_production_consumer_publishes_exact_accepted_record_and_persists_receipt(tmp_path, monkeypatch, verdict):
    monkeypatch.setenv("HOME", str(tmp_path))
    contract = ContractSnapshot(("#2209",), "REQ-001: Preserve the accepted payload.")
    policy = StrongPolicyIdentity("strong-route", "options", "v1")
    cycle = PrReviewCycleRepository("owner/repo", tmp_path / "cycle.json")
    cycle.record_ordinary_pass(42, RoundProvenance("head-a", "base-a"), contract)
    claim = cycle.claim_strong_audit(42, RoundProvenance("head-a", "base-a"), contract, policy)
    findings = [replace(_payload().findings[0], origin_round_id=claim.claim_id)] if verdict == "FINDINGS" else []
    accepted = cycle.record_strong_result(42, claim.claim_id, verdict, "reviewer/model", findings)
    if findings:
        with pytest.raises(ValueError, match="complete accepted finding bundle"):
            AcceptedReviewPayload.strong("owner/repo", 42, accepted, ())
    inputs = pr_processor.TwoTierGateInputs(TwoTierPrGate("owner/repo", cycle), contract, policy, "head-a", "base-a")
    sent_bodies = []

    class FakeReviewer:
        def __init__(self, config):
            pass

        def publish_exact_pr_review(self, repository, pr_number, head_sha, body, authorize):
            assert (repository, pr_number, head_sha) == ("owner/repo", 42, "head-a")
            assert authorize() is True
            sent_bodies.append(body)
            return ReviewPublicationResult(True, "987", "")

        def find_exact_pr_review(self, repository, pr_number, head_sha, body):
            raise AssertionError("a new reserved operation must send before reconciliation")

    monkeypatch.setattr(pr_processor, "load_reviewer_app_config", lambda repo_name: object())
    monkeypatch.setattr(pr_processor, "GitHubAppReviewer", FakeReviewer)

    published, reason = pr_processor._consume_pending_two_tier_publication("owner/repo", 42, inputs)

    assert published is True
    assert reason == "confirmed authenticated STRONG_AUDIT publication receipt 987"
    assert len(sent_bodies) == 1
    assert f"auto-coder-two-tier-review:v1:" in sent_bodies[0]
    assert '"requirements_text":"REQ-001: Preserve the accepted payload."' in sent_bodies[0]
    published_payload = json.loads(sent_bodies[0].split("```json\n", 1)[1].split("\n```", 1)[0])
    assert published_payload["verdict"] == verdict
    assert [item["finding_id"] for item in published_payload["findings"]] == (["finding-1"] if verdict == "FINDINGS" else [])
    if findings:
        assert published_payload["findings"][0]["origin_round_id"] == claim.claim_id
        assert published_payload["findings"][0]["focused_regression_scenario"] == "Assert the unanchored provider payload."
    snapshot = cycle.snapshot(42)
    assert snapshot.accepted_strong_round is not None
    assert snapshot.accepted_strong_round.round_id == accepted.round_id
    assert snapshot.accepted_strong_round.publication_status == PUBLICATION_ACKNOWLEDGED
    operations = json.loads((tmp_path / ".auto-coder" / "owner/repo" / "pr_review_effects.json").read_text())["operations"]
    assert list(operations.values())[0]["receipt"] == "987"
    assert list(operations.values())[0]["status"] == CONFIRMED
