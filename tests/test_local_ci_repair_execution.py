from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import Iterator

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.ci_repair_authority import CIRepairAuthority
from auto_coder.local_ci_repair_execution import (
    CorrectionPorts,
    LocalCIRepairPorts,
    LocalCIRepairRequest,
    execute_local_ci_repair,
)
from auto_coder.pr_repair_dispatch import PRRepairResult, RepairDisposition, RepairFactState


@dataclass
class UpdateActions:
    repair_result: PRRepairResult | None
    quota_deferred: bool = False
    retry_not_before: float | None = None
    actions: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[str]:
        return iter(self.actions)


def _request(config: AutomationConfig, *, checked_out: bool) -> LocalCIRepairRequest:
    return LocalCIRepairRequest(
        repository="owner/repo",
        pr_number=7,
        expected_head="head-1",
        branch_name="feature/fix",
        already_checked_out=checked_out,
        failed_checks=({"name": "tests"},),
        config=config,
        github_client=object(),
    )


def _ports(events: list[str], *, allowed: bool = True, enabled: bool = True) -> LocalCIRepairPorts:
    @contextmanager
    def authority(_client: object, _repo: str, _number: int, _head: str):  # type: ignore[no-untyped-def]
        events.append("authority")
        yield CIRepairAuthority(allowed, "current" if allowed else "invalidated", "head-1")

    return LocalCIRepairPorts(
        authority=authority,
        prepare_branch=lambda: events.append("prepare") is None or True,
        branch_context=lambda _branch: nullcontext(events.append("branch")),
        automatic_fix_enabled=lambda: enabled,
        update_base=lambda: UpdateActions(PRRepairResult(RepairDisposition.NO_UPDATE_NEEDED)),
        summarize_checks=lambda _checks: ("lint failed", []),
        read_logs=lambda _checks: "lint failed",
        correction=CorrectionPorts(
            automatic_fix_enabled=lambda: enabled,
            current_backend=lambda: object(),
            high_score_backend=lambda: object(),
            initial_correction=lambda _logs, _manager, _targets, started: (started(), events.append("repair"), ("corrected",))[2],
            verify=lambda _targets: __import__("auto_coder.ci_repair_verification", fromlist=["FocusedVerification"]).FocusedVerification(),
            follow_up=lambda *_args: ((), ""),
            new_work_allowed=lambda: True,
            pr_closed=lambda: False,
            submit=lambda: type("Submission", (), {"actions": ("candidate published",), "resulting_head": "head-2", "synchronized": True})(),
            defer_verification=lambda: False,
            trace_initial=lambda: None,
        ),
        extract_failed_tests=lambda _logs: [],
        trace_base_update=lambda _result: None,
    )


def test_invalidated_authority_stops_before_first_mutation() -> None:
    """REQ-002/006: the observable final checkpoint precedes preparation."""
    config = AutomationConfig()
    events: list[str] = []

    result = execute_local_ci_repair(_request(config, checked_out=False), _ports(events, allowed=False))

    assert events == ["authority"]
    assert result.actions == ("Deferred local CI repair for PR #7: invalidated",)
    assert result.attempt_started is None
    assert result.synchronized is None


@pytest.mark.parametrize("checked_out", [False, True])
def test_both_checkout_origins_run_inside_fresh_authority(checked_out: bool) -> None:
    """REQ-001/006: both production origins share one bounded composition."""
    config = AutomationConfig()
    events: list[str] = []

    result = execute_local_ci_repair(_request(config, checked_out=checked_out), _ports(events))

    assert events == (["authority", "branch", "repair"] if checked_out else ["authority", "prepare", "branch", "repair"])
    assert result.attempt_started is True
    assert result.candidate_head == "head-2"
    assert result.synchronized is True


def test_disabled_fix_does_not_observe_or_mutate_checkout_required_route() -> None:
    """REQ-003/005: the kill switch starts neither checkout nor backend work."""
    config = AutomationConfig()
    config.automatic_test_fix = False
    events: list[str] = []

    result = execute_local_ci_repair(_request(config, checked_out=False), _ports(events, enabled=False))

    assert events == []
    assert "disabled" in result.actions[0]
    assert result.attempt_started is None


