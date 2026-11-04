import contextlib
import json
import math
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cli_helpers import AdversarialValidationAvailability, resolve_adversarial_validation_availability
from auto_coder.codex_usage_checker import CodexWeeklyUsage, parse_codex_weekly_usage
from auto_coder.exceptions import LocalWriterSettlementError
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


def test_strong_audit_fetches_missing_exact_base_in_real_worktree(tmp_path: Path, monkeypatch, _use_real_commands: None) -> None:
    remote, original_base, head = _repository(tmp_path)
    _git(remote, "branch", "feature", head)
    _git(remote, "update-ref", "refs/pull/18/head", head)
    _git(remote, "checkout", "-b", "main", original_base)
    (remote / "base-only.txt").write_text("advanced base\n")
    _git(remote, "add", "base-only.txt")
    _git(remote, "commit", "-m", "advance base independently")
    base = _git(remote, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "--depth", "1", "--single-branch", "--branch", "feature", remote.as_uri(), str(checkout))
    assert subprocess.run(["git", "cat-file", "-e", base], cwd=checkout, capture_output=True).returncode != 0
    (checkout / "untracked.txt").write_text("preserve\n")
    (checkout / "contract.txt").write_text("local dirty change\n")
    _git(checkout, "add", "contract.txt")
    status = _git(checkout, "status", "--porcelain")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    inputs.gate.ordinary_pass(18, head, base, inputs.contract)
    manager = MagicMock()

    def result(review_input, backend_manager, execution_cwd):
        assert backend_manager is manager
        assert Path(execution_cwd).resolve() != checkout.resolve()
        assert (Path(execution_cwd) / ".git").is_file()
        assert _git(Path(execution_cwd), "rev-parse", "HEAD") == head
        assert review_input.base_sha == base
        assert "-advanced base" in review_input.diff_evidence
        assert "-base" in review_input.diff_evidence
        assert "+head" in review_input.diff_evidence
        assert "local dirty change" not in review_input.diff_evidence
        # Base acquisition must not replace the PR fetch's FETCH_HEAD.
        assert _git(checkout, "rev-parse", "FETCH_HEAD") == head
        return ReviewExecutionResult(
            mode=ReviewMode.STRONG_AUDIT,
            round_id=review_input.round_id,
            attempt_id=review_input.attempt_id,
            head_sha=head,
            base_sha=base,
            contract_identity=inputs.contract.identity,
            policy_identity=inputs.policy.identity,
            finding_set_revision=0,
            reviewer_provenance="strong/test/model",
            verdict=VERDICT_PASS,
        )

    with (
        patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability", return_value=AdversarialValidationAvailability(backend_manager=manager)),
        patch("auto_coder.pr_processor.execute_review", side_effect=result) as transport,
    ):
        accepted, reason = _execute_pending_strong_audit("owner/repo", 18, inputs)

    assert accepted is True, reason
    transport.assert_called_once()
    assert _git(checkout, "cat-file", "-t", base) == "commit"
    assert _git(checkout, "symbolic-ref", "HEAD") == "refs/heads/feature"
    assert _git(checkout, "rev-parse", "HEAD") == head
    assert _git(checkout, "status", "--porcelain") == status
    assert (checkout / "contract.txt").read_text() == "local dirty change\n"
    assert (checkout / "untracked.txt").read_text() == "preserve\n"
    assert len(_git(checkout, "worktree", "list", "--porcelain").split("worktree ")) == 2
    assert inputs.gate.state.snapshot(18).accepted_strong_round is not None


