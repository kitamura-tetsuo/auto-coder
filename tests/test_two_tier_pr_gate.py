from auto_coder.pr_review_cycle import (
    VERDICT_PASS,
    ContractSnapshot,
    PrReviewCycleRepository,
    RoundProvenance,
    StrongPolicyIdentity,
)
from auto_coder.two_tier_pr_gate import TwoTierPrGate


def _contract(text: str = "REQ-001: preserve the gate") -> ContractSnapshot:
    return ContractSnapshot(("#2102",), text)


def _policy(model: str = "strong-1") -> StrongPolicyIdentity:
    return StrongPolicyIdentity("backend_strong_pr_adversarial_validation", model, "v1")


def test_ordinary_pass_is_pending_and_cannot_authorize_merge(tmp_path) -> None:
    repository = PrReviewCycleRepository("owner/repo", tmp_path / "cycle.json")
    gate = TwoTierPrGate("owner/repo", repository)
    gate.ordinary_pass(7, "head", "base", _contract())

    assert not gate.authorize_merge(
        7,
        current_head_sha="head",
        current_base_sha="base",
        current_contract=_contract(),
        current_policy=_policy(),
    )
    diagnostic = gate.diagnostic(7, current_head_sha="head", backend="codex/strong-1")
    assert diagnostic.phase == "STRONG_PENDING"
    assert diagnostic.waiting_reason == "ordinary PASS recorded; strong audit required"


def test_published_strong_pass_authorizes_only_exact_snapshot(tmp_path) -> None:
    repository = PrReviewCycleRepository("owner/repo", tmp_path / "cycle.json")
    gate = TwoTierPrGate("owner/repo", repository)
    contract, policy = _contract(), _policy()
    provenance = RoundProvenance("head", "base")
    gate.ordinary_pass(7, "head", "base", contract)
    claim = repository.claim_strong_audit(7, provenance, contract, policy)
    round_record = repository.record_strong_result(7, claim.claim_id, VERDICT_PASS, "codex/strong-1")
    repository.acknowledge_publication(7, round_record.round_id)
    repository.accept_strong_pass_completion(7, round_record.round_id)

    assert gate.authorize_merge(7, current_head_sha="head", current_base_sha="base", current_contract=contract, current_policy=policy)
    assert not gate.authorize_merge(7, current_head_sha="new-head", current_base_sha="base", current_contract=contract, current_policy=policy)
    assert not gate.authorize_merge(7, current_head_sha="head", current_base_sha="base", current_contract=_contract("REQ-001: changed"), current_policy=policy)
    assert not gate.authorize_merge(7, current_head_sha="head", current_base_sha="base", current_contract=contract, current_policy=_policy("strong-2"))

    diagnostic = gate.diagnostic(7, current_head_sha="head", backend="codex/strong-1")
    assert diagnostic.completion_basis == "STRONG_PASS"
    assert diagnostic.audited_head == "head"
    assert diagnostic.outstanding_finding_ids == ()
