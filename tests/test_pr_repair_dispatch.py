from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.pr_repair_contract

from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_repair_dispatch import (
    PRRepairRequest,
    PRRepairResult,
    RepairDisposition,
    RepairFactState,
    RepairKind,
    RepairOrigin,
    dispatch_pr_repair,
)


def request(origin: RepairOrigin = RepairOrigin.LOCAL) -> PRRepairRequest:
    return PRRepairRequest(
        repository="owner/repo",
        pr_number=2504,
        head_sha="H1",
        kind=RepairKind.CI_FAILURE,
        origin=origin,
        owner_identity="task_e_owner" if origin is not RepairOrigin.LOCAL else "",
        local_origin="checked-out-pr" if origin is RepairOrigin.LOCAL else "",
    )


@pytest.mark.parametrize(
    "cloud_result",
    [
        pytest.param({"provider": "codex-cloud", "task_id": "task-1", "receipt_identity": "pending"}, id="indeterminate-receipt"),
        pytest.param({"provider": "claude-routine", "deferred": True, "retry_not_before": 4321.0}, id="quota-hold"),
    ],
)
def test_definitive_merge_rejection_preserves_cloud_wait_without_local_fallback(cloud_result):
    """REQ-004: uncertain cloud ownership stops direct conflict fallback."""
    from auto_coder import pr_processor

    api = MagicMock()
    api.pulls.get.return_value = {"number": 2504, "mergeable": False, "head": {"sha": "H1"}}
    route = pr_processor.MergeRouteDisposition()
    result = pr_processor.CloudConflictDelegationResult(reason="wait for provider", **cloud_result)
    with (
        patch.object(pr_processor, "_is_jules_pr", return_value=False),
        patch.object(pr_processor, "_is_dependabot_pr", return_value=False),
        patch.object(pr_processor, "_delegate_cloud_merge_conflict_repair_result", return_value=result) as delegate,
        patch.object(pr_processor, "_resolve_pr_merge_conflicts") as local,
        patch.object(pr_processor, "_trigger_fallback_for_pr_failure") as fallback,
    ):
        assert pr_processor._handle_definitive_merge_rejection("owner/repo", 2504, AutomationConfig(), MagicMock(), api, "owner", "repo", MagicMock(), MagicMock(), MagicMock(), "H1", route) is False

    delegate.assert_called_once()
    local.assert_not_called()
    fallback.assert_not_called()
    assert route.outcome is pr_processor.PRProcessingOutcome.DEFERRED
    assert route.retry_not_before == cloud_result.get("retry_not_before")


def test_adapter_preserves_structured_effect_evidence() -> None:
    expected = PRRepairResult(
        RepairDisposition.APPLIED,
        ("pushed H2",),
        attempt_started=True,
        resulting_head_state=RepairFactState.AVAILABLE,
        resulting_head="H2",
        synchronization_state=RepairFactState.AVAILABLE,
        synchronized=True,
    )
    operation = MagicMock(return_value=expected)

    assert dispatch_pr_repair(request(), operation) == expected
    operation.assert_called_once_with(request())


@pytest.mark.parametrize(
    ("origin", "owner", "local"),
    [
        (RepairOrigin.CODEX_CLOUD, "", ""),
        (RepairOrigin.CLAUDE_ROUTINE, "", ""),
        (RepairOrigin.JULES, "", ""),
        (RepairOrigin.LOCAL, "", ""),
    ],
)
def test_request_refuses_missing_existing_origin_identity(origin: RepairOrigin, owner: str, local: str) -> None:
    with pytest.raises(ValueError):
        PRRepairRequest("owner/repo", 1, "H1", RepairKind.MERGE_CONFLICT, origin, owner, local)


def test_codex_ci_adapter_does_not_turn_refusal_into_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder import pr_processor

    producer = MagicMock(return_value=pr_processor.CodexCloudFeedbackResult(False, False, ("allowance exhausted",)))
    monkeypatch.setattr(pr_processor, "_send_codex_cloud_error_feedback", producer)
    pr_data = {"number": 12, "head": {"sha": "H1"}}

    result = pr_processor._dispatch_codex_ci_failure_repair("owner/repo", pr_data, [{"name": "tests"}], AutomationConfig(), MagicMock(), force=False)

    assert result.disposition is RepairDisposition.REFUSED
    assert result.receipt_state is RepairFactState.UNKNOWN
    assert not result.attempt_started
    producer.assert_called_once()


def test_codex_ci_adapter_preserves_authority_deferral_and_unknown_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder import pr_processor

    origin = pr_processor.CloudTaskOrigin("codex-cloud", "task_e_owner", MagicMock())
    monkeypatch.setattr(
        pr_processor,
        "_resolve_cloud_task_origin",
        MagicMock(return_value=pr_processor.CloudTaskOriginResolution(origin)),
    )
    producer = MagicMock(
        side_effect=[
            pr_processor.CodexCloudFeedbackResult(False, True, ("head changed",), deferred=True),
            pr_processor.CodexCloudFeedbackResult(True, False, ("already delivered",)),
        ]
    )
    monkeypatch.setattr(pr_processor, "_send_codex_cloud_error_feedback", producer)
    pr_data = {"number": 12, "head": {"sha": "H1"}}

    deferred = pr_processor._dispatch_codex_ci_failure_repair("owner/repo", pr_data, [{"name": "tests"}], AutomationConfig(), MagicMock(), force=False)
    replay = pr_processor._dispatch_codex_ci_failure_repair("owner/repo", pr_data, [{"name": "tests"}], AutomationConfig(), MagicMock(), force=False)

    assert deferred.disposition is RepairDisposition.DEFERRED
    assert deferred.attempt_started is None
    assert replay.disposition is RepairDisposition.ACCEPTED_OR_UNCERTAIN
    assert replay.attempt_started is None


def test_cloud_conflict_adapter_preserves_deferred_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder import pr_processor

    producer = MagicMock(
        return_value=pr_processor.CloudConflictDelegationResult(
            reason="owner quota wait",
            deferred=True,
            retry_not_before=1234.0,
        )
    )
    monkeypatch.setattr(pr_processor, "_delegate_cloud_merge_conflict_repair_result", producer)
    owner = MagicMock(backend_name="claude-routine")
    monkeypatch.setattr(pr_processor, "_resolve_cloud_conflict_origin", MagicMock(return_value=(owner, "session-12")))

    result = pr_processor._dispatch_cloud_conflict_repair("owner/repo", {"number": 12, "head": {"sha": "H1"}}, MagicMock())

    assert result.disposition is RepairDisposition.DEFERRED
    assert result.retry_not_before == 1234.0
    assert result.receipt_state is RepairFactState.UNKNOWN
    producer.assert_called_once()