def test_unavailable_exact_base_in_real_worktree_refuses_without_reviewer(tmp_path: Path, monkeypatch, _use_real_commands: None) -> None:
    remote, _base, head = _repository(tmp_path)
    _git(remote, "update-ref", "refs/pull/18/head", head)
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "--depth", "1", remote.as_uri(), str(checkout))
    (checkout / "contract.txt").write_text("preserved staged content\n")
    _git(checkout, "add", "contract.txt")
    (checkout / "untracked.txt").write_text("preserve\n")
    status = _git(checkout, "status", "--porcelain")
    branch = _git(checkout, "symbolic-ref", "HEAD")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("HOME", str(tmp_path))
    missing = "f" * 40
    assert subprocess.run(["git", "cat-file", "-e", missing], cwd=checkout, capture_output=True).returncode != 0
    inputs = _inputs(tmp_path, missing, head)
    inputs.gate.ordinary_pass(18, head, missing, inputs.contract)
    with (
        patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability", return_value=AdversarialValidationAvailability(backend_manager=MagicMock())),
        patch("auto_coder.pr_processor.execute_review") as transport,
    ):
        accepted, reason = _execute_pending_strong_audit("owner/repo", 18, inputs)
    assert accepted is False
    assert f"reviewed base {missing} fetch failed (exit=128)" in reason
    transport.assert_not_called()
    snapshot = inputs.gate.state.snapshot(18)
    assert snapshot.phase == "STRONG_PENDING"
    assert snapshot.accepted_strong_round is None
    assert snapshot.active_claim is None
    assert _git(checkout, "symbolic-ref", "HEAD") == branch
    assert _git(checkout, "rev-parse", "HEAD") == head
    assert _git(checkout, "rev-parse", "FETCH_HEAD") == head
    assert _git(checkout, "status", "--porcelain") == status
    assert (checkout / "contract.txt").read_text() == "preserved staged content\n"
    assert (checkout / "untracked.txt").read_text() == "preserve\n"


@pytest.mark.parametrize(
    ("commands", "diagnostic"),
    [
        ([CommandResult(False, "", "missing", 1), CommandResult(False, "", "fatal: remote unavailable", 128)], "fetch failed (exit=128): fatal: remote unavailable"),
        ([CommandResult(False, "", "missing", 1), CommandResult(False, "", "token=ghp_example123", 128)], "fetch failed (exit=128): token=[REDACTED]"),
        ([CommandResult(False, "", "missing", 1), CommandResult(False, "", "x" * 3000, 128)], "fetch failed (exit=128): " + "x" * 2000),
        ([CommandResult(False, "", "missing", 1), CommandResult(True, "", "", 0), CommandResult(False, "", "fatal: missing commit", 128)], "verification failed (exit=128): fatal: missing commit"),
        ([CommandResult(True, "", "", 0), CommandResult(False, "", "fatal: bad object", 128), CommandResult(True, "file.txt", "", 0)], "required reviewed diff evidence is unavailable (exit=128): fatal: bad object"),
        ([CommandResult(True, "", "", 0), CommandResult(True, "diff", "", 0), CommandResult(False, "", "fatal: index unavailable", 128)], "required tracked repository paths evidence is unavailable (exit=128): fatal: index unavailable"),
    ],
)
def test_evidence_failure_preserves_diagnostic_and_pending_claim(tmp_path: Path, monkeypatch, commands: list[CommandResult], diagnostic: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, "base", "head")
    inputs.gate.ordinary_pass(19, "head", "base", inputs.contract)
    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", return_value=contextlib.nullcontext(str(tmp_path))),
        patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability", return_value=AdversarialValidationAvailability(backend_manager=MagicMock())),
        patch("auto_coder.pr_processor.CommandExecutor.run_command", side_effect=commands),
        patch("auto_coder.pr_processor.execute_review") as transport,
    ):
        accepted, reason = _execute_pending_strong_audit("owner/repo", 19, inputs)
    assert accepted is False
    assert diagnostic in reason
    assert reason.startswith("strong audit execution failed: ")
    assert "ghp_example123" not in reason
    assert "x" * 2001 not in reason
    transport.assert_not_called()
    snapshot = _inputs(tmp_path, "base", "head").gate.state.snapshot(19)
    assert snapshot.phase == "STRONG_PENDING"
    assert diagnostic in snapshot.waiting_reason
    assert snapshot.active_claim is None
    assert snapshot.accepted_strong_round is None


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
                CommandResult(True, "", "", 0),
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


