"""Maintained production-origin contracts for PR lifecycle extraction.

These tests deliberately enter through production functions and replace only
GitHub/provider/Git effect boundaries.  They are selectable as a stable group
with ``-m pr_lifecycle_contract``.
"""

import subprocess
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository
from auto_coder.adversarial_validator import AdversarialValidationResult, format_adversarial_validation_comment
from auto_coder.automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from auto_coder.ci_observation import (
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from auto_coder.cloud_manager import CloudTaskBinding
from auto_coder.durable_repair_allowance import RepairAllowanceLedger
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.local_review_repair import LocalReviewRepairRequest, admit_local_repair_allowance
from auto_coder.pr_processor import (
    ClaimedReviewThreadGateState,
    CloudTaskOrigin,
    CloudTaskOriginResolution,
    CodexCloudFeedbackResult,
    PRActionList,
    PRReviewCoordinationResult,
    TwoTierGateInputs,
    _handle_pr_merge,
)
from auto_coder.pr_repair_dispatch import PRRepairResult, RepairDisposition, RepairFactState, RepairOrigin
from auto_coder.pr_review_cycle import VERDICT_FINDINGS, ContractSnapshot, Finding, PrReviewCycleRepository, RoundProvenance, StrongPolicyIdentity
from auto_coder.two_tier_pr_gate import TwoTierPrGate
from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult, _check_github_actions_status
from auto_coder.utils import CommandResult

pytestmark = pytest.mark.pr_lifecycle_contract

REPOSITORY = "owner/repo"
PR_NUMBER = 2501
H1 = "1" * 40
H2 = "2" * 40


def _snapshot(
    availability: ObservationAvailability,
    conclusion: CIConclusion | None = None,
    *,
    head_sha: str = H1,
    attempt: int = 1,
) -> CIObservationSnapshot:
    facts = ()
    if conclusion is not None:
        facts = (
            WorkflowObservation(
                WorkflowExecutionIdentity("pr-tests", "701", attempt),
                conclusion,
                "PR Tests",
                workflow_path=".github/workflows/pr-tests.yml",
            ),
        )
    return CIObservationSnapshot(
        ObservationSubject("https://api.github.test", REPOSITORY, PR_NUMBER, head_sha),
        ObservationRequest("github-actions", "checks+workflows"),
        f"cycle-{availability.value}-{attempt}",
        0,
        availability,
        facts,
        unavailable_reason="provider unavailable" if availability is ObservationAvailability.UNAVAILABLE else None,
    )


@pytest.mark.parametrize(
    ("snapshot", "success", "pending", "error", "ids"),
    [
        (_snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS), True, False, None, [701]),
        (_snapshot(ObservationAvailability.KNOWN, CIConclusion.PENDING), False, True, None, [701]),
        (_snapshot(ObservationAvailability.KNOWN_EMPTY), False, True, "No current CI observations", []),
        (_snapshot(ObservationAvailability.UNAVAILABLE), False, False, "provider unavailable", []),
        (_snapshot(ObservationAvailability.SUPERSEDED), False, False, "superseded", []),
    ],
)
def test_real_ci_interpreter_preserves_non_authorizing_observations(snapshot, success, pending, error, ids):
    """REQ-001/002: GitHub-shaped observations reach the real interpreter."""
    client = SimpleNamespace(token="controlled-token")
    pr_data = {"number": PR_NUMBER, "head": {"sha": H1}}

    with patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()), patch("auto_coder.util.github_action.observe_ci", return_value=snapshot):
        result = _check_github_actions_status(REPOSITORY, pr_data, AutomationConfig(), client)

    assert result.success is success
    assert result.in_progress is pending
    assert result.error == error
    assert result.ids == ids
    assert result.observation is snapshot


def test_real_ci_interpreter_uses_only_newest_workflow_attempt():
    """REQ-002: a pending H2 observation supersedes an older green attempt."""
    old = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS).facts[0]
    new = WorkflowObservation(
        WorkflowExecutionIdentity("pr-tests", "701", 2),
        CIConclusion.PENDING,
        "PR Tests",
        workflow_path=".github/workflows/pr-tests.yml",
    )
    snapshot = CIObservationSnapshot(
        ObservationSubject("https://api.github.test", REPOSITORY, PR_NUMBER, H2),
        ObservationRequest("github-actions", "checks+workflows"),
        "cycle-h2",
        0,
        ObservationAvailability.KNOWN,
        (old, new),
    )
    client = SimpleNamespace(token="controlled-token")

    with patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()), patch("auto_coder.util.github_action.observe_ci", return_value=snapshot):
        result = _check_github_actions_status(REPOSITORY, {"number": PR_NUMBER, "head": {"sha": H2}}, AutomationConfig(), client)

    assert result.success is False
    assert result.in_progress is True
    assert result.ids == [701]


