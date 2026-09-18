"""Tests for provider repair delivery and completion correlation (GitHub Issue #2141).

Covers:
- AS-001: A real accepted follow-up, then its completion (REQ-001..REQ-004, REQ-007, REQ-010, REQ-011)
  across Local, Jules, Codex Cloud, and Claude Routine boundaries.
- AS-002: Known, unavailable, same known state (REQ-004..REQ-006, REQ-008).
- AS-003: Intervening request and no-code completion (REQ-004..REQ-006).
- AS-004: Lost acceptance and quota refusal are different (REQ-002, REQ-003, REQ-008, REQ-009).
- AS-005: Validation captured too early (REQ-006, REQ-007).
- AS-006: Persistence and owner changes (REQ-002, REQ-007..REQ-010).
- Standalone REQ-008 restart reconstruction and REQ-009 quota/ownership preservation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from auto_coder.bounded_repair_bundle import (
    BoundedBlockerHandoff,
    RepairHandoffBundle,
)
from auto_coder.cloud_task_client_base import CloudTask, CloudTaskState
from auto_coder.durable_repair_allowance import (
    GenerationLifecycleState,
    RepairAllowanceLedger,
    RepairAllowanceStatus,
    ValidationAvailability,
    ValidationObservation,
)
from auto_coder.exceptions import AutoCoderUsageLimitError
from auto_coder.provider_repair_correlation import (
    AdmissionRefusalError,
    AdmissionTicket,
    AdmissionTicketPersistenceError,
    CorrectionCompletionStatus,
    DeliveryStatus,
    OwnerMismatchError,
    ProviderRepairCoordinator,
    ProviderRepairCorrelationStore,
    ProviderType,
    StaleTicketEpochError,
    TicketStatus,
)

API_ORIGIN = "https://api.github.com"
REPO = "kitamura-tetsuo/auto-coder"
PR_NUMBER = 2141
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture()
def db_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "repair_allowance.db", tmp_path / "provider_correlation.db"


@pytest.fixture()
def coordinator(db_paths: tuple[Path, Path]) -> ProviderRepairCoordinator:
    allowance_db, corr_db = db_paths
    allowance_ledger = RepairAllowanceLedger(db_path=allowance_db)
    corr_store = ProviderRepairCorrelationStore(db_path=corr_db)
    allowance_ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    return ProviderRepairCoordinator(allowance_ledger, corr_store)


def _make_bundle(bundle_id: str, blocker_ids: tuple[str, ...], head_sha: str = HEAD_SHA) -> RepairHandoffBundle:
    blockers = tuple(
        BoundedBlockerHandoff(
            blocker_id=bid,
            reviewed_head_sha=head_sha,
            original_correction_scope=f"Scope for {bid}",
            required_corrective_outcome=f"Outcome for {bid}",
            production_boundary_oracle=f"Oracle for {bid}",
        )
        for bid in blocker_ids
    )
    return RepairHandoffBundle(
        bundle_id=bundle_id,
        api_origin=API_ORIGIN,
        repo_name=REPO,
        pr_number=PR_NUMBER,
        head_branch="feature/fix-2141",
        base_branch="main",
        reviewed_head_sha=head_sha,
        blockers=blockers,
    )


# ---------------------------------------------------------------------------
# Mock Native Provider Objects
# ---------------------------------------------------------------------------


@dataclass
class MockWhamTurn:
    id: str
    role: str
    status: str = "completed"
    content: str = "Fixed issue"


class MockCodexCloudClient:
    def __init__(self, turns: Optional[List[MockWhamTurn]] = None):
        self.turns: List[MockWhamTurn] = turns if turns is not None else []
        self.wham_client = MagicMock()
        self.wham_client.resolve_latest_assistant_turn.side_effect = self._latest_turn
        self.wham_client.get_task_turns.side_effect = lambda tid: self.turns
        self.send_call_count = 0
        self.quota_insufficient = False

    def _latest_turn(self, task_id: str) -> str:
        assistant_turns = [t for t in self.turns if t.role == "assistant"]
        return assistant_turns[-1].id if assistant_turns else ""

    def send_followup(self, task_id: str, message: str, logical_identities: tuple[str, ...] = ()) -> bool:
        self.send_call_count += 1
        if self.quota_insufficient:
            raise AutoCoderUsageLimitError("Codex Cloud quota exhausted")
        return True


class MockJulesClient:
    def __init__(self, session_dict: Optional[dict[str, Any]] = None):
        self.session_data: dict[str, Any] = (
            session_dict
            if session_dict is not None
            else {
                "id": "jules-task-1",
                "state": "IN_PROGRESS",
                "updateTime": "2026-09-19T00:00:00Z",
                "messages": [],
                "outputs": {},
            }
        )
        self.send_call_count = 0

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.session_data

    def send_followup(self, task_id: str, message: str) -> bool:
        self.send_call_count += 1
        return True


class MockClaudeRoutineClient:
    def __init__(self, task: Optional[CloudTask] = None):
        self.current_task: Optional[CloudTask] = task or CloudTask(
            task_id="claude-session-1",
            state=CloudTaskState.RUNNING,
            updated_at=datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc),
        )
        self.send_call_count = 0
        self.token = "mock-token"

    def get_task(self, task_id: str) -> Optional[CloudTask]:
        return self.current_task

    def send_followup(self, task_id: str, message: str) -> bool:
        self.send_call_count += 1
        return True


# ---------------------------------------------------------------------------
# AS-001 — Real accepted follow-up, then completion (REQ-001..004, REQ-007, 010, 011)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["local", "jules", "codex-cloud", "claude-routine"])
def test_as001_real_accepted_followup_then_completion(coordinator: ProviderRepairCoordinator, provider: str) -> None:
    """AS-001: For each provider and local execution, drive the supported repair entry

    with an immutable bundle and admitted generation, observe acceptance, terminal
    correction, and capture evidence into validation input before reviewer validation.
    """
    blocker_id = f"blk_{provider}_001"
    bundle = _make_bundle(f"bnd_{provider}_001", (blocker_id,))
    owner_id = f"task_{provider}_100" if provider != "local" else "inv_local_100"

    # Setup provider client mock
    client: Any = None
    if provider == "codex-cloud":
        client = MockCodexCloudClient([MockWhamTurn("asst_turn_0", "assistant")])
    elif provider == "jules":
        client = MockJulesClient(
            {
                "id": owner_id,
                "state": "IN_PROGRESS",
                "updateTime": "2026-09-19T01:00:00Z",
                "messages": [],
            }
        )
    elif provider == "claude-routine":
        client = MockClaudeRoutineClient(
            CloudTask(
                task_id=owner_id,
                state=CloudTaskState.RUNNING,
                updated_at=datetime(2026, 9, 19, 1, 0, 0, tzinfo=timezone.utc),
            )
        )
    elif provider == "local":
        client = lambda prompt: True

    # 1. Bind admission ticket before outbound boundary (REQ-002)
    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, provider, owner_id, client=client)
    assert ticket.status == TicketStatus.BOUND
    assert ticket.generation_id.startswith("gen_")
    assert ticket.bundle_id == bundle.bundle_id
    assert ticket.owner_id == owner_id

    # Check that generation is RESERVED in allowance ledger
    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.RESERVED

    # 2. Dispatch repair (REQ-002, REQ-003)
    deliv_obs = coordinator.dispatch_repair(ticket, "Fix the issue", owner_id, snapshot.epoch, client=client)
    assert deliv_obs.delivery_status == DeliveryStatus.CONFIRMED
    assert deliv_obs.receipt_identity != ""

    # Check generation is CONFIRMED_DELIVERED
    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.CONFIRMED_DELIVERED

    # 3. Simulate remote completion causally after baseline (REQ-004)
    if provider == "codex-cloud":
        client.turns.append(MockWhamTurn("user_turn_1", "user", content="Fix the issue"))
        client.turns.append(MockWhamTurn("asst_turn_1", "assistant", content="Fixed the issue with code change"))
    elif provider == "jules":
        client.session_data = {
            "id": owner_id,
            "state": "COMPLETED",
            "updateTime": "2026-09-19T02:00:00Z",
            "outputs": {"pullRequest": "https://github.com/owner/repo/pull/1"},
        }
    elif provider == "claude-routine":
        client.current_task = CloudTask(
            task_id=owner_id,
            state=CloudTaskState.COMPLETED,
            updated_at=datetime(2026, 9, 19, 2, 0, 0, tzinfo=timezone.utc),
            pull_request="https://github.com/owner/repo/pull/1",
        )
    elif provider == "local":
        client = {"status": "completed", "output": "Successfully fixed code", "code_changed": True}

    corr_obs = coordinator.observe_and_correlate_completion(ticket, client=client)
    assert corr_obs.completion_status == CorrectionCompletionStatus.COMPLETED
    assert corr_obs.code_changed is True
    assert corr_obs.provider_native_ref != ""

    # Check generation advanced to PENDING_REVALIDATION
    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.PENDING_REVALIDATION

    # 4. Capture validation evidence before reviewer runs (REQ-007)
    val_binding = coordinator.capture_validation_evidence(ticket, HEAD_SHA)
    assert val_binding.generation_id == ticket.generation_id
    assert val_binding.provider_native_ref == corr_obs.provider_native_ref
    assert not val_binding.is_settled

    # 5. Settle validation results (REQ-007)
    validations = [
        ValidationObservation(
            blocker_id=blocker_id,
            still_unmet=False,  # Successfully corrected
            availability=ValidationAvailability.KNOWN,
            validation_seq=val_binding.completion_seq + 1,
            evidence="Code verified on current head",
        )
    ]
    settled_snapshot = coordinator.settle_validation_results(val_binding, validations)
    settled_gen = settled_snapshot.get_generation(ticket.generation_id)
    assert settled_gen is not None
    assert settled_gen.lifecycle_state == GenerationLifecycleState.SETTLED

    # Check blocker settlement
    settlement = settled_gen.get_settlement(blocker_id)
    assert settlement is not None
    assert settlement.settlement.value == "CORRECTED"
    assert settlement.charged is False


# ---------------------------------------------------------------------------
# AS-002 — Known, unavailable, same known state (REQ-004..REQ-006, REQ-008)
# ---------------------------------------------------------------------------


def test_as002_known_unavailable_same_known_state(coordinator: ProviderRepairCoordinator) -> None:
    """AS-002: Retain old completed task/activity. Make observation unavailable

    during send, then reveal same old completion with refreshed timestamp.
    Must not count as completing new repair. Repeat with old PAUSED state
    and native completion from another task.
    """
    blocker_id = "blk_as002"
    bundle = _make_bundle("bnd_as002", (blocker_id,))
    owner_id = "jules-session-as002"

    old_session = {
        "id": owner_id,
        "state": "COMPLETED",
        "updateTime": "2026-09-19T01:00:00Z",
        "outputs": {"pullRequest": "pr-1"},
    }
    client = MockJulesClient(old_session)

    # 1. Bind ticket and dispatch repair
    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.JULES.value, owner_id, client=client)
    coordinator.dispatch_repair(ticket, "Fix please", owner_id, ticket.expected_epoch, client=client)

    # Observation is temporarily unavailable or returns same old timestamp
    corr_obs = coordinator.observe_and_correlate_completion(ticket, client=client)
    assert corr_obs.completion_status == CorrectionCompletionStatus.UNAVAILABLE

    # Reveal same old state with refreshed timestamp (same updateTime <= baseline)
    old_session["updateTime"] = "2026-09-19T01:00:00Z"  # Still at baseline
    corr_obs_refreshed = coordinator.observe_and_correlate_completion(ticket, client=client)
    assert corr_obs_refreshed.completion_status == CorrectionCompletionStatus.UNAVAILABLE

    # Generation remains in CONFIRMED_DELIVERED, not completed
    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.CONFIRMED_DELIVERED

    # 2. Test PAUSED state: PAUSED must never count as completed correction (REQ-005)
    client.session_data = {
        "id": owner_id,
        "state": "AWAITING_USER_FEEDBACK",
        "updateTime": "2026-09-19T03:00:00Z",  # Advanced timestamp
    }
    corr_obs_paused = coordinator.observe_and_correlate_completion(ticket, client=client)
    assert corr_obs_paused.completion_status == CorrectionCompletionStatus.UNAVAILABLE

    # 3. Test completion from another task (task ID mismatch) (REQ-006)
    client.session_data = {
        "id": "foreign-task-999",
        "state": "COMPLETED",
        "updateTime": "2026-09-19T04:00:00Z",
        "outputs": {"pullRequest": "pr-foreign"},
    }
    corr_obs_foreign = coordinator.observe_and_correlate_completion(ticket, client=client)
    assert corr_obs_foreign.completion_status == CorrectionCompletionStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# AS-003 — Intervening request and no-code completion (REQ-004..REQ-006)
# ---------------------------------------------------------------------------


def test_as003_intervening_competing_request_induces_ambiguity(coordinator: ProviderRepairCoordinator) -> None:
    """AS-003: After the admitted request, introduce a competing manual request

    in the same provider task. An otherwise uncorrelated terminal activity
    must remain ambiguous.
    """
    blocker_id = "blk_as003_interv"
    bundle = _make_bundle("bnd_as003_interv", (blocker_id,))
    owner_id = "task_codex_interv"

    client = MockCodexCloudClient([MockWhamTurn("asst_turn_base", "assistant")])
    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.CODEX_CLOUD.value, owner_id, client=client)
    coordinator.dispatch_repair(ticket, "Auto-Coder fix", owner_id, ticket.expected_epoch, client=client)

    # Introduce admitted request, then a competing manual request, then a completed assistant turn
    client.turns.append(MockWhamTurn("user_auto_coder", "user", content="Auto-Coder fix"))
    client.turns.append(MockWhamTurn("user_competing_manual", "user", content="Manual developer command"))
    client.turns.append(MockWhamTurn("asst_turn_final", "assistant", content="Done something"))

    corr_obs = coordinator.observe_and_correlate_completion(ticket, client=client)
    assert corr_obs.completion_status == CorrectionCompletionStatus.AMBIGUOUS
    assert "Competing intervening" in corr_obs.evidence

    # Generation does NOT advance
    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.CONFIRMED_DELIVERED


def test_as003_no_code_completion_is_eligible_correction(coordinator: ProviderRepairCoordinator) -> None:
    """AS-003: Positively correlated response stating inability / no code change:

    it is a completed correction eligible for later independent assessment,
    not proof of success and not dependent on a new commit (code_changed=False).
    """
    blocker_id = "blk_as003_nocode"
    bundle = _make_bundle("bnd_as003_nocode", (blocker_id,))
    owner_id = "task_codex_nocode"

    client = MockCodexCloudClient([MockWhamTurn("asst_turn_base", "assistant")])
    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.CODEX_CLOUD.value, owner_id, client=client)
    coordinator.dispatch_repair(ticket, "Fix problem", owner_id, ticket.expected_epoch, client=client)

    # Provider returns explicit inability / CANNOT_FIX
    client.turns.append(MockWhamTurn("user_prompt", "user", content="Fix problem"))
    client.turns.append(MockWhamTurn("asst_nocode", "assistant", content="CANNOT_FIX: unable to resolve requirement"))

    corr_obs = coordinator.observe_and_correlate_completion(ticket, client=client)
    assert corr_obs.completion_status == CorrectionCompletionStatus.COMPLETED
    assert corr_obs.code_changed is False
    assert corr_obs.is_no_change_result is True

    # Generation advances to PENDING_REVALIDATION
    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.PENDING_REVALIDATION
    assert gen.completion_code_changed is False

    # Validation can now be captured and settled
    val_binding = coordinator.capture_validation_evidence(ticket, HEAD_SHA)
    validations = [
        ValidationObservation(
            blocker_id=blocker_id,
            still_unmet=True,  # Blocker remains unmet
            availability=ValidationAvailability.KNOWN,
            validation_seq=val_binding.completion_seq + 1,
            evidence="Blocker still present as model reported CANNOT_FIX",
        )
    ]
    settled_snapshot = coordinator.settle_validation_results(val_binding, validations)
    settlement = settled_snapshot.get_generation(ticket.generation_id).get_settlement(blocker_id)
    assert settlement is not None
    assert settlement.settlement.value == "STILL_OPEN"
    assert settlement.charged is True  # Charged because it went through full completed generation!


# ---------------------------------------------------------------------------
# AS-004 — Lost acceptance and quota refusal are different (REQ-002, 003, 008, 009)
# ---------------------------------------------------------------------------


def test_as004_quota_refusal_is_definite_non_delivery(coordinator: ProviderRepairCoordinator) -> None:
    """AS-004: First refuse the request at pre-send quota boundary:

    no corrective work is admitted remotely and no failure is charged.
    Generation returns to RESERVED.
    """
    blocker_id = "blk_as004_quota"
    bundle = _make_bundle("bnd_as004_quota", (blocker_id,))
    owner_id = "task_codex_quota"

    client = MockCodexCloudClient([MockWhamTurn("asst_base", "assistant")])
    client.quota_insufficient = True  # Triggers AutoCoderUsageLimitError

    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.CODEX_CLOUD.value, owner_id, client=client)
    deliv_obs = coordinator.dispatch_repair(ticket, "Fix", owner_id, ticket.expected_epoch, client=client)

    assert deliv_obs.delivery_status == DeliveryStatus.DEFINITE_NON_DELIVERY

    # Generation remains RESERVED so it can retry delivery without allocating a new generation
    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.RESERVED

    # No blocker failure charged
    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.failed_count == 0


def test_as004_generic_false_or_lost_response_is_indeterminate(coordinator: ProviderRepairCoordinator) -> None:
    """AS-004: A generic client False without transport certainty must NOT

    follow definite refusal path. It is INDETERMINATE, holding the slot.
    """
    blocker_id = "blk_as004_indet"
    bundle = _make_bundle("bnd_as004_indet", (blocker_id,))
    owner_id = "task_jules_indet"

    client = MockJulesClient()
    client.send_followup = MagicMock(return_value=False)  # Generic False

    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.JULES.value, owner_id, client=client)
    deliv_obs = coordinator.dispatch_repair(ticket, "Fix", owner_id, ticket.expected_epoch, client=client)

    assert deliv_obs.delivery_status == DeliveryStatus.INDETERMINATE

    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.INDETERMINATE

    # Outstanding slot is held, blocking another speculative admission (REQ-003)
    bundle_new = _make_bundle("bnd_speculative", (blocker_id,))
    with pytest.raises(AdmissionRefusalError, match="already has outstanding generation"):
        coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle_new, ProviderType.JULES.value, owner_id, client=client)


# ---------------------------------------------------------------------------
# AS-005 — Validation captured too early (REQ-006, REQ-007)
# ---------------------------------------------------------------------------


def test_as005_validation_captured_too_early_cannot_settle(coordinator: ProviderRepairCoordinator) -> None:
    """AS-005: Capture a review input before generation G completes.

    Complete G, then return old review result. It cannot settle G.
    Only a validation captured with G's completion evidence can settle it.
    """
    blocker_id = "blk_as005"
    bundle = _make_bundle("bnd_as005", (blocker_id,))
    owner_id = "task_claude_as005"

    client = MockClaudeRoutineClient()
    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.CLAUDE_ROUTINE.value, owner_id, client=client)
    coordinator.dispatch_repair(ticket, "Fix", owner_id, ticket.expected_epoch, client=client)

    # Attempt to capture validation evidence BEFORE completion observation:
    from auto_coder.provider_repair_correlation import CorrelationUnavailableError

    with pytest.raises(CorrelationUnavailableError, match="expected PENDING_REVALIDATION"):
        coordinator.capture_validation_evidence(ticket, HEAD_SHA)

    # Now simulate generation G completing
    client.current_task = CloudTask(
        task_id=owner_id,
        state=CloudTaskState.COMPLETED,
        updated_at=datetime(2026, 9, 19, 5, 0, 0, tzinfo=timezone.utc),
        pull_request="pr-url",
    )
    coordinator.observe_and_correlate_completion(ticket, client=client)

    # Now capture validation evidence with G's completion evidence
    val_binding = coordinator.capture_validation_evidence(ticket, HEAD_SHA)
    assert val_binding.generation_id == ticket.generation_id

    # Settle with validation observations
    validations = [
        ValidationObservation(
            blocker_id=blocker_id,
            still_unmet=False,
            availability=ValidationAvailability.KNOWN,
            validation_seq=val_binding.completion_seq + 1,
            evidence="Independently verified",
        )
    ]
    settled_snapshot = coordinator.settle_validation_results(val_binding, validations)
    settled_gen = settled_snapshot.get_generation(ticket.generation_id)
    assert settled_gen is not None
    assert settled_gen.lifecycle_state == GenerationLifecycleState.SETTLED


# ---------------------------------------------------------------------------
# AS-006 — Persistence and owner changes (REQ-002, REQ-007..REQ-010)
# ---------------------------------------------------------------------------


def test_as006_persistence_failure_refuses_provider_call(coordinator: ProviderRepairCoordinator) -> None:
    """AS-006: Fail baseline/admission persistence and assert no provider call."""
    blocker_id = "blk_as006_fail"
    bundle = _make_bundle("bnd_as006_fail", (blocker_id,))
    owner_id = "task_codex_as006"

    client = MockCodexCloudClient([MockWhamTurn("asst_base", "assistant")])

    # Simulate persistence failure in correlation store
    coordinator.correlation_store._simulate_failure_before_commit = True

    with pytest.raises(AdmissionTicketPersistenceError):
        coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.CODEX_CLOUD.value, owner_id, client=client)

    # Provider send was NEVER called
    assert client.send_call_count == 0


def test_as006_owner_or_epoch_change_refuses_dispatch(coordinator: ProviderRepairCoordinator) -> None:
    """AS-006: Change authoritative task/epoch before queued send is released

    and assert refusal rather than delivery to old owner.
    """
    blocker_id = "blk_as006_owner"
    bundle = _make_bundle("bnd_as006_owner", (blocker_id,))
    owner_id = "task_jules_as006"

    client = MockJulesClient()
    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.JULES.value, owner_id, client=client)

    # Attempt dispatch with a different owner task
    with pytest.raises(OwnerMismatchError, match="Authoritative owner task changed"):
        coordinator.dispatch_repair(ticket, "Fix", "different_owner_task_999", ticket.expected_epoch, client=client)

    # Attempt dispatch with a stale epoch
    with pytest.raises(StaleTicketEpochError, match="Namespace epoch changed"):
        coordinator.dispatch_repair(ticket, "Fix", owner_id, ticket.expected_epoch + 1, client=client)

    assert client.send_call_count == 0


# ---------------------------------------------------------------------------
# Standalone REQ-008 & REQ-009 Regressions
# ---------------------------------------------------------------------------


def test_req008_reconstruction_after_restart(db_paths: tuple[Path, Path]) -> None:
    """REQ-008: Reconstruct bindings and observations after restart from durable store."""
    allowance_db, corr_db = db_paths
    ledger = RepairAllowanceLedger(db_path=allowance_db)
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    corr_store = ProviderRepairCorrelationStore(db_path=corr_db)
    coordinator = ProviderRepairCoordinator(ledger, corr_store)

    blocker_id = "blk_req008"
    bundle = _make_bundle("bnd_req008", (blocker_id,))
    owner_id = "task_req008"

    client = MockCodexCloudClient([MockWhamTurn("asst_1", "assistant")])
    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.CODEX_CLOUD.value, owner_id, client=client)
    coordinator.dispatch_repair(ticket, "Fix", owner_id, ticket.expected_epoch, client=client)

    # Simulate restart by creating new store and coordinator instances
    new_ledger = RepairAllowanceLedger(db_path=allowance_db)
    new_corr_store = ProviderRepairCorrelationStore(db_path=corr_db)
    new_coordinator = ProviderRepairCoordinator(new_ledger, new_corr_store)

    # Reconstructed ticket
    reconstructed_ticket = new_corr_store.get_admission_ticket(ticket.ticket_id)
    assert reconstructed_ticket is not None
    assert reconstructed_ticket.ticket_id == ticket.ticket_id
    assert reconstructed_ticket.generation_id == ticket.generation_id
    assert reconstructed_ticket.owner_id == owner_id
    assert reconstructed_ticket.status == TicketStatus.DELIVERED

    # Reconstructed generation in allowance ledger
    snapshot = new_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.CONFIRMED_DELIVERED


def test_req001_provider_routing_preserves_canonical_blocker_and_allowance_identity(
    coordinator: ProviderRepairCoordinator,
) -> None:
    """REQ-001: Provider routing across Codex Cloud, Jules, and Local must NOT

    change canonical blocker or allowance identity. Allowance failure counts
    accumulate across provider switches up to exhaustion.
    """
    blocker_id = "blk_req001_canonical"
    bundle = _make_bundle("bnd_req001", (blocker_id,))

    # Generation 1: via Codex Cloud
    client_codex = MockCodexCloudClient([MockWhamTurn("t0", "assistant")])
    t1 = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.CODEX_CLOUD.value, "task_codex", client=client_codex)
    s1 = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    coordinator.dispatch_repair(t1, "Fix 1", "task_codex", s1.epoch, client=client_codex)
    client_codex.turns.append(MockWhamTurn("u1", "user"))
    client_codex.turns.append(MockWhamTurn("a1", "assistant", content="attempt 1"))
    coordinator.observe_and_correlate_completion(t1, client=client_codex)
    vb1 = coordinator.capture_validation_evidence(t1, HEAD_SHA)
    s1 = coordinator.settle_validation_results(vb1, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, validation_seq=vb1.completion_seq + 1)])
    assert s1.get_blocker_allowance(blocker_id).failed_count == 1

    # Generation 2: via Jules
    client_jules = MockJulesClient({"id": "task_jules", "state": "IN_PROGRESS", "updateTime": "2026-09-19T01:00:00Z"})
    t2 = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.JULES.value, "task_jules", client=client_jules)
    s2 = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    coordinator.dispatch_repair(t2, "Fix 2", "task_jules", s2.epoch, client=client_jules)
    client_jules.session_data = {
        "id": "task_jules",
        "state": "COMPLETED",
        "updateTime": "2026-09-19T02:00:00Z",
        "outputs": {"pullRequest": "pr-2"},
    }
    coordinator.observe_and_correlate_completion(t2, client=client_jules)
    vb2 = coordinator.capture_validation_evidence(t2, HEAD_SHA)
    s2 = coordinator.settle_validation_results(vb2, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, validation_seq=vb2.completion_seq + 1)])
    assert s2.get_blocker_allowance(blocker_id).failed_count == 2

    # Generation 3: via Local
    client_local = {"status": "completed", "output": "CANNOT_FIX", "code_changed": False}
    t3 = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.LOCAL.value, "inv_local_3", client=None)
    s3 = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    coordinator.dispatch_repair(t3, "Fix 3", "inv_local_3", s3.epoch, client=None)
    coordinator.observe_and_correlate_completion(t3, client=client_local)
    vb3 = coordinator.capture_validation_evidence(t3, HEAD_SHA)
    s3 = coordinator.settle_validation_results(vb3, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, validation_seq=vb3.completion_seq + 1)])

    # 3 failures reached: canonical blocker is now EXHAUSTED
    allowance = s3.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.failed_count == 3
    assert allowance.status == RepairAllowanceStatus.EXHAUSTED

    # A 4th repair is denied regardless of provider
    with pytest.raises(AdmissionRefusalError, match="exhausted open blocker"):
        coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.LOCAL.value, "inv_local_4", client=None)


def test_req009_quota_outcomes_retain_existing_owner_task(coordinator: ProviderRepairCoordinator) -> None:
    """REQ-009: Adapt detailed quota/admission outcomes where the provider supplies them;

    retain existing owner task for repair, never create a replacement task/PR to evade
    uncertain delivery or exhaustion, and never mark remotely running task completed.
    """
    blocker_id = "blk_req009_quota"
    bundle = _make_bundle("bnd_req009", (blocker_id,))
    owner_id = "task_codex_orig"

    client = MockCodexCloudClient([MockWhamTurn("base", "assistant")])
    client.quota_insufficient = True

    ticket = coordinator.bind_admission_ticket(API_ORIGIN, REPO, PR_NUMBER, bundle, ProviderType.CODEX_CLOUD.value, owner_id, client=client)
    obs = coordinator.dispatch_repair(ticket, "Fix", owner_id, ticket.expected_epoch, client=client)

    assert obs.delivery_status == DeliveryStatus.DEFINITE_NON_DELIVERY
    assert ticket.owner_id == owner_id  # Owner task retained

    # The running task is not marked completed or released
    snapshot = coordinator.allowance_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    gen = snapshot.get_generation(ticket.generation_id)
    assert gen is not None
    assert gen.lifecycle_state == GenerationLifecycleState.RESERVED
