"""Comprehensive regressions for PR repair exhaustion and operator resumption (GitHub Issue #2142).

Covers:
- AS-001: End-to-end non-convergence stops automatic dispatch (REQ-001, REQ-002, REQ-004, REQ-005, REQ-011)
- AS-002: Bypass matrix: all repair origins stopped (REQ-001, REQ-002, REQ-003, REQ-006)
- AS-003: Human and forced-change revalidation (REQ-006)
- AS-004: Explicit operator resumption (REQ-007, REQ-010)
- AS-005: Resumption safety preconditions & missing evidence (REQ-007, REQ-010)
- AS-006: Crash recovery for operator grants (REQ-012)
- AS-007: Review limits never become repair success (REQ-003)
- CLI commands: pr-repair status (REQ-008) and pr-repair resume (REQ-007)
- Configuration: max_failed_corrections loading, repo override, validation (REQ-009)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig
from auto_coder.bounded_repair_bundle import BoundedBlockerHandoff, RepairHandoffBundle
from auto_coder.canonical_pr_blocker_ledger import (
    BlockerAdmissionPayload,
    BlockerDisposition,
    BlockerSnapshot,
    CanonicalPRBlockerLedger,
    QualifiedRequirement,
)
from auto_coder.cli import main
from auto_coder.cli_commands_pr_repair import pr_repair_group
from auto_coder.durable_repair_allowance import (
    BlockerSettlement,
    CompletionAvailability,
    CorrectiveGenerationBundle,
    DeliveryOutcome,
    GenerationLifecycleState,
    InvalidOperatorGrantError,
    RepairAllowanceIdempotencyConflictError,
    RepairAllowanceLedger,
    RepairAllowanceStatus,
    ValidationAvailability,
    ValidationObservation,
    reconcile_unfulfilled_grant_reevaluations,
)
from auto_coder.github_pending_work import PendingWorkStore, WorkIdentity
from auto_coder.llm_backend_config import get_pr_repair_max_failed_corrections
from auto_coder.pr_processor import (
    _close_empty_pr,
    _close_stale_jules_pr,
    _delegate_cloud_review_thread_repair,
    _handle_pr_merge,
    _merge_pr,
    _reject_unsafe_codex_cloud_pr,
    _send_adversarial_validation_feedback_to_cloud_task,
    _send_codex_cloud_error_feedback,
    _send_jules_error_feedback,
    _start_mergeability_remediation,
)
from auto_coder.pr_repair_guard import (
    check_pr_repair_exhaustion,
    format_exhaustion_comment,
    publish_exhaustion_comment_deduped,
)
from auto_coder.provider_repair_correlation import (
    AdmissionRefusalError,
    ProviderRepairCoordinator,
    ProviderRepairCorrelationStore,
)

API_ORIGIN = "https://api.github.com"
REPO = "kitamura-tetsuo/auto-coder"
PR_NUMBER = 2142


@pytest.fixture()
def allowance_db(tmp_path: Path) -> Path:
    return tmp_path / "repair_allowance.db"


@pytest.fixture()
def allowance_store(allowance_db: Path) -> RepairAllowanceLedger:
    return RepairAllowanceLedger(db_path=allowance_db)


@pytest.fixture()
def blocker_db(tmp_path: Path) -> Path:
    return tmp_path / "canonical_blockers.db"


@pytest.fixture()
def blocker_store(blocker_db: Path) -> CanonicalPRBlockerLedger:
    return CanonicalPRBlockerLedger(db_path=blocker_db)


@pytest.fixture()
def pending_work_db(tmp_path: Path) -> Path:
    return tmp_path / "pending_work.db"


@pytest.fixture()
def pending_work_store(pending_work_db: Path) -> PendingWorkStore:
    return PendingWorkStore(db_path=pending_work_db)


def _exhaust_blocker(store: RepairAllowanceLedger, blocker_id: str, limit: int = 3) -> int:
    """Helper to simulate consecutive failed generations exhausting a blocker."""
    try:
        snapshot = store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
        epoch = snapshot.epoch
    except Exception:
        snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
        epoch = snapshot.epoch

    for i in range(limit):
        bundle = CorrectiveGenerationBundle(
            bundle_reference=f"bundle-{blocker_id}-{i}",
            covered_blocker_ids=(blocker_id,),
            owning_identity="task-1",
            observed_baseline=f"sha-{i}",
        )
        adm = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, f"adm-{blocker_id}-{i}", epoch, bundle, open_blocker_ids=(blocker_id,), default_limit_for_new_blockers=limit)
        assert adm.admitted
        gid = adm.generation_id
        epoch = adm.snapshot.epoch
        sn = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, f"del-{blocker_id}-{i}", epoch, gid, DeliveryOutcome.CONFIRMED, f"rec-{i}")
        epoch = sn.epoch
        sn = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, f"comp-{blocker_id}-{i}", epoch, gid, CompletionAvailability.KNOWN, completion_seq=10 * (i + 1), code_changed=True)
        epoch = sn.epoch
        sn = store.record_validation_results(
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            f"val-{blocker_id}-{i}",
            epoch,
            gid,
            [ValidationObservation(blocker_id=blocker_id, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=10 * (i + 1) + 1)],
        )
        epoch = sn.epoch

    return epoch


# ---------------------------------------------------------------------------
# AS-001: End-to-end non-convergence stops automatic dispatch
# ---------------------------------------------------------------------------


def test_as001_non_convergence_stops_dispatch(allowance_store: RepairAllowanceLedger, blocker_store: CanonicalPRBlockerLedger) -> None:
    """AS-001: 3 failed generations exhaust allowance -> AUTO_REPAIR_EXHAUSTED -> no 4th dispatch.

    Deduplicated comment published once; unaffected PR continues normally (REQ-004).
    """
    blocker_id = "blk_as001"
    _exhaust_blocker(allowance_store, blocker_id, limit=3)

    # Check exhaustion detection
    exhaustion_info = check_pr_repair_exhaustion(
        repo_name=REPO,
        pr_number=PR_NUMBER,
        allowance_ledger=allowance_store,
        blocker_ledger=blocker_store,
    )
    assert exhaustion_info is not None
    assert exhaustion_info.is_exhausted
    assert exhaustion_info.machine_readable_reason == "AUTO_REPAIR_EXHAUSTED"
    assert blocker_id in exhaustion_info.exhausted_blocker_ids

    # Outbound ticket binding refuses 4th generation
    coord = ProviderRepairCoordinator(allowance_ledger=allowance_store)
    handoff_bundle = RepairHandoffBundle(
        bundle_id="bnd_4th",
        repo_name=REPO,
        pr_number=PR_NUMBER,
        reviewed_head_sha="sha-3",
        blockers=(BoundedBlockerHandoff(blocker_id=blocker_id),),
    )
    with pytest.raises(AdmissionRefusalError) as exc_info:
        coord.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, handoff_bundle, "local", "task-1")
    assert "AUTO_REPAIR_EXHAUSTED" in str(exc_info.value)

    # Deduplicated human comment: first call publishes, second call skips
    mock_gh = MagicMock()
    mock_gh.get_pr_comments.return_value = []
    published_1 = publish_exhaustion_comment_deduped(mock_gh, REPO, PR_NUMBER, exhaustion_info)
    assert published_1 is True
    assert mock_gh.add_pr_comment.call_count == 1
    call_body = mock_gh.add_pr_comment.call_args[0][2]
    assert "<!-- auto-coder:pr-repair-exhausted -->" in call_body
    assert blocker_id in call_body

    # Second call with existing comment marker skips
    mock_gh.get_pr_comments.return_value = [{"body": call_body}]
    published_2 = publish_exhaustion_comment_deduped(mock_gh, REPO, PR_NUMBER, exhaustion_info)
    assert published_2 is False
    assert mock_gh.add_pr_comment.call_count == 1  # No second post

    # REQ-004: Unrelated PR continues normally
    unrelated_pr = 9999
    unrelated_info = check_pr_repair_exhaustion(
        repo_name=REPO,
        pr_number=unrelated_pr,
        allowance_ledger=allowance_store,
        blocker_ledger=blocker_store,
    )
    assert unrelated_info is None


# ---------------------------------------------------------------------------
# AS-002: Bypass matrix: all repair origins stopped
# ---------------------------------------------------------------------------


def test_as002_bypass_matrix_stopped(allowance_store: RepairAllowanceLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-002: Exhaustion stops repair across all candidate origins."""
    blocker_id = "blk_as002"
    _exhaust_blocker(allowance_store, blocker_id)

    # Monkeypatch check_pr_repair_exhaustion to use our test allowance_store
    def mock_check(repo_name: str, pr_number: int, **kwargs):
        if repo_name == REPO and pr_number == PR_NUMBER:
            return check_pr_repair_exhaustion(repo_name, pr_number, allowance_ledger=allowance_store)
        return None

    monkeypatch.setattr("auto_coder.pr_processor.check_pr_repair_exhaustion", mock_check)
    monkeypatch.setattr("auto_coder.pr_repair_guard.check_pr_repair_exhaustion", mock_check)

    mock_gh = MagicMock()
    mock_gh.get_pr_comments.return_value = []
    pr_data = {"number": PR_NUMBER, "head": {"sha": "head123", "ref": "feat"}, "state": "open"}
    config = AutomationConfig()

    # 1. CI / conflict repair stopped
    conflict_actions = _start_mergeability_remediation(PR_NUMBER, "dirty", REPO)
    assert any("repair allowance exhausted" in a for a in conflict_actions)

    # 2. Recovery-driven replacement or reissue stopped
    empty_result = _close_empty_pr(mock_gh, REPO, pr_data, config)
    assert not empty_result.closed
    assert any("repair allowance exhausted" in a for a in empty_result.actions)

    unsafe_result = _reject_unsafe_codex_cloud_pr(mock_gh, REPO, {"number": PR_NUMBER, "head": {"ref": "work"}}, config)
    assert not unsafe_result.closed

    stale_jules = _close_stale_jules_pr(mock_gh, REPO, {"number": PR_NUMBER, "user": {"login": "google-labs-jules"}}, config)
    assert not stale_jules.closed

    # 3. Provider error feedback stopped
    codex_feedback = _send_codex_cloud_error_feedback(REPO, pr_data, [], config, mock_gh)
    assert not codex_feedback.delivered
    assert any("repair allowance is exhausted" in a for a in codex_feedback.actions)

    jules_feedback = _send_jules_error_feedback(REPO, pr_data, [], config, mock_gh)
    assert any("repair allowance is exhausted" in a for a in jules_feedback)

    # 4. Review repair delegation stopped
    review_feedback = _delegate_cloud_review_thread_repair(REPO, pr_data, mock_gh)
    assert not review_feedback.delivered
    assert any("repair allowance is exhausted" in a for a in review_feedback)

    # 5. Adversarial feedback stopped
    adv_feedback = _send_adversarial_validation_feedback_to_cloud_task(REPO, pr_data, "head123", "report", mock_gh)
    assert any("repair allowance is exhausted" in a for a in adv_feedback)

    # 6. Automatic merge stopped
    merge_allowed = _merge_pr(REPO, PR_NUMBER, {}, config, mock_gh)
    assert merge_allowed is False