def test_failed_publication_keeps_candidate_and_synchronization_facts_distinct() -> None:
    """REQ-005/006: a commit without a push is not a published repair."""
    config = AutomationConfig()
    events: list[str] = []
    ports = _ports(events)
    ports = replace(
        ports,
        correction=replace(
            ports.correction,
            submit=lambda: type("Submission", (), {"actions": ("commit created", "push failed"), "resulting_head": "head-2", "synchronized": False})(),
        ),
    )

    result = execute_local_ci_repair(_request(config, checked_out=True), ports)

    assert result.attempt_started is True
    assert result.candidate_head == "head-2"
    assert result.synchronized is False
    assert result.failure_reason == "local CI repair was not synchronized"


@pytest.mark.parametrize(
    "update_result",
    [
        PRRepairResult(
            RepairDisposition.APPLIED,
            resulting_head_state=RepairFactState.AVAILABLE,
            resulting_head="head-2",
            synchronization_state=RepairFactState.AVAILABLE,
            synchronized=True,
        ),
        PRRepairResult(
            RepairDisposition.ACCEPTED_OR_UNCERTAIN,
            reason="Jules owns the conflict repair",
            attempt_started=True,
            receipt_state=RepairFactState.AVAILABLE,
            receipt="jules-session-7",
        ),
    ],
)
def test_optional_base_update_preserves_publication_or_owner_evidence(update_result: PRRepairResult) -> None:
    """REQ-002: terminal base work retains its typed executor evidence."""
    config = AutomationConfig()
    config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = False
    events: list[str] = []
    ports = replace(_ports(events), update_base=lambda: UpdateActions(update_result))

    result = execute_local_ci_repair(_request(config, checked_out=True), ports)

    assert result.repair_result is update_result
    assert result.candidate_head == update_result.resulting_head
    assert result.attempt_started == update_result.attempt_started
    assert result.synchronized is (True if update_result.disposition is RepairDisposition.APPLIED else None)
    assert "repair" not in events


@pytest.mark.parametrize(
    ("update_result", "expected_trace", "diagnostic"),
    [
        (
            PRRepairResult(
                RepairDisposition.APPLIED,
                resulting_head_state=RepairFactState.AVAILABLE,
                resulting_head="head-2",
                synchronization_state=RepairFactState.AVAILABLE,
                synchronized=True,
            ),
            "pushed",
            "at head-2",
        ),
        (PRRepairResult(RepairDisposition.APPLIED), "applied_unconfirmed", "publication is unknown; resulting head: unknown"),
        (PRRepairResult(RepairDisposition.ACCEPTED_OR_UNCERTAIN), "delegated", "receipt: unknown; resulting head: unknown"),
        (PRRepairResult(RepairDisposition.DEFERRED, retry_not_before=123.0), "deferred", "waiting for a later retry"),
        (PRRepairResult(RepairDisposition.REFUSED), "refused", "was refused"),
        (PRRepairResult(RepairDisposition.FAILED), "failed", "failed"),
        (PRRepairResult(RepairDisposition.QUALITY_REFUSED), "quality_refused", "quality gate"),
    ],
)
def test_base_update_diagnostics_follow_typed_facts_not_display_text(
    update_result: PRRepairResult,
    expected_trace: str,
    diagnostic: str,
) -> None:
    """REQ-001/002/004: identical producer prose cannot manufacture a push."""
    config = AutomationConfig()
    config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = False
    effects: list[str] = []
    ports = replace(
        _ports(effects),
        update_base=lambda: UpdateActions(update_result, actions=("Updated PR with base branch",)),
        trace_base_update=lambda value: effects.append(f"trace:{value}"),
    )

    result = execute_local_ci_repair(_request(config, checked_out=True), ports)

    assert result.repair_result is update_result
    assert any(diagnostic in action for action in result.actions)
    assert f"trace:{expected_trace}" in effects
    assert effects.count(f"trace:{expected_trace}") == 1
    assert "repair" not in effects
    assert result.retry_not_before == update_result.retry_not_before


def test_base_update_trace_failure_does_not_change_selected_result() -> None:
    """REQ-003/004: diagnostic failure cannot turn publication into failure."""
    config = AutomationConfig()
    config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = False
    update_result = PRRepairResult(
        RepairDisposition.APPLIED,
        resulting_head_state=RepairFactState.AVAILABLE,
        resulting_head="head-2",
        synchronization_state=RepairFactState.AVAILABLE,
        synchronized=True,
    )
    ports = replace(
        _ports([]),
        update_base=lambda: UpdateActions(update_result),
        trace_base_update=lambda _value: (_ for _ in ()).throw(RuntimeError("trace unavailable")),
    )

    result = execute_local_ci_repair(_request(config, checked_out=True), ports)

    assert result.repair_result is update_result
    assert result.candidate_head == "head-2"
    assert result.synchronized is True
    assert result.failure_reason == ""
