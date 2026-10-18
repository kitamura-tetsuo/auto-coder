from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import Iterator

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.ci_repair_authority import CIRepairAuthority
from auto_coder.local_ci_repair_execution import (
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

    def __iter__(self) -> Iterator[str]:
        return iter(())


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
        repair=lambda _logs, _targets: (
            events.append("repair")
            or PRRepairResult(
                RepairDisposition.APPLIED,
                ("candidate published",),
                attempt_started=True,
                resulting_head_state=RepairFactState.AVAILABLE,
                resulting_head="head-2",
                synchronization_state=RepairFactState.AVAILABLE,
                synchronized=True,
            )
        ),
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
        repair=lambda _logs, _targets: PRRepairResult(
            RepairDisposition.FAILED,
            ("commit created", "push failed"),
            reason="local CI repair was not synchronized",
            attempt_started=True,
            resulting_head_state=RepairFactState.AVAILABLE,
            resulting_head="head-2",
            synchronization_state=RepairFactState.AVAILABLE,
            synchronized=False,
        ),
    )

    result = execute_local_ci_repair(_request(config, checked_out=True), ports)

    assert result.attempt_started is True
    assert result.candidate_head == "head-2"
    assert result.synchronized is False
    assert result.failure_reason == "local CI repair was not synchronized"