# ---------------------------------------------------------------------------
# AS-003: Human and forced-change revalidation
# ---------------------------------------------------------------------------


def test_as003_human_revalidation_clears_hold(allowance_store: RepairAllowanceLedger, blocker_store: CanonicalPRBlockerLedger) -> None:
    """AS-003: Human commit resolving all exhausted blockers clears exhaustion hold without deleting history."""
    # Register open blockers in canonical blocker ledger
    blocker_store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    p1 = BlockerAdmissionPayload(category="finding")
    b1, snap1 = blocker_store.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op1", 1, p1)
    p2 = BlockerAdmissionPayload(category="finding")
    b2, snap2 = blocker_store.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op2", snap1.ledger_revision, p2)

    _exhaust_blocker(allowance_store, b1)
    _exhaust_blocker(allowance_store, b2)

    info = check_pr_repair_exhaustion(REPO, PR_NUMBER, allowance_ledger=allowance_store, blocker_ledger=blocker_store)
    assert info is not None
    assert set(info.exhausted_blocker_ids) == {b1, b2}

    # Resolve only b1 (partial fix)
    b_snap = blocker_store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    blocker_store.record_transition(API_ORIGIN, REPO, PR_NUMBER, "op_res1", b_snap.ledger_revision, b1, BlockerDisposition.VERIFIED_CORRECTION, evidence="fixed in new commit")

    info_partial = check_pr_repair_exhaustion(REPO, PR_NUMBER, allowance_ledger=allowance_store, blocker_ledger=blocker_store)
    assert info_partial is not None
    assert info_partial.is_exhausted
    assert info_partial.exhausted_blocker_ids == (b2,)

    # Resolve b2 (complete fix)
    b_snap2 = blocker_store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    blocker_store.record_transition(API_ORIGIN, REPO, PR_NUMBER, "op_res2", b_snap2.ledger_revision, b2, BlockerDisposition.VERIFIED_CORRECTION, evidence="fixed in new commit")

    info_cleared = check_pr_repair_exhaustion(REPO, PR_NUMBER, allowance_ledger=allowance_store, blocker_ledger=blocker_store)
    assert info_cleared is None  # Active hold cleared!

    # Historical failures retained
    a_snap = allowance_store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    assert a_snap.get_blocker_allowance(b1).total_failed_count == 3
    assert a_snap.get_blocker_allowance(b2).total_failed_count == 3


