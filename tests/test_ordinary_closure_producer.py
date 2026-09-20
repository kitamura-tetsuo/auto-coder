import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_coder.cli_helpers import AdversarialValidationAvailability
from auto_coder.pr_processor import TwoTierGateInputs, _execute_pending_ordinary_closure
from auto_coder.pr_review_cycle import ContractSnapshot, Finding, RoundProvenance, StrongPolicyIdentity
from auto_coder.pr_review_execution import FindingDisposition, ReviewExecutionResult, ReviewMode, ScopeAssessment
from auto_coder.two_tier_pr_gate import TwoTierPrGate
from auto_coder.utils import CommandResult


def _finding(claim_id: str) -> Finding:
    return Finding(
        finding_id="finding-a",
        origin_round_id=claim_id,
        requirement_ids=("#2210/REQ-005",),
        requirement_texts=("Independently assess every finding.",),
        counterexample="The second path remains broken.",
        expected_behavior="Both paths preserve the invariant.",
        actual_behavior="The second path loses state.",
        evidence="src/state.py:40",
        affected_boundary="delete_two",
        focused_regression_scenario="Exercise both paths.",
    )


def test_production_ordinary_closure_uses_cumulative_diff_and_accepts_bounded_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    contract = ContractSnapshot(("#2210",), "Issue #2210 REQ-005: Independently assess every finding.")
    policy = StrongPolicyIdentity("backend_strong_pr_adversarial_validation", "model=strong", "v1")
    gate = TwoTierPrGate("owner/repo")
    base, audited, repaired = "b" * 40, "a" * 40, "c" * 40
    gate.ordinary_pass(22, audited, base, contract)
    claim = gate.state.claim_strong_audit(22, RoundProvenance(audited, base), contract, policy)
    strong_round = gate.state.record_strong_result(22, claim.claim_id, "FINDINGS", "strong/model", [_finding(claim.claim_id)])
    gate.ordinary_pass(22, repaired, base, contract)
    inputs = TwoTierGateInputs(gate, contract, policy, repaired, base)
    manager = MagicMock()

    @contextlib.contextmanager
    def worktree(*args, **kwargs):
        yield str(tmp_path)

    def result(review_input, backend_manager, execution_cwd):
        assert review_input.mode is ReviewMode.ORDINARY_CLOSURE
        assert review_input.audited_head_sha == audited
        assert review_input.head_sha == repaired
        assert review_input.finding_set_revision == 1
        assert tuple(item.finding_id for item in review_input.findings) == ("finding-a",)
        assert "full cumulative repair" in review_input.diff_evidence
        return ReviewExecutionResult(
            mode=ReviewMode.ORDINARY_CLOSURE,
            round_id=strong_round.round_id,
            attempt_id=review_input.attempt_id,
            head_sha=repaired,
            base_sha=base,
            contract_identity=contract.identity,
            policy_identity=policy.identity,
            finding_set_revision=1,
            reviewer_provenance="ordinary/model",
            verdict="PASS",
            dispositions=(FindingDisposition("finding-a", "FIXED", "Both paths now preserve state."),),
            scope=ScopeAssessment.BOUNDED,
            scope_evidence="Only the repair and its regression changed.",
        )

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch(
            "auto_coder.cli_helpers.resolve_adversarial_validation_availability",
            return_value=AdversarialValidationAvailability(backend_manager=manager),
        ) as availability,
        patch(
            "auto_coder.pr_processor.CommandExecutor.run_command",
            side_effect=[
                CommandResult(True, "full cumulative repair", "", 0),
                CommandResult(True, "src/state.py\ntests/test_state.py\n", "", 0),
            ],
        ) as command,
        patch("auto_coder.pr_processor.execute_review", side_effect=result) as transport,
    ):
        accepted, reason, reviewer_backend = _execute_pending_ordinary_closure("owner/repo", 22, inputs)

    assert accepted is True
    assert reason == "accepted bounded ordinary closure from ordinary/model; publication remains pending"
    assert reviewer_backend == "ordinary/model"
    availability.assert_called_once_with("pr", execution_cwd=str(tmp_path))
    assert command.call_args_list[0].args[0][-2:] == [audited, repaired]
    transport.assert_called_once()
    snapshot = gate.state.snapshot(22)
    assert snapshot.accepted_closure is not None
    assert snapshot.accepted_closure.head_sha == repaired
    assert snapshot.open_findings == ()
