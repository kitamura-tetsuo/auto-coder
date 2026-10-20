"""Synchronous local execution of an admitted pull-request CI repair."""

from __future__ import annotations

import math
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, Sequence

from loguru import logger

from .automation_config import AutomationConfig
from .ci_repair_authority import CIRepairAuthority
from .ci_repair_verification import FocusedVerification, dedupe_targets, follow_up_budget_exhausted
from .exceptions import AutoCoderRetryableBackendError
from .pr_processing_evidence import record_editing_invocation
from .pr_repair_dispatch import PRRepairResult, RepairDisposition, RepairFactState


class ActionResult(Protocol):
    """Narrow view of the existing action/result carrier used by base update."""

    repair_result: PRRepairResult | None
    quota_deferred: bool
    retry_not_before: float | None

    def __iter__(self) -> Iterator[str]: ...


class SubmissionResult(Protocol):
    @property
    def actions(self) -> tuple[str, ...]: ...

    @property
    def resulting_head(self) -> str: ...

    @property
    def synchronized(self) -> bool | None: ...


@dataclass(frozen=True)
class CorrectionPorts:
    automatic_fix_enabled: Callable[[], bool]
    current_backend: Callable[[], object | None]
    high_score_backend: Callable[[], object | None]
    initial_correction: Callable[[str, object | None, list[str], Callable[[], None]], Sequence[str]]
    verify: Callable[[list[str]], FocusedVerification]
    follow_up: Callable[[dict[str, object], list[dict[str, object]], object | None, Callable[[], None]], tuple[Sequence[str], str]]
    new_work_allowed: Callable[[], bool]
    pr_closed: Callable[[], bool]
    submit: Callable[[], SubmissionResult]
    defer_verification: Callable[[], bool]
    trace_initial: Callable[[], None]


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
    repair_result: PRRepairResult | None = None


@dataclass(frozen=True)
class LocalCIRepairPorts:
    authority: Callable[[object, str, int, str], AbstractContextManager[CIRepairAuthority]]
    prepare_branch: Callable[[], bool]
    branch_context: Callable[[str], AbstractContextManager[object]]
    automatic_fix_enabled: Callable[[], bool]
    update_base: Callable[[], ActionResult]
    summarize_checks: Callable[[Sequence[object]], tuple[str, list[str] | None]]
    read_logs: Callable[[Sequence[object]], str]
    correction: CorrectionPorts
    extract_failed_tests: Callable[[str], list[str]]
    trace_base_update: Callable[[str], None]


def _record_base_update_trace(ports: LocalCIRepairPorts, result: str) -> None:
    """Record a diagnostic without allowing observability to change execution."""
    try:
        ports.trace_base_update(result)
    except Exception as exc:
        logger.warning("Failed to record base-update diagnostic '{}': {}", result, exc)


def _base_update_diagnostic(pr_number: int, result: PRRepairResult) -> tuple[str, str]:
    """Render the operator action and trace result from typed producer facts."""
    head = result.resulting_head if result.resulting_head_state is RepairFactState.AVAILABLE and result.resulting_head else "unknown"
    synchronized = result.synchronized if result.synchronization_state is RepairFactState.AVAILABLE else None

    if result.disposition is RepairDisposition.NO_UPDATE_NEEDED:
        return "up_to_date", f"PR #{pr_number} is up to date with main branch; test failures are due to PR content"
    if result.disposition is RepairDisposition.APPLIED:
        if synchronized is True and head != "unknown":
            return "pushed", f"Updated PR #{pr_number} with base branch at {head}; awaiting GitHub Actions"
        synchronization = "unknown" if synchronized is None else "not synchronized"
        return "applied_unconfirmed", f"Base update for PR #{pr_number} was applied but publication is {synchronization}; resulting head: {head}"
    if result.disposition is RepairDisposition.ACCEPTED_OR_UNCERTAIN:
        receipt = result.receipt if result.receipt_state is RepairFactState.AVAILABLE and result.receipt else "unknown"
        return "delegated", f"Base update for PR #{pr_number} was delegated and is pending evidence; receipt: {receipt}; resulting head: {head}"
    if result.disposition is RepairDisposition.DEFERRED:
        return "deferred", f"Base update for PR #{pr_number} is waiting for a later retry; resulting head: {head}"
    if result.disposition is RepairDisposition.REFUSED:
        return "refused", f"Base update for PR #{pr_number} was refused; resulting head: {head}"
    if result.disposition is RepairDisposition.QUALITY_REFUSED:
        return "quality_refused", f"Base update for PR #{pr_number} was refused by the quality gate; resulting head: {head}"
    return "failed", f"Base update for PR #{pr_number} failed; resulting head: {head}"


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
                _record_base_update_trace(ports, "skipped")
                if not request.failed_checks:
                    actions.append(f"No specific failed checks found for PR #{request.pr_number}")
                    return LocalCIRepairResult(tuple(actions))
                diagnostics, targets = ports.summarize_checks(request.failed_checks)
                correction = execute_correction(request, diagnostics, targets, ports.correction, ports.extract_failed_tests)
                actions.extend(correction.actions)
                return _merge_correction_result(actions, correction)

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

            trace_result, diagnostic = _base_update_diagnostic(request.pr_number, update_result)
            actions.append(diagnostic)
            _record_base_update_trace(ports, trace_result)

            if update_result.disposition is RepairDisposition.QUALITY_REFUSED:
                return LocalCIRepairResult(tuple(actions), close_for_quality=True, repair_result=update_result)

            if update_result.disposition is not RepairDisposition.NO_UPDATE_NEEDED:
                return LocalCIRepairResult(
                    tuple(actions),
                    attempt_started=update_result.attempt_started,
                    candidate_head=update_result.resulting_head,
                    synchronized=(update_result.synchronized if update_result.synchronization_state is RepairFactState.AVAILABLE else None),
                    quota_deferred=update.quota_deferred,
                    retry_not_before=update.retry_not_before if update.retry_not_before is not None else update_result.retry_not_before,
                    failure_reason=(update_result.reason or "base branch update failed") if update_result.disposition is RepairDisposition.FAILED else "",
                    repair_result=update_result,
                )

            if not ports.automatic_fix_enabled():
                actions.append(f"Automatic test fix is disabled for PR #{request.pr_number}; skipping test-failure repair")
                return LocalCIRepairResult(tuple(actions))
            if not request.failed_checks:
                actions.append(f"No specific failed checks found for PR #{request.pr_number}")
                return LocalCIRepairResult(tuple(actions))
            correction = execute_correction(request, ports.read_logs(request.failed_checks), None, ports.correction, ports.extract_failed_tests)
            actions.extend(correction.actions)
            return _merge_correction_result(actions, correction)