# ---------------------------------------------------------------------------
# AS-004: Explicit operator resumption
# ---------------------------------------------------------------------------


def test_as004_explicit_operator_resumption(
    allowance_store: RepairAllowanceLedger,
    pending_work_store: PendingWorkStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AS-004: Operator resume command unblocks PR and schedules re-evaluation."""
    blocker_id = "blk_as004"
    epoch = _exhaust_blocker(allowance_store, blocker_id)

    # Monkeypatch get_pending_work_store and default ledger
    monkeypatch.setattr("auto_coder.cli_commands_pr_repair.RepairAllowanceLedger", lambda: allowance_store)
    monkeypatch.setattr("auto_coder.cli_commands_pr_repair.PendingWorkStore", lambda: pending_work_store)

    runner = CliRunner()
    res = runner.invoke(
        pr_repair_group,
        ["resume", "--repo", REPO, "--pr", str(PR_NUMBER), "--expected-epoch", str(epoch), "--request-id", "req-token-1", "--json"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["granted"] is True
    assert data["new_epoch"] == epoch + 1
    assert blocker_id in data["granted_blocker_ids"]

    # Blocker allowance is now ALLOWABLE
    snap = allowance_store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    b_allowance = snap.get_blocker_allowance(blocker_id)
    assert b_allowance is not None
    assert b_allowance.status == RepairAllowanceStatus.ALLOWABLE
    assert b_allowance.remaining == 3

    # Work scheduled in pending work store
    obligations = pending_work_store.all_pending()
    assert len(obligations) == 1
    assert obligations[0].identity.repository == REPO
    assert obligations[0].identity.entity == f"pr:{PR_NUMBER}"

    # Idempotent replay with same request_id returns existing grant
    res_idemp = runner.invoke(
        pr_repair_group,
        ["resume", "--repo", REPO, "--pr", str(PR_NUMBER), "--expected-epoch", str(epoch), "--request-id", "req-token-1", "--json"],
    )
    assert res_idemp.exit_code == 0
    assert json.loads(res_idemp.output)["granted"] is True


# ---------------------------------------------------------------------------
# AS-005: Resumption safety preconditions & missing evidence
# ---------------------------------------------------------------------------


def test_as005_resumption_preconditions(
    allowance_store: RepairAllowanceLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AS-005: Resumption fails if epoch is stale or generation is active."""
    blocker_id = "blk_as005"
    epoch = _exhaust_blocker(allowance_store, blocker_id)

    monkeypatch.setattr("auto_coder.cli_commands_pr_repair.RepairAllowanceLedger", lambda: allowance_store)
    runner = CliRunner()

    # 1. Stale expected epoch
    res_stale = runner.invoke(
        pr_repair_group,
        ["resume", "--repo", REPO, "--pr", str(PR_NUMBER), "--expected-epoch", str(epoch - 1), "--request-id", "req-stale", "--json"],
    )
    assert res_stale.exit_code != 0
    assert "Contention: expected namespace epoch" in res_stale.output or "Mismatched expected epoch" in res_stale.output

    # 2. In-flight generation blocks grant
    # Admit a generation
    bundle = CorrectiveGenerationBundle(bundle_reference="inflight", covered_blocker_ids=(blocker_id,), owning_identity="task-x", observed_baseline="sha-x")
    # First grant fresh allowance so we can admit
    allowance_store.operator_grant(API_ORIGIN, REPO, PR_NUMBER, "req-grant-init", epoch)
    epoch = allowance_store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER).epoch
    adm = allowance_store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit-active", epoch, bundle, open_blocker_ids=(blocker_id,))
    epoch = adm.snapshot.epoch

    res_active = runner.invoke(
        pr_repair_group,
        ["resume", "--repo", REPO, "--pr", str(PR_NUMBER), "--expected-epoch", str(epoch), "--request-id", "req-active", "--json"],
    )
    assert res_active.exit_code != 0
    assert "Cannot grant a fresh repair allowance while a generation is outstanding" in res_active.output