def test_execution_safety_diagnostic_remains_pending_after_restart(tmp_path: Path, monkeypatch) -> None:
    repository, base, head = _repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    inputs.gate.ordinary_pass(13, head, base, inputs.contract)
    diagnostic = "Codex execution safety could not be established; provider replacement is withheld: " "private workspace repository preparation failed (uid=None, gid=None): " "repository readiness requires an explicit non-root worker identity"

    @contextlib.contextmanager
    def worktree(*args, **kwargs):
        yield str(repository)

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch(
            "auto_coder.cli_helpers.resolve_adversarial_validation_availability",
            return_value=AdversarialValidationAvailability(backend_manager=MagicMock()),
        ),
        patch(
            "auto_coder.pr_processor.CommandExecutor.run_command",
            side_effect=[CommandResult(True, "", "", 0), CommandResult(True, "diff", "", 0), CommandResult(True, "contract.txt", "", 0)],
        ),
        patch("auto_coder.pr_processor.execute_review", side_effect=LocalWriterSettlementError(diagnostic)) as transport,
    ):
        accepted, reason = _execute_pending_strong_audit("owner/repo", 13, inputs)

    assert accepted is False
    assert reason == f"strong audit execution failed: {diagnostic}"
    transport.assert_called_once()
    snapshot = _inputs(tmp_path, base, head).gate.state.snapshot(13)
    assert snapshot.phase == "STRONG_PENDING"
    assert snapshot.waiting_reason == diagnostic
    assert snapshot.active_claim is None
    assert snapshot.accepted_strong_round is None


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
                CommandResult(True, "", "", 0),
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
    if strategy == "surplus":
        assert time.time() < snapshot.retry_not_before <= time.time() + 1800.0
    else:
        assert snapshot.retry_not_before == pytest.approx(usage.reset_at.timestamp())


def test_stale_explicit_recheck_cannot_supersede_newer_wait_or_invoke(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, "base", "head")
    inputs.gate.ordinary_pass(18, "head", "base", inputs.contract)
    selected = inputs.gate.state.snapshot(18)
    competing = inputs.gate.state.claim_strong_audit(18, RoundProvenance("head", "base"), inputs.contract, inputs.policy)
    newer_deadline = time.time() + 900.0
    inputs.gate.state.abandon_claim(18, competing.claim_id, "newer quota refusal", newer_deadline)

    with patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability") as resolver:
        accepted, reason = _execute_pending_strong_audit(
            "owner/repo",
            18,
            inputs,
            allow_retry_override=True,
            expected_version=selected.transition_version,
        )

    assert accepted is False
    assert "wait changed before admission" in reason
    resolver.assert_not_called()
    retained = inputs.gate.state.snapshot(18)
    assert retained.waiting_reason == "newer quota refusal"
    assert retained.retry_not_before == newer_deadline


def test_surplus_exact_boundary_schedules_strictly_future_recheck_from_parsed_usage() -> None:
    now = 2_000_000_000.0
    payload = {
        "rateLimits": {
            "primary": {"windowDurationMins": 10080, "usedPercent": 80.0, "resetsAt": now + 4 * 86400},
        }
    }
    usage = parse_codex_weekly_usage(payload, now=datetime.fromtimestamp(now, tz=timezone.utc))

    with (
        patch("auto_coder.cli_helpers.time.time", return_value=now),
        patch("auto_coder.cli_helpers.get_llm_config", return_value=_strong_codex_config("surplus")),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
    ):
        availability = resolve_adversarial_validation_availability("strong_pr")

    assert availability.exhausted is True
    assert now < availability.retry_not_before_epoch <= now + 1800.0


