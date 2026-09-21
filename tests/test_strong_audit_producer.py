import contextlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cli_helpers import AdversarialValidationAvailability
from auto_coder.codex_usage_checker import CodexWeeklyUsage
from auto_coder.llm_backend_config import LLMBackendConfiguration
from auto_coder.pr_processor import TwoTierGateInputs, _execute_pending_strong_audit
from auto_coder.pr_review_cycle import VERDICT_PASS, ContractSnapshot, RoundProvenance, StrongPolicyIdentity
from auto_coder.pr_review_execution import ReviewExecutionResult, ReviewMode
from auto_coder.two_tier_pr_gate import TwoTierPrGate
from auto_coder.utils import CommandResult


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "contract.txt").write_text("base\n")
    _git(repository, "add", "contract.txt")
    _git(repository, "commit", "-m", "base")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "contract.txt").write_text("head\n")
    _git(repository, "commit", "-am", "head")
    return repository, base, _git(repository, "rev-parse", "HEAD")


def _inputs(tmp_path: Path, base: str, head: str) -> TwoTierGateInputs:
    return TwoTierGateInputs(
        gate=TwoTierPrGate("owner/repo"),
        contract=ContractSnapshot(("#2208",), "Issue #2208 REQ-001: Run the audit."),
        policy=StrongPolicyIdentity("backend_strong_pr_adversarial_validation", "model=strong", "v1"),
        head_sha=head,
        base_sha=base,
    )


def test_pending_strong_audit_calls_transport_once_and_durably_accepts(tmp_path: Path, monkeypatch) -> None:
    repository, base, head = _repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    inputs.gate.ordinary_pass(12, head, base, inputs.contract)
    manager = MagicMock()

    @contextlib.contextmanager
    def worktree(*args, **kwargs):
        yield str(repository)

    def result(review_input, backend_manager, execution_cwd):
        assert review_input.mode is ReviewMode.STRONG_AUDIT
        assert review_input.head_sha == head
        assert review_input.base_sha == base
        assert "contract.txt" in review_input.repository_evidence
        assert "-base" in review_input.diff_evidence
        assert "+head" in review_input.diff_evidence
        assert backend_manager is manager
        assert execution_cwd == str(repository)
        return ReviewExecutionResult(
            mode=ReviewMode.STRONG_AUDIT,
            round_id=review_input.round_id,
            attempt_id=review_input.attempt_id,
            head_sha=head,
            base_sha=base,
            contract_identity=inputs.contract.identity,
            policy_identity=inputs.policy.identity,
            finding_set_revision=0,
            reviewer_provenance="strong/codex/model",
            verdict="PASS",
        )

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch(
            "auto_coder.cli_helpers.resolve_adversarial_validation_availability",
            return_value=AdversarialValidationAvailability(backend_manager=manager),
        ),
        patch(
            "auto_coder.pr_processor.CommandExecutor.run_command",
            side_effect=[
                CommandResult(True, "-base\n+head\n", "", 0),
                CommandResult(True, "contract.txt\n", "", 0),
            ],
        ),
        patch("auto_coder.pr_processor.execute_review", side_effect=result) as transport,
    ):
        accepted, reason = _execute_pending_strong_audit("owner/repo", 12, inputs)
        accepted_again, reason_again = _execute_pending_strong_audit("owner/repo", 12, inputs)

    assert accepted is True
    assert "publication remains pending" in reason
    assert accepted_again is False
    assert reason_again == "Accepted strong result awaits publication acknowledgement"
    transport.assert_called_once()
    reconstructed = TwoTierGateInputs(TwoTierPrGate("owner/repo"), inputs.contract, inputs.policy, head, base)
    snapshot = reconstructed.gate.state.snapshot(12)
    assert snapshot.accepted_strong_round is not None
    assert snapshot.accepted_strong_round.verdict == "PASS"
    assert snapshot.phase == "STRONG_PENDING"


def test_unavailable_strong_route_releases_claim_without_acceptance(tmp_path: Path, monkeypatch) -> None:
    repository, base, head = _repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    inputs.gate.ordinary_pass(13, head, base, inputs.contract)

    @contextlib.contextmanager
    def worktree(*args, **kwargs):
        yield str(repository)

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch(
            "auto_coder.cli_helpers.resolve_adversarial_validation_availability",
            return_value=AdversarialValidationAvailability(),
        ),
        patch("auto_coder.pr_processor.execute_review") as transport,
    ):
        accepted, reason = _execute_pending_strong_audit("owner/repo", 13, inputs)

    assert accepted is False
    assert reason == "strong reviewer route is UNAVAILABLE"
    transport.assert_not_called()
    snapshot = inputs.gate.state.snapshot(13)
    assert snapshot.active_claim is None
    assert snapshot.accepted_strong_round is None
    assert snapshot.waiting_reason == "strong reviewer route is UNAVAILABLE"