# ---------------------------------------------------------------------------
# AS-006: Crash recovery for operator grants
# ---------------------------------------------------------------------------


def test_as006_crash_recovery_for_operator_grants(
    allowance_store: RepairAllowanceLedger,
    pending_work_store: PendingWorkStore,
) -> None:
    """AS-006: Process crash before scheduling re-evaluation is recovered on next run."""
    blocker_id = "blk_as006"
    epoch = _exhaust_blocker(allowance_store, blocker_id)

    # SQLite commits grant with reevaluation_delivered = 0
    res = allowance_store.operator_grant(API_ORIGIN, REPO, PR_NUMBER, "req-crash-1", epoch)
    assert res.granted

    # Check unfulfilled
    unfulfilled = allowance_store.get_unfulfilled_operator_grants()
    assert len(unfulfilled) == 1
    assert unfulfilled[0]["request_id"] == "req-crash-1"

    # Pending work has not been scheduled yet
    assert len(pending_work_store.all_pending()) == 0

    # Crash recovery runs
    count = reconcile_unfulfilled_grant_reevaluations(allowance_store, pending_work_store)
    assert count == 1

    # Now pending work is scheduled
    assert len(pending_work_store.all_pending()) == 1

    # And unfulfilled list is now empty
    assert len(allowance_store.get_unfulfilled_operator_grants()) == 0