@pytest.mark.parametrize("obsolete", [CIConclusion.FAILURE, CIConclusion.PENDING])
def test_handler_preserves_real_interpreter_newest_attempt_semantics(tmp_path, monkeypatch, obsolete):
    """REQ-005: obsolete attempts cannot override the interpreted green route."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    old = WorkflowObservation(
        WorkflowExecutionIdentity("pr-tests", "701", 1),
        obsolete,
        "PR Tests",
        workflow_path=".github/workflows/pr-tests.yml",
    )
    newest = WorkflowObservation(
        WorkflowExecutionIdentity("pr-tests", "701", 2),
        CIConclusion.SUCCESS,
        "PR Tests",
        workflow_path=".github/workflows/pr-tests.yml",
    )
    snapshot = CIObservationSnapshot(
        ObservationSubject("https://api.github.test", REPOSITORY, PR_NUMBER, H1),
        ObservationRequest("github-actions", "checks+workflows"),
        "cycle-newest-attempt",
        0,
        ObservationAvailability.KNOWN,
        (old, newest),
    )
    config = AutomationConfig()
    config.AUTO_MERGE = False
    pr_data = {"number": PR_NUMBER, "body": "Fixes #2503", "labels": [], "head": {"ref": "feature", "sha": H1}}
    client = MagicMock(token="controlled-token")

    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
        patch("auto_coder.util.github_action.observe_ci", return_value=snapshot),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.util.github_action.GitHubClient.get_instance", return_value=client),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.get_detailed_checks_from_history") as failure_repair,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})

    assert f"All GitHub Actions checks passed for PR #{PR_NUMBER}" in actions
    assert not any("still in progress" in action for action in actions)
    failure_repair.assert_not_called()


@pytest.mark.parametrize(
    ("enabled", "exhausted", "expected_calls", "expected_text"),
    [
        (True, False, 1, "controlled repair boundary"),
        (False, False, 0, "not mergeable"),
        (True, True, 0, "repair allowance exhausted"),
    ],
)
def test_direct_processing_routes_conflicts_only_to_authorized_effect_boundary(tmp_path, monkeypatch, enabled, exhausted, expected_calls, expected_text):
    """REQ-001/002: direct processing selects the existing conflict boundary."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = enabled
    pr_data = {
        "number": PR_NUMBER,
        "body": "Closes #2501",
        "labels": [],
        "head": {"ref": "issue-2501", "sha": H1},
    }
    exhaustion = SimpleNamespace(is_exhausted=True, exhausted_blocker_ids=("conflict",)) if exhausted else None
    client = MagicMock()

    remediation_actions = PRActionList(("controlled repair boundary",))
    with (
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": False, "merge_state_status": "dirty"}),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=exhaustion),
        patch("auto_coder.pr_processor.publish_exhaustion_comment_deduped"),
        patch("auto_coder.pr_processor._start_mergeability_remediation", return_value=remediation_actions) as repair,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})

    assert repair.call_count == expected_calls
    assert any(expected_text in action for action in actions)