def test_surplus_earlier_boundary_preempts_thirty_minute_recheck() -> None:
    now = 2_000_000_000.0
    boundary_delay = 600.0
    payload = {
        "rateLimits": {
            "primary": {
                "windowDurationMins": 10080,
                "usedPercent": 80.0,
                "resetsAt": now + 4 * 86400 + boundary_delay,
            },
        }
    }
    usage = parse_codex_weekly_usage(payload, now=datetime.fromtimestamp(now, tz=timezone.utc))

    with (
        patch("auto_coder.cli_helpers.time.time", return_value=now),
        patch("auto_coder.cli_helpers.get_llm_config", return_value=_strong_codex_config("surplus")),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
    ):
        availability = resolve_adversarial_validation_availability("strong_pr")

    assert availability.exhausted is True
    assert availability.retry_not_before_epoch == math.nextafter(now + boundary_delay, math.inf)


def test_parsed_unchanged_twenty_percent_becomes_eligible_below_four_days() -> None:
    now = datetime(2033, 5, 18, tzinfo=timezone.utc)
    payload = {
        "rateLimits": {
            "primary": {"windowDurationMins": 10080, "usedPercent": 80.0, "resetsAt": (now + timedelta(days=4) - timedelta(microseconds=1)).timestamp()},
        }
    }
    usage = parse_codex_weekly_usage(payload, now=now)
    manager = MagicMock()

    with (
        patch("auto_coder.cli_helpers.get_llm_config", return_value=_strong_codex_config("surplus")),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
        patch("auto_coder.cli_helpers.build_backend_manager", return_value=manager),
    ):
        availability = resolve_adversarial_validation_availability("strong_pr")

    assert usage.minimum_remaining_percent == 20.0
    assert availability.backend_manager is manager
    assert availability.exhausted is False


@pytest.mark.parametrize("eligible", [False, True])
def test_due_production_recheck_preserves_prior_wait_across_restart(
    tmp_path: Path,
    monkeypatch,
    eligible: bool,
) -> None:
    repository, base, head = _repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    inputs.gate.ordinary_pass(20, head, base, inputs.contract)
    initial = inputs.gate.state.claim_strong_audit(20, RoundProvenance(head, base), inputs.contract, inputs.policy)
    inputs.gate.state.abandon_claim(20, initial.claim_id, "surplus reserve denied", retry_not_before=100.0)
    selected = inputs.gate.state.snapshot(20)
    manager = MagicMock() if eligible else None

    @contextlib.contextmanager
    def worktree(*args, **kwargs):
        yield str(repository)

    def review_result(review_input, backend_manager, execution_cwd):
        assert backend_manager is manager
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
            verdict=VERDICT_PASS,
        )

    availability = AdversarialValidationAvailability(
        backend_manager=manager,
        exhausted=not eligible,
        retry_not_before_epoch=None if eligible else 200.0,
    )
    with (
        patch("auto_coder.pr_review_cycle.time.time", return_value=101.0),
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability", return_value=availability),
        patch("auto_coder.pr_processor.execute_review", side_effect=review_result) as reviewer,
    ):
        accepted, _reason = _execute_pending_strong_audit(
            "owner/repo",
            20,
            inputs,
            expected_version=selected.transition_version,
        )

    assert accepted is eligible
    assert reviewer.call_count == int(eligible)
    state_path = tmp_path / ".auto-coder" / "owner/repo" / "pr_review_cycle.json"
    persisted = json.loads(state_path.read_text())
    history = persisted["prs"]["20"]["wait_history"]
    assert history == [
        {
            "reason": "surplus reserve denied",
            "retry_not_before": 100.0,
            "superseded_at": 101.0,
            "superseded_by": "automatic-due-recheck",
        }
    ]
    recovered = _inputs(tmp_path, base, head).gate.state.snapshot(20)
    if eligible:
        assert recovered.accepted_strong_round is not None
    else:
        assert recovered.waiting_reason == "strong reviewer route is EXHAUSTED"
        assert recovered.retry_not_before == 200.0