def _merge_correction_result(actions: list[str], correction: LocalCIRepairResult) -> LocalCIRepairResult:
    return LocalCIRepairResult(
        tuple(actions),
        attempt_started=correction.attempt_started,
        candidate_head=correction.candidate_head,
        synchronized=correction.synchronized,
        failure_reason=correction.failure_reason,
    )


def execute_correction(
    request: LocalCIRepairRequest,
    diagnostics: str,
    failed_tests: list[str] | None,
    ports: CorrectionPorts,
    extract_failed_tests: Callable[[str], list[str]],
) -> LocalCIRepairResult:
    """Compose correction, focused sweeps, follow-ups and publication."""
    actions: list[str] = []
    known_failures = dedupe_targets(failed_tests if failed_tests is not None else extract_failed_tests(diagnostics))
    attempt_started = False
    corrected = False

    def mark_started() -> None:
        nonlocal attempt_started, corrected
        attempt_started = True
        corrected = True

    enabled = ports.automatic_fix_enabled()
    current_backend = ports.current_backend() if enabled else None
    high_score_backend = ports.high_score_backend() if enabled else None
    history: list[dict[str, object]] = []
    try:
        if enabled:
            ports.trace_initial()
            actions.append(f"Starting PR issue fixing for PR #{request.pr_number} using GitHub Actions logs")
            actions.extend(ports.initial_correction(diagnostics, high_score_backend, known_failures, mark_started))
            if attempt_started:
                record_editing_invocation(high_score_backend, "local-ci-repair", request.repository)
        else:
            actions.append(f"Automatic test fix is disabled for PR #{request.pr_number}; skipping GitHub Actions fix")

        verification = FocusedVerification()
        if ports.defer_verification():
            actions.append("Local verification deferred by operator until after merge; GitHub CI remains authoritative")
        elif not known_failures:
            actions.append("Local verification not performed: CI diagnostics identified no test file to select; full validation is left to CI")
        else:
            follow_ups = 0
            while True:
                verification = ports.verify(known_failures)
                failing = verification.failed
                if not failing:
                    break
                if not ports.automatic_fix_enabled():
                    actions.append(f"Automatic test fix is disabled for PR #{request.pr_number}; skipping local test repair")
                    break
                if follow_up_budget_exhausted(request.config.MAX_FIX_ATTEMPTS, follow_ups):
                    actions.append(f"Max fix attempts ({request.config.MAX_FIX_ATTEMPTS}) reached for PR #{request.pr_number}")
                    break
                if not ports.new_work_allowed():
                    actions.append(f"Deferred another repair attempt for PR #{request.pr_number}: graceful shutdown is draining")
                    break
                if ports.pr_closed():
                    actions.append(f"PR #{request.pr_number} is closed on GitHub. Aborting fix loop.")
                    return LocalCIRepairResult(tuple(actions), attempt_started=attempt_started)
                follow_ups += 1
                if follow_ups >= 2 and high_score_backend and current_backend != high_score_backend:
                    current_backend = high_score_backend
                    actions.append(f"Switched to fallback backend for PR #{request.pr_number}")
                follow_actions, response = ports.follow_up(failing[0].raw, history, current_backend, mark_started)
                if attempt_started:
                    record_editing_invocation(current_backend, "local-ci-repair-follow-up", request.repository)
                actions.extend(follow_actions)
                if not response and not math.isfinite(float(request.config.MAX_FIX_ATTEMPTS)):
                    actions.append(f"No local correction was produced for {failing[0].target}; stopping local repair")
                    break
                history.append({"attempt_number": follow_ups, "llm_output": response, "test_result": failing[0].raw})
            actions.extend(verification.describe())
            if verification.all_passed:
                actions.append(f"All {len(verification.results)} known CI-failed test file(s) passed locally on the latest corrected state (focused scope only; full validation pending CI)")
            else:
                actions.append("Known CI-failed tests are not all verified locally; full validation pending CI")

        if not ports.automatic_fix_enabled() or not corrected:
            return LocalCIRepairResult(tuple(actions), attempt_started=attempt_started)
        submission = ports.submit()
        actions.extend(submission.actions)
        return LocalCIRepairResult(
            tuple(actions),
            attempt_started=attempt_started,
            candidate_head=submission.resulting_head,
            synchronized=submission.synchronized,
            failure_reason="local CI repair was not synchronized" if submission.synchronized is False else "",
        )
    except AutoCoderRetryableBackendError:
        raise
    except Exception as exc:
        actions.append(f"Error fixing PR issues with testing for PR #{request.pr_number}: {exc}")
        return LocalCIRepairResult(tuple(actions), attempt_started=attempt_started)