@pytest.mark.parametrize(
    ("disposition", "expected_outcome", "retry_not_before"),
    [
        (RepairDisposition.FAILED, PRProcessingOutcome.FAILED, None),
        (RepairDisposition.DEFERRED, PRProcessingOutcome.DEFERRED, 4321.0),
    ],
)
def test_public_conflict_facade_preserves_typed_remediation_result(
    tmp_path,
    monkeypatch,
    disposition,
    expected_outcome,
    retry_not_before,
):
    """REQ-006: diagnostic wording cannot replace the typed conflict outcome."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": PR_NUMBER, "body": "Closes #2501", "labels": [], "head": {"ref": "issue-2501", "sha": H1}}
    client = MagicMock()
    result = PRRepairResult(disposition, ("wording intentionally unrelated",), reason="typed reason", retry_not_before=retry_not_before)
    remediation = PRActionList(("arbitrary diagnostic",))
    remediation.repair_result = result
    remediation.quota_deferred = disposition is RepairDisposition.DEFERRED
    remediation.retry_not_before = retry_not_before
    status = ProcessedPRResult(pr_data=pr_data, actions_taken=[], priority="fix", analysis=None)

    with (
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": False, "merge_state_status": "dirty"}),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
        patch("auto_coder.pr_processor._start_mergeability_remediation", return_value=remediation),
        patch("auto_coder.pr_processor._merge_pr") as merge,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {}, status)

    assert actions.repair_result is result
    assert status.outcome is expected_outcome
    assert status.retry_not_before == retry_not_before
    assert status.error == ("typed reason" if disposition is RepairDisposition.FAILED else None)
    merge.assert_not_called()


def _run_ci_consumer(snapshot, *, remote_head=H1, thread_state=None, body="Fixes #99", ledger=None):
    """Enter the merge route while controlling only provider/effect boundaries."""
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = ledger is not None
    config.pr_adversarial_validation = ledger is not None
    config.pr_review_thread_gate = thread_state is not None
    pr_data = {"number": PR_NUMBER, "body": body, "labels": [], "head": {"ref": "feature", "sha": H1}}
    client = MagicMock(token="controlled-token")
    client.get_pull_request.return_value = {"head": {"sha": remote_head}}
    cycles = iter(range(20))

    def observe(*_args, **_kwargs):
        return replace(snapshot, cycle_id=f"{snapshot.cycle_id}-{next(cycles)}")

    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
        patch("auto_coder.util.github_action.GitHubClient.get_instance", return_value=client),
        patch("auto_coder.util.github_action.observe_ci", side_effect=observe),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=thread_state is not None),
        patch("auto_coder.pr_processor._is_pr_adversarial_validation_enabled", return_value=ledger is not None),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=thread_state or ClaimedReviewThreadGateState()),
        patch("auto_coder.durable_repair_allowance.RepairAllowanceLedger", return_value=ledger),
        patch("auto_coder.pr_processor._verify_pending_local_correction") as local_verification,
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
        patch("auto_coder.pr_processor.ci_observation_merge_authority", return_value=nullcontext(True)),
        patch("auto_coder.pr_processor._merge_pr", return_value=True) as merge,
        patch("auto_coder.pr_processor.get_detailed_checks_from_history") as failure_consumer,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})
    return actions, merge, failure_consumer, local_verification


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(ObservationAvailability.KNOWN, CIConclusion.PENDING),
        _snapshot(ObservationAvailability.KNOWN_EMPTY),
        _snapshot(ObservationAvailability.UNAVAILABLE),
        _snapshot(ObservationAvailability.PARTIAL),
    ],
    ids=["pending", "known-empty", "unavailable", "partial"],
)
def test_non_authorizing_ci_reaches_consumer_without_merge_or_failure_repair(snapshot):
    """REQ-002/003: incomplete evidence is deferral, never merge or repair."""
    _, merge, failure_consumer, _ = _run_ci_consumer(snapshot)
    merge.assert_not_called()
    failure_consumer.assert_not_called()


def test_known_empty_handler_preserves_ci_wait_without_dispatch(tmp_path, monkeypatch):
    """REQ-005: real empty CI decoding retains the handler's wait/recheck."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    pr_data = {
        "number": PR_NUMBER,
        "body": "Fixes #2503",
        "labels": [],
        "head": {"ref": "issue-2503", "sha": H1},
    }
    client = MagicMock(token="controlled-token")
    api = MagicMock()
    api.headers = {}
    api.checks.list_for_ref.return_value = {"check_runs": []}
    api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": []}

    with (
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.util.github_action.GitHubClient.get_instance", return_value=client),
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=api),
        patch("auto_coder.pr_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.get_dispatch_claim_store") as claim_store,
        patch("auto_coder.util.github_action.trigger_workflow_dispatch") as dispatch,
        patch("auto_coder.pr_processor._merge_pr") as merge,
        patch("auto_coder.pr_processor.get_detailed_checks_from_history") as failure_repair,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})

    assert any(f"GitHub Actions checks are still in progress for PR #{PR_NUMBER}" in action for action in actions)
    claim_store.assert_not_called()
    dispatch.assert_not_called()
    merge.assert_not_called()
    failure_repair.assert_not_called()
    from auto_coder.entity_invalidation import DurableInvalidationQueue

    queue = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    assert queue.seconds_until_next_ci(REPOSITORY) is not None


def test_green_ci_reaches_merge_but_h1_to_h2_change_fails_closed():
    """REQ-002/004: current green reaches merge; a changed remote head does not."""
    green = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS)
    current_actions, current_merge, current_failure, _ = _run_ci_consumer(green)
    stale_actions, stale_merge, stale_failure, _ = _run_ci_consumer(green, remote_head=H2)

    current_merge.assert_called_once()
    assert isinstance(current_actions, PRReviewCoordinationResult)
    assert current_actions.scope.repository == REPOSITORY
    assert current_actions.scope.pr_number == PR_NUMBER
    assert current_actions.scope.head_sha == H1
    assert current_actions.disposition.value == "MERGE_COMPLETED"
    assert any("Successfully merged" in action for action in current_actions)
    stale_merge.assert_not_called()
    assert any("head SHA changed" in action for action in stale_actions)
    current_failure.assert_not_called()
    stale_failure.assert_not_called()


def test_green_ci_with_blocking_review_thread_never_becomes_ci_failure_repair():
    """REQ-003: a review-gate deferral retains its route under green CI."""
    state = ClaimedReviewThreadGateState(
        blocking_unresolved=(ReviewThread(id="blocking-thread"),),
        has_blocking_unresolved=True,
    )

    actions, merge, failure_consumer, _ = _run_ci_consumer(
        _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS),
        thread_state=state,
    )

    merge.assert_not_called()
    failure_consumer.assert_not_called()
    assert any("unresolved review threads" in action for action in actions)


def test_settled_local_job_waits_for_independent_validation_at_green_ci(tmp_path):
    """REQ-003: local completion alone is never final merge authority."""
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    request = LocalReviewRepairRequest(
        repository=REPOSITORY,
        pr_number=PR_NUMBER,
        head_repository=REPOSITORY,
        head_ref="feature",
        head_sha=H1,
        feedback_identities=("blocking-thread",),
        prompt="repair the bounded finding",
    )
    authority, reason = admit_local_repair_allowance(request, ledger)
    assert reason == ""
    assert authority is not None
    authority.mark_invocation()
    authority.mark_completion(code_changed=True, evidence="published local correction")

    actions, merge, failure_consumer, local_verification = _run_ci_consumer(
        _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS),
        body="<!-- auto-coder:local-llm -->\nFixes #99",
        ledger=ledger,
    )

    local_verification.assert_called_once()
    merge.assert_not_called()
    failure_consumer.assert_not_called()
    assert not any("Successfully merged" in action for action in actions)


