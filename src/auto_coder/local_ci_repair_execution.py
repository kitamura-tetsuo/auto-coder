"""Synchronous local execution of an admitted pull-request CI repair."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, Sequence

from .automation_config import AutomationConfig
from .ci_repair_authority import CIRepairAuthority
from .pr_repair_dispatch import PRRepairResult, RepairDisposition


class ActionResult(Protocol):
    """Narrow view of the existing action/result carrier used by base update."""

    repair_result: PRRepairResult | None
    quota_deferred: bool
    retry_not_before: float | None

    def __iter__(self) -> Iterator[str]: ...


@dataclass(frozen=True)
class LocalCIRepairRequest:
    repository: str
    pr_number: int
    expected_head: str
    branch_name: str
    already_checked_out: bool
    failed_checks: tuple[object, ...]
    config: AutomationConfig
    github_client: object


@dataclass(frozen=True)
class LocalCIRepairResult:
    """Facts from the bounded execution, without interpreting action prose."""

    actions: tuple[str, ...] = ()
    attempt_started: bool | None = None
    candidate_head: str = ""
    synchronized: bool | None = None
    quota_deferred: bool = False
    retry_not_before: float | None = None
    failure_reason: str = ""
    close_for_quality: bool = False


@dataclass(frozen=True)
class LocalCIRepairPorts:
    authority: Callable[[object, str, int, str], AbstractContextManager[CIRepairAuthority]]
    prepare_branch: Callable[[], bool]
    branch_context: Callable[[str], AbstractContextManager[object]]
    automatic_fix_enabled: Callable[[], bool]
    update_base: Callable[[], ActionResult]
    summarize_checks: Callable[[Sequence[object]], tuple[str, list[str] | None]]
    read_logs: Callable[[Sequence[object]], str]
    repair: Callable[[str, list[str] | None], PRRepairResult]
    trace_base_update: Callable[[str], None]


def execute_local_ci_repair(request: LocalCIRepairRequest, ports: LocalCIRepairPorts) -> LocalCIRepairResult:
    """Run branch preparation, optional base update, repair and publication.

    The final exact-head observation owns the lifetime of every first mutation,
    including checkout preparation.  The service deliberately consumes typed
    base-update and repair facts; display text never authorizes the next effect.
    """
    actions: list[str] = []

    if not ports.automatic_fix_enabled() and not request.already_checked_out:
        return LocalCIRepairResult((f"Automatic test fix is disabled for PR #{request.pr_number}; skipping test-failure repair",))

    with ports.authority(
        request.github_client,
        request.repository,
        request.pr_number,
        request.expected_head,
    ) as authority:
        if not authority.allowed:
            return LocalCIRepairResult((f"Deferred local CI repair for PR #{request.pr_number}: {authority.reason}",))

        prepared = request.already_checked_out or ports.prepare_branch()
        if not prepared:
            return LocalCIRepairResult((f"Failed to prepare PR #{request.pr_number} branch",))

        with ports.branch_context(request.branch_name):
            actions.append(f"Checked out PR #{request.pr_number} branch")

            if request.config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL:
                actions.append(f"[Policy] Skipping base branch update for PR #{request.pr_number} " "(config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=True)")
                ports.trace_base_update("skipped")
                if not request.failed_checks:
                    actions.append(f"No specific failed checks found for PR #{request.pr_number}")
                    return LocalCIRepairResult(tuple(actions))
                diagnostics, targets = ports.summarize_checks(request.failed_checks)
                repair = ports.repair(diagnostics, targets)
                actions.extend(repair.actions)
                return _result_from_repair(actions, repair)

            actions.append(f"[Policy] Performing base branch update for PR #{request.pr_number} before fixes " "(config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=False)")
            update = ports.update_base()
            actions.extend(update)
            update_result = update.repair_result
            if update_result is None:
                return LocalCIRepairResult(
                    tuple(actions),
                    quota_deferred=update.quota_deferred,
                    retry_not_before=update.retry_not_before,
                    failure_reason="base update returned no typed control result",
                )

            if update_result.disposition in {
                RepairDisposition.NO_UPDATE_NEEDED,
                RepairDisposition.FAILED,
            }:
                actions = [action for action in actions if action != "ACTION_FLAG:SKIP_ANALYSIS"]

            if update_result.disposition is RepairDisposition.QUALITY_REFUSED:
                return LocalCIRepairResult(tuple(actions), close_for_quality=True)

            if update_result.disposition is not RepairDisposition.NO_UPDATE_NEEDED:
                if update_result.disposition in {
                    RepairDisposition.APPLIED,
                    RepairDisposition.ACCEPTED_OR_UNCERTAIN,
                    RepairDisposition.DEFERRED,
                    RepairDisposition.REFUSED,
                }:
                    actions.append(f"Updated PR #{request.pr_number} with base branch, skipping to next PR for GitHub Actions check")
                    ports.trace_base_update("pushed")
                    return LocalCIRepairResult(
                        tuple(actions),
                        quota_deferred=update.quota_deferred,
                        retry_not_before=update.retry_not_before,
                    )
                reason = update_result.reason or "base branch update failed"
                actions.append(f"Base branch comparison or update failed for PR #{request.pr_number}")
                return LocalCIRepairResult(
                    tuple(actions),
                    quota_deferred=update.quota_deferred,
                    retry_not_before=update.retry_not_before,
                    failure_reason=reason,
                )

            actions.append(f"PR #{request.pr_number} is up to date with main branch, test failures are due to PR content")
            ports.trace_base_update("up_to_date")
            if not ports.automatic_fix_enabled():
                actions.append(f"Automatic test fix is disabled for PR #{request.pr_number}; skipping test-failure repair")
                return LocalCIRepairResult(tuple(actions))
            if not request.failed_checks:
                actions.append(f"No specific failed checks found for PR #{request.pr_number}")
                return LocalCIRepairResult(tuple(actions))
            repair = ports.repair(ports.read_logs(request.failed_checks), None)
            actions.extend(repair.actions)
            return _result_from_repair(actions, repair)


def _result_from_repair(actions: list[str], repair: PRRepairResult) -> LocalCIRepairResult:
    return LocalCIRepairResult(
        tuple(actions),
        attempt_started=repair.attempt_started,
        candidate_head=repair.resulting_head,
        synchronized=(repair.synchronized if repair.synchronization_state.value == "available" else None),
        failure_reason=repair.reason if repair.disposition is RepairDisposition.FAILED else "",
    )
