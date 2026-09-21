from unittest.mock import MagicMock, patch

from fastapi import FastAPI

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard import init_dashboard
from src.auto_coder.dashboard_repo_jobs import facts_rows, select_execution, status_text
from src.auto_coder.execution_trace import Outcome
from src.auto_coder.repo_job_trace import (
    RepoJobFacts,
    RepoJobTarget,
    RepoJobTraceCollector,
    get_repo_job_trace_collector,
)

TARGET = RepoJobTarget("owner/repo", "dependency-rescan")


def setup_function() -> None:
    RepoJobTraceCollector._instance = None


def teardown_function() -> None:
    RepoJobTraceCollector._instance = None


def test_projection_follows_latest_and_keeps_exact_pin_across_generation_reuse() -> None:
    collector = get_repo_job_trace_collector()
    with collector.start_execution(TARGET, "test", facts=RepoJobFacts(observed_invalidation_generation=7)) as first:
        first_id = first.scope.execution_id
        first.set_outcome(Outcome.FAILED)
    with collector.start_execution(TARGET, "test", facts=RepoJobFacts(observed_invalidation_generation=7)) as second:
        second_id = second.scope.execution_id
        collector.record_stage_reached(
            "dependency-rescan.completed",
            "test",
            outcome=Outcome.COMPLETED,
            facts=RepoJobFacts(discovered_issue_count=3, confirmed_handoff_count=3),
        )
        second.set_outcome(Outcome.COMPLETED)

    snapshot = collector.get_snapshot(TARGET)
    assert select_execution(snapshot, TARGET, None).execution.execution_id == second_id
    pinned = select_execution(snapshot, TARGET, first_id)
    assert pinned.execution.execution_id == first_id
    assert pinned.pinned_evicted is False
    missing = select_execution(snapshot, TARGET, "evicted-id")
    assert missing.execution is None
    assert missing.pinned_evicted is True


def test_projection_preserves_absence_and_does_not_derive_counts() -> None:
    rows = dict(facts_rows(RepoJobFacts(target_issue_refs=get_repo_job_trace_collector().clip_target_issue_refs([1, 2], total_count=9, truncated=True))))
    assert rows["Discovered Issues"] == "unavailable"
    assert rows["Confirmed handoffs"] == "unavailable"
    assert rows["Scheduled wake (not an eligibility deadline)"] == "unavailable"


def test_completed_status_requires_the_recorded_completion_stage() -> None:
    collector = get_repo_job_trace_collector()
    with collector.start_execution(TARGET, "test") as handle:
        handle.set_outcome(Outcome.COMPLETED)
    snapshot = collector.get_snapshot(TARGET)
    summary = next(iter(snapshot.executions.values()))
    events = [item for item in snapshot.observations if item.execution_id == summary.execution_id]
    assert status_text(summary, events) == "Finished; rescan completion unavailable"


@patch("src.auto_coder.dashboard.ui")
def test_mounted_job_route_and_exact_legacy_alias(mock_ui: MagicMock) -> None:
    pages = {}

    def page(path):
        def decorator(callback):
            pages[path] = callback
            return callback

        return decorator

    mock_ui.page.side_effect = page
    init_dashboard(FastAPI(), MagicMock(spec=AutomationEngine), "owner/repo")

    assert "/jobs/dependency-rescan" in pages
    pages["/detail/{item_type}/{item_number}"]("dependency", 1)
    mock_ui.navigate.to.assert_called_once_with("/jobs/dependency-rescan")

    mock_ui.reset_mock()
    pages["/detail/{item_type}/{item_number}"]("dependency", 2)
    assert any("Invalid or unresolved target" in str(call.args[0]) for call in mock_ui.label.call_args_list)
    mock_ui.navigate.to.assert_not_called()