@pytest.mark.parametrize("strong_state", ["accepted-finding", "pending-audit"])
def test_green_ci_retains_durable_strong_review_gate(tmp_path, monkeypatch, strong_state):
    """REQ-003: accepted findings and pending Strong work remain merge gates."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    cycle = PrReviewCycleRepository(REPOSITORY, tmp_path / "review-cycle.json")
    gate = TwoTierPrGate(REPOSITORY, cycle)
    contract = ContractSnapshot(("#2501",), "Issue #2501 REQ-003: Retain independent review gates.")
    policy = StrongPolicyIdentity("strong-review", "model=controlled", "v1")
    inputs = TwoTierGateInputs(gate, contract, policy, H1, "base-1")
    gate.ordinary_pass(PR_NUMBER, H1, "base-1", contract)

    if strong_state == "accepted-finding":
        claim = cycle.claim_strong_audit(PR_NUMBER, RoundProvenance(H1, "base-1"), contract, policy)
        finding = Finding(
            finding_id="finding-contract",
            origin_round_id=claim.claim_id,
            requirement_ids=("#2501/REQ-003",),
            requirement_texts=("Retain independent review gates.",),
            counterexample="Merge while an accepted finding remains open.",
            expected_behavior="The accepted finding blocks merge.",
            actual_behavior="An extraction could bypass the finding store.",
            evidence="controlled production review state",
            affected_boundary="final merge admission",
            focused_regression_scenario="Process green CI with an open accepted finding.",
        )
        strong_round = cycle.record_strong_result(PR_NUMBER, claim.claim_id, VERDICT_FINDINGS, "strong/controlled", [finding])
        cycle.acknowledge_publication(PR_NUMBER, strong_round.round_id)

    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = True
    pr_data = {
        "number": PR_NUMBER,
        "body": "Fixes #2501",
        "labels": [],
        "head": {"ref": "feature", "sha": H1},
        "base": {"ref": "main", "sha": "base-1"},
    }
    client = MagicMock(token="controlled-token")
    client.get_pr_review_threads_strict.return_value = []
    client.get_pr_comments.return_value = [{"body": format_adversarial_validation_comment(AdversarialValidationResult(result="PASS"), H1)}]
    client.get_pull_request.return_value = {"head": {"sha": H1}, "base": {"sha": "base-1"}}
    green = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS)

    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
        patch("auto_coder.util.github_action.observe_ci", return_value=green),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor._is_pr_adversarial_validation_enabled", return_value=True),
        patch(
            "auto_coder.pr_processor._get_adversarial_validation_eligibility",
            return_value=SimpleNamespace(is_applicable=True, issue_numbers=(2501,), lookup_error=None),
        ),
        patch("auto_coder.pr_processor._two_tier_gate_inputs", return_value=inputs),
        patch(
            "auto_coder.pr_processor._resume_retained_closure",
            return_value=SimpleNamespace(
                unavailable=False,
                revalidate=False,
                ordinary_pass_retained=False,
                reason="accepted finding remains open",
            ),
        ),
        patch("auto_coder.pr_processor._execute_pending_strong_audit", return_value=(False, "strong audit remains pending")) as strong_audit,
        patch("auto_coder.pr_processor._consume_pending_two_tier_publication", return_value=(False, "no effect is ready")),
        patch("auto_coder.pr_processor._merge_pr") as merge,
        patch("auto_coder.pr_processor.get_detailed_checks_from_history") as failure_consumer,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})

    assert any("All GitHub Actions checks passed" in action for action in actions)
    merge.assert_not_called()
    failure_consumer.assert_not_called()
    if strong_state == "accepted-finding":
        assert any("Strong audit findings" in action for action in actions)
        strong_audit.assert_not_called()
        assert cycle.snapshot(PR_NUMBER).open_findings == (finding,)
    else:
        assert any("Two-tier review" in action for action in actions), actions
        strong_audit.assert_called_once()
        assert cycle.snapshot(PR_NUMBER).waiting_reason == "ordinary PASS recorded; strong audit required"


def test_head_change_crosses_observable_final_boundary_before_merge():
    """REQ-004: a head mutation ordered at the final checkpoint fences merge."""
    green = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS)
    reached_checkpoint = Event()
    release_checkpoint = Event()
    remote = {"head": H1}
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = False
    config.pr_adversarial_validation = False
    config.pr_review_thread_gate = False
    pr_data = {"number": PR_NUMBER, "body": "Closes #2501", "labels": [], "head": {"ref": "issue-2501", "sha": H1}}
    client = MagicMock(token="controlled-token")
    cycles = iter(range(20))

    def observe_remote(*_args):
        reached_checkpoint.set()
        assert release_checkpoint.wait(timeout=5)
        return {"head": {"sha": remote["head"]}}

    client.get_pull_request.side_effect = observe_remote
    outcome = []
    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
        patch("auto_coder.util.github_action.GitHubClient.get_instance", return_value=client),
        patch("auto_coder.util.github_action.observe_ci", side_effect=lambda *_args, **_kwargs: replace(green, cycle_id=f"{green.cycle_id}-{next(cycles)}")),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor._is_pr_adversarial_validation_enabled", return_value=False),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
        patch("auto_coder.pr_processor.ci_observation_merge_authority", return_value=nullcontext(True)),
        patch("auto_coder.pr_processor._merge_pr", return_value=True) as merge,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        worker = Thread(target=lambda: outcome.extend(_handle_pr_merge(client, REPOSITORY, pr_data, config, {})))
        worker.start()
        assert reached_checkpoint.wait(timeout=5)
        remote["head"] = H2
        release_checkpoint.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    merge.assert_not_called()
    assert any("head SHA changed" in action for action in outcome)


@pytest.mark.parametrize("register_newer", [True, False], ids=["newer-attempt", "current-attempt"])
def test_final_attempt_admission_serializes_newer_and_current_work(tmp_path, register_newer):
    """REQ-004: the final serialized attempt gate fences only stale work."""
    repository = AdversarialValidationAttemptRepository(REPOSITORY, tmp_path / "attempts.json")
    reached_checkpoint = Event()
    release_checkpoint = Event()

    class CheckpointRepository:
        def __init__(self, delegate):
            self.delegate = delegate
            self.transition_count = 0

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        @contextmanager
        def serialized_transition(self):
            self.transition_count += 1
            if self.transition_count == 2:
                reached_checkpoint.set()
                assert release_checkpoint.wait(timeout=5)
            with self.delegate.serialized_transition():
                yield

    checkpoint_repository = CheckpointRepository(repository)
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = True
    pr_data = {"number": PR_NUMBER, "body": "Fixes #99", "labels": [], "head": {"ref": "feature", "sha": H1}}
    client = MagicMock()
    client.get_pr_review_threads_strict.return_value = []
    client.get_pr_comments.return_value = [{"body": format_adversarial_validation_comment(AdversarialValidationResult(result="PASS"), H1)}]
    client.get_pull_request.return_value = {"head": {"sha": H1}}
    result = AdversarialValidationResult(result="PASS", summary="current gates passed")

    outcome = []
    ci_reads = iter(range(20))

    def current_ci_status(*_args, **_kwargs):
        snapshot = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS)
        return SimpleNamespace(
            success=True,
            ids=[701],
            in_progress=False,
            error=None,
            observation=replace(snapshot, cycle_id=f"{snapshot.cycle_id}-{next(ci_reads)}"),
        )

    with (
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_adversarial_validation_enabled", return_value=True),
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor.AdversarialValidationAttemptRepository", return_value=checkpoint_repository),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._check_github_actions_status", side_effect=current_ci_status),
        patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False),
        patch("auto_coder.pr_processor.run_adversarial_validation", return_value=result),
        patch(
            "auto_coder.pr_processor.publish_adversarial_review",
            return_value=ReviewPublicationResult(True, "APPROVE", ""),
        ),
        patch("auto_coder.pr_processor.ci_observation_merge_authority", return_value=nullcontext(True)),
        patch("auto_coder.pr_processor.isolated_pr_head_worktree", return_value=nullcontext(tmp_path)),
        patch("auto_coder.pr_processor._merge_pr", return_value=True) as merge,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        worker = Thread(target=lambda: outcome.extend(_handle_pr_merge(client, REPOSITORY, pr_data, config, {}, force_adversarial_validation=True)))
        worker.start()
        assert reached_checkpoint.wait(timeout=5)
        if register_newer:
            newer = repository.start(PR_NUMBER, H1)
            repository.finish(newer.attempt_id, "PASS")
        release_checkpoint.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    if register_newer:
        merge.assert_not_called()
        assert any("newer adversarial-validation attempt is applicable" in action for action in outcome), outcome
    else:
        assert merge.call_count == 1, outcome
        assert any("Successfully merged" in action for action in outcome)


def test_terminal_ci_failure_retains_cloud_owner_without_local_repair(tmp_path, monkeypatch):
    """REQ-003: current terminal CI failure continues through its cloud route."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    failed = _snapshot(ObservationAvailability.KNOWN, CIConclusion.FAILURE)
    config = AutomationConfig()
    pr_data = {
        "number": PR_NUMBER,
        "body": "Fixes #2501\nhttps://chatgpt.com/codex/tasks/task_e_ci_owner",
        "labels": [],
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "head": {"ref": "cloud/feature", "sha": H1},
        "base": {"ref": "main", "sha": "base-1"},
    }
    client = MagicMock(token="controlled-token")

    requests = []

    def capture_dispatch(request, operation):
        requests.append(request)
        return operation(request)

    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
        patch(
            "auto_coder.pr_processor._check_github_actions_status",
            return_value=GitHubActionsStatusResult(success=False, ids=[701], observation=failed),
        ),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch(
            "auto_coder.pr_processor._resolve_cloud_task_origin",
            return_value=CloudTaskOriginResolution(CloudTaskOrigin("codex-cloud", "task_e_ci_owner", MagicMock())),
        ),
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=SimpleNamespace(failed_checks=[{"name": "PR Tests"}])),
        patch(
            "auto_coder.pr_processor._send_codex_cloud_error_feedback",
            return_value=CodexCloudFeedbackResult(delivered=True, actions=("controlled cloud CI continuation",)),
        ) as cloud_continuation,
        patch("auto_coder.pr_processor.dispatch_pr_repair", side_effect=capture_dispatch),
        patch("auto_coder.pr_processor._apply_github_actions_fix") as local_repair,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})

    cloud_continuation.assert_called_once()
    assert len(requests) == 1
    assert requests[0].origin is RepairOrigin.CODEX_CLOUD
    assert requests[0].owner_identity == "task_e_ci_owner"
    local_repair.assert_not_called()
    assert any("GitHub Actions checks failed" in action for action in actions)
    assert "controlled cloud CI continuation" in actions
    assert any("Codex Cloud will handle fixing" in action for action in actions)