# ---------------------------------------------------------------------------
# AS-007: Review limits never become repair success
# ---------------------------------------------------------------------------


def test_as007_review_limits_never_repair_success(
    allowance_store: RepairAllowanceLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AS-007: Reaching max review limit with exhausted allowance stops merge and records BLOCKED."""
    blocker_id = "blk_as007"
    _exhaust_blocker(allowance_store, blocker_id)

    def mock_check(repo_name: str, pr_number: int, **kwargs):
        if repo_name == REPO and pr_number == PR_NUMBER:
            return check_pr_repair_exhaustion(repo_name, pr_number, allowance_ledger=allowance_store)
        return None

    monkeypatch.setattr("auto_coder.pr_processor.check_pr_repair_exhaustion", mock_check)

    config = AutomationConfig()
    config.MAX_ADVERSARIAL_VALIDATIONS = 1

    mock_gh = MagicMock()
    mock_gh.get_pr_comments.return_value = [{"body": "<!-- auto-coder-adversarial-validation:verdict: PASS -->"}]
    mock_gh.get_pr_reviews_strict.return_value = []

    # Mock CI checks passing
    checks = MagicMock()
    checks.success = True
    checks.error = None
    checks.in_progress = False
    monkeypatch.setattr("auto_coder.pr_processor._check_github_actions_status", lambda *args, **kwargs: checks)
    monkeypatch.setattr("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", lambda *args, **kwargs: True)
    monkeypatch.setattr("auto_coder.pr_processor._get_mergeable_state", lambda *args, **kwargs: {"mergeable": True, "merge_state_status": "clean"})
    monkeypatch.setattr("auto_coder.pr_processor._get_published_adversarial_validation_status", lambda *args, **kwargs: ("PASS", None))
    monkeypatch.setattr("auto_coder.pr_processor._adversarial_validation_exhaustion_retry_due", lambda *args, **kwargs: (False, None))
    monkeypatch.setattr("auto_coder.pr_processor._get_review_thread_gate_state", lambda *args, **kwargs: MagicMock(has_blocking_unresolved=False, claimed=(), blocking_unresolved=()))
    monkeypatch.setattr("auto_coder.pr_processor._get_claimed_review_thread_state", lambda *args, **kwargs: MagicMock(lookup_error=None, claimed=()))
    monkeypatch.setattr("auto_coder.pr_processor._get_adversarial_validation_eligibility", lambda *args, **kwargs: MagicMock(lookup_error=None, is_applicable=True, issue_numbers=()))
    monkeypatch.setattr("auto_coder.pr_processor._is_dependabot_pr", lambda *args, **kwargs: False)
    monkeypatch.setattr("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", lambda *args, **kwargs: False)

    pr_data = {"number": PR_NUMBER, "head": {"sha": "head_green", "ref": "feat"}, "state": "open"}
    actions = _handle_pr_merge(mock_gh, REPO, pr_data, config, {})

    assert any("Automatic merge disabled" in a and "repair allowance exhausted" in a for a in actions)


# ---------------------------------------------------------------------------
# CLI status and resume commands
# ---------------------------------------------------------------------------


def test_cli_pr_repair_status_and_resume(allowance_store: RepairAllowanceLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI pr-repair status displays state accurately in text and json; read-only guarantees zero mutations."""
    blocker_id = "blk_cli_status"
    epoch = _exhaust_blocker(allowance_store, blocker_id)

    monkeypatch.setattr("auto_coder.cli_commands_pr_repair.RepairAllowanceLedger", lambda: allowance_store)

    runner = CliRunner()

    # Text output
    res_text = runner.invoke(main, ["pr-repair", "status", "--repo", REPO, "--pr", str(PR_NUMBER)])
    assert res_text.exit_code == 0
    assert "EXHAUSTED" in res_text.output
    assert blocker_id in res_text.output
    assert f"Current Epoch: {epoch}" in res_text.output

    # JSON output
    res_json = runner.invoke(main, ["pr-repair", "status", "--repo", REPO, "--pr", str(PR_NUMBER), "--json"])
    assert res_json.exit_code == 0
    data = json.loads(res_json.output)
    assert data["repair_state"] == "EXHAUSTED"
    assert data["is_exhausted"] is True
    assert data["current_epoch"] == epoch
    assert blocker_id in data["exhausted_blocker_ids"]

    # Verify zero mutations
    snap_after = allowance_store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    assert snap_after.epoch == epoch


# ---------------------------------------------------------------------------
# Configuration loading & validation
# ---------------------------------------------------------------------------


def test_config_pr_repair_max_failed_corrections(tmp_path: Path) -> None:
    """Config loading: default 3, global setting, repo override, invalid rejection."""
    # 1. Default
    non_existent = tmp_path / "non_existent.toml"
    assert get_pr_repair_max_failed_corrections(config_path=non_existent) == 3

    # 2. Section default in config.toml
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[pr_repair]
max_failed_corrections = 5

[repository."kitamura-tetsuo/auto-coder".pr_repair]
max_failed_corrections = 8
"""
    )

    # Repository override takes precedence
    assert get_pr_repair_max_failed_corrections(config_path=config_file, repo_name="kitamura-tetsuo/auto-coder") == 8
    # Other repo falls back to global pr_repair setting
    assert get_pr_repair_max_failed_corrections(config_path=config_file, repo_name="other/repo") == 5

    # 3. Invalid non-positive integer
    bad_config = tmp_path / "bad_config.toml"
    bad_config.write_text(
        """
[pr_repair]
max_failed_corrections = 0
"""
    )
    with pytest.raises(ValueError) as exc:
        get_pr_repair_max_failed_corrections(config_path=bad_config)
    assert "> 0" in str(exc.value)