def test_completed_strong_audit_is_reused_without_transport_after_restart(tmp_path: Path, monkeypatch) -> None:
    repository, base, head = _repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    inputs.gate.ordinary_pass(14, head, base, inputs.contract)
    claim = inputs.gate.state.claim_strong_audit(
        14,
        provenance=RoundProvenance(head, base),
        contract=inputs.contract,
        policy=inputs.policy,
    )
    round0 = inputs.gate.state.record_strong_result(14, claim.claim_id, VERDICT_PASS, "strong/codex/model")
    inputs.gate.state.acknowledge_publication(14, round0.round_id)
    completed = inputs.gate.state.accept_strong_pass_completion(14, round0.round_id)
    reconstructed = _inputs(tmp_path, base, head)

    with patch("auto_coder.pr_processor.execute_review") as transport:
        accepted, reason = _execute_pending_strong_audit("owner/repo", 14, reconstructed)

    assert accepted is False
    assert reason == "Applicable STRONG_PASS completion already exists"
    transport.assert_not_called()
    after = reconstructed.gate.state.snapshot(14)
    assert after.completion == completed.completion
    assert after.accepted_strong_round == completed.accepted_strong_round


def _strong_codex_config(strategy: str) -> LLMBackendConfiguration:
    return LLMBackendConfiguration.load_from_dict(
        {
            "quota_selection": {"strategy": strategy},
            "backend_strong_pr_adversarial_validation": {"order": ["codex-reviewer"]},
            "backends": {
                "codex-reviewer": {
                    "backend_type": "codex",
                    "model": "gpt-5",
                    "enabled": True,
                }
            },
        }
    )


def test_burst_quota_below_reserve_reaches_production_strong_reviewer(tmp_path: Path, monkeypatch) -> None:
    repository, base, head = _repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    inputs.gate.ordinary_pass(15, head, base, inputs.contract)
    manager = MagicMock()
    usage = CodexWeeklyUsage(
        remaining_percent=12.0,
        reset_at=datetime.now(timezone.utc) + timedelta(days=2),
        days_until_reset=2,
        minimum_remaining_percent=15.0,
    )

    @contextlib.contextmanager
    def worktree(*args, **kwargs):
        yield str(repository)

    def result(review_input, backend_manager, execution_cwd):
        assert backend_manager is manager
        assert execution_cwd == str(repository)
        return ReviewExecutionResult(
            mode=ReviewMode.STRONG_AUDIT,
            round_id=review_input.round_id,
            attempt_id=review_input.attempt_id,
            head_sha=head,
            base_sha=base,
            contract_identity=inputs.contract.identity,
            policy_identity=inputs.policy.identity,
            finding_set_revision=0,
            reviewer_provenance="strong/codex/gpt-5",
            verdict=VERDICT_PASS,
        )

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch("auto_coder.cli_helpers.get_llm_config", return_value=_strong_codex_config("burst")),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
        patch("auto_coder.cli_helpers.build_backend_manager", return_value=manager) as construct,
        patch(
            "auto_coder.pr_processor.CommandExecutor.run_command",
            side_effect=[
                CommandResult(True, "-base\n+head\n", "", 0),
                CommandResult(True, "contract.txt\n", "", 0),
            ],
        ),
        patch("auto_coder.pr_processor.execute_review", side_effect=result) as transport,
    ):
        accepted, reason = _execute_pending_strong_audit("owner/repo", 15, inputs)

    assert accepted is True
    assert "publication remains pending" in reason
    construct.assert_called_once()
    transport.assert_called_once()
    snapshot = inputs.gate.state.snapshot(15)
    assert snapshot.waiting_reason != "strong reviewer route is EXHAUSTED"
    assert snapshot.accepted_strong_round is not None


@pytest.mark.parametrize(
    ("strategy", "remaining_percent"),
    [("burst", 0.0), ("surplus", 12.0)],
)
def test_configured_strategy_retains_true_strong_exhaustion(
    tmp_path: Path,
    monkeypatch,
    strategy: str,
    remaining_percent: float,
) -> None:
    repository, base, head = _repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    pr_number = 16 if strategy == "burst" else 17
    inputs.gate.ordinary_pass(pr_number, head, base, inputs.contract)
    usage = CodexWeeklyUsage(
        remaining_percent=remaining_percent,
        reset_at=datetime.now(timezone.utc) + timedelta(days=2),
        days_until_reset=2,
        minimum_remaining_percent=15.0,
    )

    @contextlib.contextmanager
    def worktree(*args, **kwargs):
        yield str(repository)

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch("auto_coder.cli_helpers.get_llm_config", return_value=_strong_codex_config(strategy)),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
        patch("auto_coder.cli_helpers.build_backend_manager") as construct,
        patch("auto_coder.pr_processor.execute_review") as transport,
    ):
        accepted, reason = _execute_pending_strong_audit("owner/repo", pr_number, inputs)

    assert accepted is False
    assert reason == "strong reviewer route is EXHAUSTED"
    construct.assert_not_called()
    transport.assert_not_called()
    snapshot = inputs.gate.state.snapshot(pr_number)
    assert snapshot.waiting_reason == "strong reviewer route is EXHAUSTED"
    assert snapshot.retry_not_before == pytest.approx(usage.reset_at.timestamp())