def test_terminal_ci_head_change_defers_codex_dispatch_without_provider_effect(tmp_path, monkeypatch):
    """REQ-003: exact-head authority loss is a wait, not an execution failure."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    failed = _snapshot(ObservationAvailability.KNOWN, CIConclusion.FAILURE)
    config = AutomationConfig()
    pr_data = {
        "number": PR_NUMBER,
        "body": "Fixes #2501\nhttps://chatgpt.com/codex/tasks/task_e_ci_owner",
        "labels": [],
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "head": {"ref": "cloud/feature", "sha": H1},
        "base": {"ref": "main", "sha": "base-1"},
    }
    github = MagicMock(token="controlled-token")
    provider = MagicMock()
    origin = CloudTaskOrigin("codex-cloud", "task_e_ci_owner", provider)
    dispatched = []

    def capture_dispatch(request, operation):
        result = operation(request)
        dispatched.append(result)
        return result

    status = ProcessedPRResult(pr_data=pr_data, actions_taken=[], priority="fix", analysis=None)
    denied = SimpleNamespace(allowed=False, reason="current PR head changed to H2", failure_identities=())
    with (
        patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, ids=[701], observation=failed)),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=SimpleNamespace(failed_checks=[{"name": "PR Tests"}])),
        patch("auto_coder.pr_processor._resolve_cloud_task_origin", return_value=CloudTaskOriginResolution(origin)),
        patch("auto_coder.pr_processor.current_ci_failure_authority", return_value=nullcontext(denied)),
        patch("auto_coder.pr_processor.dispatch_pr_repair", side_effect=capture_dispatch),
        patch("auto_coder.pr_processor._merge_pr") as merge,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        _handle_pr_merge(github, REPOSITORY, pr_data, config, {}, status)

    assert len(dispatched) == 1
    assert dispatched[0].disposition is RepairDisposition.DEFERRED
    assert dispatched[0].attempt_started is None
    assert status.outcome is PRProcessingOutcome.DEFERRED
    provider.continue_if_paused.assert_not_called()
    merge.assert_not_called()


def test_terminal_ci_jules_owner_enters_typed_dispatch_once(tmp_path, monkeypatch):
    """REQ-001: public Jules CI repair preserves its existing session owner."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    failed = _snapshot(ObservationAvailability.KNOWN, CIConclusion.FAILURE)
    config = AutomationConfig()
    pr_data = {
        "number": PR_NUMBER,
        "body": "Jules session: session_jules_ci",
        "_jules_session_id": "session_jules_ci",
        "labels": [],
        "user": {"login": "google-labs-jules[bot]"},
        "head": {"ref": "jules/feature", "sha": H1},
        "base": {"ref": "main", "sha": "base-1"},
    }
    github = MagicMock(token="controlled-token")
    requests = []

    def capture_dispatch(request, operation):
        requests.append(request)
        return operation(request)

    with (
        patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, ids=[701], observation=failed)),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=SimpleNamespace(failed_checks=[{"name": "PR Tests"}])),
        patch("auto_coder.pr_processor._is_codex_pr", return_value=False),
        patch("auto_coder.pr_processor._is_jules_pr", return_value=True),
        patch("auto_coder.pr_processor.cmd.run_command", return_value=CommandResult(True, "main\n", "", 0)),
        patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=SimpleNamespace(closed=False, actions=[])),
        patch("auto_coder.pr_processor._send_jules_error_feedback", return_value=["existing Jules helper sent feedback"]) as producer,
        patch("auto_coder.pr_processor.dispatch_pr_repair", side_effect=capture_dispatch),
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(github, REPOSITORY, pr_data, config, {})

    producer.assert_called_once()
    assert len(requests) == 1
    assert requests[0].origin is RepairOrigin.JULES
    assert requests[0].owner_identity == "session_jules_ci"
    assert requests[0].kind.value == "ci_failure"
    assert "existing Jules helper sent feedback" in actions