def test_due_recheck_persistence_interruption_retains_original_wait(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, "base", "head")
    inputs.gate.ordinary_pass(21, "head", "base", inputs.contract)
    initial = inputs.gate.state.claim_strong_audit(21, RoundProvenance("head", "base"), inputs.contract, inputs.policy)
    inputs.gate.state.abandon_claim(21, initial.claim_id, "surplus reserve denied", retry_not_before=100.0)
    selected = inputs.gate.state.snapshot(21)

    with (
        patch("auto_coder.pr_review_cycle.time.time", return_value=101.0),
        patch.object(inputs.gate.state, "_write", side_effect=OSError("simulated persistence interruption")),
        patch("auto_coder.cli_helpers.resolve_adversarial_validation_availability") as resolver,
    ):
        accepted, reason = _execute_pending_strong_audit(
            "owner/repo",
            21,
            inputs,
            expected_version=selected.transition_version,
        )

    assert accepted is False
    assert reason == "strong-audit admission persistence failed: simulated persistence interruption"
    resolver.assert_not_called()
    recovered = _inputs(tmp_path, "base", "head").gate.state.snapshot(21)
    assert recovered.waiting_reason == "surplus reserve denied"
    assert recovered.retry_not_before == 100.0


def test_loaded_burst_force_recovers_recorded_surplus_wait_through_real_resolver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, base, head = _repository(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs = _inputs(tmp_path, base, head)
    inputs.gate.ordinary_pass(22, head, base, inputs.contract)
    observation = datetime.now(timezone.utc)
    payload = {
        "rateLimits": {
            "primary": {
                "windowDurationMins": 10080,
                "usedPercent": 80.0,
                "resetsAt": (observation + timedelta(days=4)).timestamp(),
            },
        }
    }
    usage = parse_codex_weekly_usage(payload, now=observation)
    manager = MagicMock()

    @contextlib.contextmanager
    def worktree(*args, **kwargs):
        yield str(repository)

    def review_result(review_input, backend_manager, execution_cwd):
        assert backend_manager is manager
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
            verdict=VERDICT_PASS,
        )

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
        patch("auto_coder.cli_helpers.get_llm_config", return_value=_strong_codex_config("surplus")),
        patch("auto_coder.cli_helpers.build_backend_manager", return_value=manager) as build_manager,
        patch("auto_coder.pr_processor.execute_review", side_effect=review_result) as reviewer,
    ):
        denied, _reason = _execute_pending_strong_audit("owner/repo", 22, inputs)

    assert denied is False
    reviewer.assert_not_called()
    build_manager.assert_not_called()
    retained = inputs.gate.state.snapshot(22)
    assert retained.retry_not_before > time.time()

    with (
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", worktree),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage", return_value=usage),
        patch("auto_coder.cli_helpers.get_llm_config", return_value=_strong_codex_config("burst")),
        patch("auto_coder.cli_helpers.build_backend_manager", return_value=manager),
        patch("auto_coder.pr_processor.execute_review", side_effect=review_result) as reviewer,
    ):
        accepted, _reason = _execute_pending_strong_audit(
            "owner/repo",
            22,
            inputs,
            allow_retry_override=True,
            expected_version=retained.transition_version,
        )

    assert accepted is True
    reviewer.assert_called_once()
    persisted = json.loads((tmp_path / ".auto-coder" / "owner/repo" / "pr_review_cycle.json").read_text())
    history = persisted["prs"]["22"]["wait_history"]
    assert len(history) == 1
    assert history[0]["reason"] == "strong reviewer route is EXHAUSTED"
    assert history[0]["retry_not_before"] == retained.retry_not_before
    assert history[0]["superseded_by"] == "explicit-operator-recheck"
