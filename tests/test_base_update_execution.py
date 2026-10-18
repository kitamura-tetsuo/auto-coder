from auto_coder.base_update_execution import (
    BaseUpdateConflictExecutionService,
    BaseUpdateExecutionRequest,
    BaseUpdateExecutionResult,
)
from auto_coder.pr_repair_dispatch import PRRepairResult, RepairDisposition


def request() -> BaseUpdateExecutionRequest:
    return BaseUpdateExecutionRequest("owner/repo", 2542, "head-1", "release", "local")


def test_base_update_service_carries_target_and_typed_result() -> None:
    service = BaseUpdateConflictExecutionService()

    result = service.execute_base_update(
        request(),
        lambda: BaseUpdateExecutionResult(
            PRRepairResult(RepairDisposition.NO_UPDATE_NEEDED, resulting_head="head-1"),
            ("already current",),
        ),
    )

    assert result.actions == ("already current",)
    assert result.repair.disposition is RepairDisposition.NO_UPDATE_NEEDED
    assert (result.repair.repository, result.repair.pr_number) == ("owner/repo", 2542)
    assert (result.repair.input_head, result.repair.base_ref) == ("head-1", "release")


def test_execution_service_rejects_a_retargeted_operation() -> None:
    service = BaseUpdateConflictExecutionService()

    result = service.execute_conflict_repair(
        request(),
        lambda: PRRepairResult(
            RepairDisposition.APPLIED,
            repository="other/repo",
            pr_number=2542,
            input_head="head-1",
            base_ref="release",
        ),
    )

    assert result.disposition is RepairDisposition.FAILED
    assert result.reason == "base-update producer returned a mismatched repository identity"
    assert result.repository == "owner/repo"


def test_execution_service_does_not_fallback_after_a_refusal() -> None:
    calls = 0

    def refused() -> PRRepairResult:
        nonlocal calls
        calls += 1
        return PRRepairResult(RepairDisposition.REFUSED, reason="origin does not support repair")

    result = BaseUpdateConflictExecutionService().execute_conflict_repair(request(), refused)

    assert calls == 1
    assert result.disposition is RepairDisposition.REFUSED
    assert result.reason == "origin does not support repair"


def test_conflict_service_preserves_producer_result_when_base_is_not_preobserved() -> None:
    request_without_base = BaseUpdateExecutionRequest("owner/repo", 2542, "head-1", "", "cloud")

    result = BaseUpdateConflictExecutionService().execute_conflict_repair(
        request_without_base,
        lambda: PRRepairResult(
            RepairDisposition.DEFERRED,
            reason="owner quota wait",
            retry_not_before=1234.0,
        ),
    )

    assert result.disposition is RepairDisposition.DEFERRED
    assert result.retry_not_before == 1234.0
    assert result.base_ref == ""