def test_terminal_ci_failure_enters_typed_synchronous_local_repair(tmp_path, monkeypatch):
    """REQ-001/006: the public handler preserves the local producer's typed result."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    failed = _snapshot(ObservationAvailability.KNOWN, CIConclusion.FAILURE)
    config = AutomationConfig()
    config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = True
    pr_data = {
        "number": PR_NUMBER,
        "body": "<!-- auto-coder:local-llm -->\nFixes #2501",
        "labels": [],
        "user": {"login": "local-owner"},
        "head": {"ref": "local/feature", "sha": H1},
        "base": {"ref": "main", "sha": "base-1"},
    }
    client = MagicMock(token="controlled-token")
    produced = PRActionList(("local candidate failed to synchronize",))
    produced.repair_attempt_started = True
    produced.resulting_head = H2
    produced.synchronized = False
    requests = []
    results = []

    def capture_dispatch(request, operation):
        requests.append(request)
        result = operation(request)
        results.append(result)
        return result

    status = ProcessedPRResult(pr_data=pr_data, actions_taken=[], priority="fix", analysis=None)
    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
        patch("auto_coder.util.github_action.observe_ci", return_value=failed),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=SimpleNamespace(failed_checks=[{"name": "PR Tests"}])),
        patch("auto_coder.pr_processor.cmd.run_command", return_value=CommandResult(True, "main\n", "", 0)),
        patch("auto_coder.pr_processor.current_ci_failure_authority", return_value=nullcontext(SimpleNamespace(allowed=True, reason="", failure_identities=("PR Tests",)))),
        patch("auto_coder.pr_processor._checkout_pr_branch", return_value=True),
        patch("auto_coder.pr_processor.BranchManager", return_value=nullcontext()),
        patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("failed logs", ["tests/test_pr.py"])),
        patch("auto_coder.pr_processor._fix_pr_issues_with_testing", return_value=produced) as local_repair,
        patch("auto_coder.pr_processor.dispatch_pr_repair", side_effect=capture_dispatch),
        patch("auto_coder.pr_processor._merge_pr") as merge,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {}, status)

    local_repair.assert_called_once()
    merge.assert_not_called()
    assert len(requests) == 1
    assert requests[0].origin is RepairOrigin.LOCAL
    assert requests[0].local_origin == "synchronous-local-ci-repair"
    assert results[0].disposition is RepairDisposition.FAILED
    assert results[0].resulting_head == H2
    assert results[0].synchronized is False
    assert status.outcome is PRProcessingOutcome.FAILED
    assert "local candidate failed to synchronize" in actions


@pytest.mark.parametrize("push_succeeds", [True, False], ids=["applied", "synchronization-failed"])
def test_public_local_ci_repair_uses_real_h1_to_h2_git_result(tmp_path, monkeypatch, push_succeeds, _use_real_commands):
    """REQ-007: a real new commit is not merge authority and failed publication is not APPLIED."""
    from auto_coder import pr_processor

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "local-feature"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=repository, check=True)
    tracked = repository / "app.txt"
    tracked.write_text("H1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "H1"], cwd=repository, check=True, capture_output=True)
    h1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    monkeypatch.chdir(repository)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))

    failed = _snapshot(ObservationAvailability.KNOWN, CIConclusion.FAILURE, head_sha=h1)
    config = AutomationConfig()
    config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = True
    pr_data = {
        "number": PR_NUMBER,
        "body": "<!-- auto-coder:local-llm -->\nFixes #2501",
        "labels": [],
        "user": {"login": "local-owner"},
        "head": {"ref": "local-feature", "sha": h1},
        "base": {"ref": "main", "sha": "base-1"},
    }
    client = MagicMock(token="controlled-token")
    results = []

    def real_git_repair(*_args, **_kwargs):
        tracked.write_text("H2\n", encoding="utf-8")
        submission = pr_processor._submit_ci_repair_candidate(REPOSITORY, PR_NUMBER)
        produced = PRActionList(submission.actions)
        produced.repair_attempt_started = submission.attempt_started
        produced.resulting_head = submission.resulting_head
        produced.synchronized = submission.synchronized
        return produced

    def capture_dispatch(request, operation):
        result = operation(request)
        results.append(result)
        return result

    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
        patch(
            "auto_coder.pr_processor._check_github_actions_status",
            return_value=GitHubActionsStatusResult(success=False, ids=[701], observation=failed),
        ),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=SimpleNamespace(failed_checks=[{"name": "PR Tests"}])),
        patch("auto_coder.pr_processor.current_ci_failure_authority", return_value=nullcontext(SimpleNamespace(allowed=True, reason="", failure_identities=("PR Tests",)))),
        patch("auto_coder.pr_processor._is_automatic_test_fix_enabled", return_value=True),
        patch("auto_coder.pr_processor._is_codex_pr", return_value=False),
        patch("auto_coder.pr_processor._is_jules_pr", return_value=False),
        patch("auto_coder.pr_processor._is_local_llm_pr", return_value=True),
        patch("auto_coder.pr_processor._checkout_pr_branch", return_value=True),
        patch("auto_coder.pr_processor.BranchManager", return_value=nullcontext()),
        patch("auto_coder.pr_processor._create_github_action_log_summary", return_value=("failed logs", ["tests/test_pr.py"])),
        patch("auto_coder.pr_processor._fix_pr_issues_with_testing", side_effect=real_git_repair),
        patch("auto_coder.pr_processor.dispatch_pr_repair", side_effect=capture_dispatch),
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=client),
        patch("auto_coder.util.github_action.is_item_closed_on_github", return_value=False),
        patch("auto_coder.pr_processor.git_push", return_value=CommandResult(push_succeeds, "", "push rejected" if not push_succeeds else "", 0 if push_succeeds else 1)),
        patch("auto_coder.pr_processor._merge_pr") as merge,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})

    h2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    assert results, actions
    assert h2 != h1
    assert len(results) == 1
    assert results[0].resulting_head == h2
    assert results[0].disposition is (RepairDisposition.APPLIED if push_succeeds else RepairDisposition.FAILED)
    assert results[0].synchronized is push_succeeds
    merge.assert_not_called()


def test_durable_cloud_conflict_receipt_governs_replay_without_local_substitute(tmp_path):
    """REQ-003: real remediation retains cloud ownership across replay."""

    class ControlledCloudClient:
        def __init__(self):
            self.messages = []
            self.backend_name = "claude-routine"

        def send_followup(self, task_id, message):
            self.messages.append((task_id, message))
            return True

    cloud = ControlledCloudClient()
    state_path = tmp_path / "conflict-repairs.json"
    pr_data = {
        "number": PR_NUMBER,
        "body": "Closes #2501",
        "head": {"ref": "cloud/issue-2501", "sha": H1},
        "base": {"ref": "main", "sha": "base-1"},
    }
    github = MagicMock()
    github.get_pr_comments.return_value = []
    github.token = "controlled-token"
    api = MagicMock()
    api.pulls.get.return_value = pr_data
    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True

    def git_effect(command, **_kwargs):
        if command[:2] == ["git", "rev-list"]:
            return CommandResult(True, "1\n", "", 0)
        if command[:2] == ["git", "merge"] and "--abort" not in command:
            return CommandResult(False, "", "controlled conflict", 1)
        return CommandResult(True, "", "", 0)

    requests = []
    results = []

    def capture_dispatch(request, operation):
        requests.append(request)
        result = operation(request)
        results.append(result)
        return result

    with (
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": False, "merge_state_status": "dirty"}),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=github),
        patch("auto_coder.pr_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.pr_processor._checkout_pr_branch", return_value=True),
        patch("auto_coder.pr_processor.BranchManager"),
        patch("auto_coder.pr_processor.cmd.run_command", side_effect=git_effect),
        patch("auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(cloud, "task-existing")),
        patch("auto_coder.pr_processor.dispatch_pr_repair", side_effect=capture_dispatch),
        patch("auto_coder.pr_processor._cloud_conflict_state_path", return_value=state_path),
        patch("auto_coder.conflict_resolver._perform_base_branch_merge_and_conflict_resolution") as local_repair,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        first = _handle_pr_merge(github, REPOSITORY, pr_data, config, {})
        replay = _handle_pr_merge(github, REPOSITORY, pr_data, config, {})

    assert any("accepted merge-conflict-repair" in action for action in first)
    assert any("accepted merge-conflict-repair" in action for action in replay)
    assert len(cloud.messages) == 1
    assert [request.origin for request in requests] == [RepairOrigin.CLAUDE_ROUTINE, RepairOrigin.CLAUDE_ROUTINE]
    assert [request.owner_identity for request in requests] == ["task-existing", "task-existing"]
    assert [result.disposition for result in results] == [RepairDisposition.ACCEPTED_OR_UNCERTAIN] * 2
    assert [result.attempt_started for result in results] == [True, False]
    assert [result.receipt_state for result in results] == [RepairFactState.UNKNOWN, RepairFactState.UNKNOWN]
    local_repair.assert_not_called()
    assert state_path.exists()


def test_durable_review_repair_receipt_replays_through_pr_entry_without_local_effect(tmp_path, monkeypatch):
    """REQ-003: supported replay retains its cloud owner and receipt."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    state_path = tmp_path / "review-repairs.json"
    green = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS)
    thread = ReviewThread(
        id="PRRT_repair",
        is_resolved=False,
        comments=[ReviewThreadComment(database_id=201, body="Repair this finding", author_login="reviewer")],
    )
    state = ClaimedReviewThreadGateState(
        unresolved=(thread,),
        blocking_unresolved=(thread,),
        has_blocking_unresolved=True,
    )
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = False
    pr_data = {
        "number": PR_NUMBER,
        "body": "Fixes #99\nhttps://chatgpt.com/codex/tasks/task-contract",
        "labels": [],
        "head": {"ref": "cloud/feature", "sha": H1},
        "base": {"ref": "main", "sha": "base-1"},
    }
    client = MagicMock(token="controlled-token")
    client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(head_ref="cloud/feature", head_sha=H1, base_ref="main")
    provider = MagicMock()
    provider.send_followup.return_value = True

    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
        patch("auto_coder.util.github_action.observe_ci", return_value=green),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor.retry_pending_stale_review_thread_rollbacks", return_value=()),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=True),
        patch("auto_coder.pr_processor._is_pr_adversarial_validation_enabled", return_value=False),
        patch("auto_coder.pr_processor._get_claimed_review_thread_state", return_value=state),
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="codex-cloud", task_id="task-contract")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=provider),
        patch("auto_coder.pr_processor._revalidate_local_review_repair_route") as local_effect,
        patch("auto_coder.pr_processor._merge_pr") as merge,
        patch("auto_coder.pr_processor.get_detailed_checks_from_history") as failure_consumer,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        first = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})
        replay = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})

    assert any("All GitHub Actions checks passed" in action for action in first)
    assert any("All GitHub Actions checks passed" in action for action in replay)
    provider.send_followup.assert_called_once()
    assert state_path.exists()
    local_effect.assert_not_called()
    merge.assert_not_called()
    failure_consumer.assert_not_called()
