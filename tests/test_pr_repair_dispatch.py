from unittest.mock import MagicMock

import pytest

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

    result = pr_processor._dispatch_cloud_conflict_repair("owner/repo", {"number": 12, "head": {"sha": "H1"}}, MagicMock())

    assert result.disposition is RepairDisposition.DEFERRED
    assert result.retry_not_before == 1234.0
    assert result.receipt_state is RepairFactState.UNKNOWN
    producer.assert_called_once()
