"""Mandatory production-to-mounted-view dashboard observability regressions."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.dashboard import init_dashboard
from auto_coder.execution_trace import EventKind, TraceCollector, get_trace_collector


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


def _mounted_detail(mock_ui, item_type: str, item_number: int):
    pages = {}

    def page(path):
        def register(function):
            pages[path] = function
            return function

        return register

    mock_ui.page.side_effect = page
    init_dashboard(FastAPI(), MagicMock(spec=AutomationEngine), "owner/repo")
    pages["/detail/{item_type}/{item_number}"](item_type=item_type, item_number=item_number)
    return mock_ui.mermaid.call_args[0][0] if mock_ui.mermaid.called else ""


def _assert_required_stage_visible(diagram: str, display_text: str) -> None:
    assert display_text in diagram, f"required production stage {display_text!r} did not reach the mounted detail view"


def _run_admission_to_view(mock_ui, item_type: str, item_number: int) -> None:
    """A real pre-worker denial is displayed without fabricated downstream work."""
    config = AutomationConfig()
    if item_type == "issue":
        config.ISSUE_ALLOWLIST = []
    else:
        config.PR_ALLOWLIST = []
    candidate = Candidate(
        type=item_type,
        data={"number": item_number, "title": "Denied", "body": "", "labels": [], "head": {"sha": "a" * 40}},
        priority=0,
    )

    result = AutomationEngine(MagicMock(), config)._process_single_candidate_unified("owner/repo", candidate, config)

    assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
    snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type=item_type, item_number=item_number)
    assert any(event.kind == EventKind.STAGE_RESULT.value and event.stage_id == f"{item_type}.author-admission" for event in snapshot.events)
    diagram = _mounted_detail(mock_ui, item_type, item_number)
    _assert_required_stage_visible(diagram, "author admission")
    assert f"{item_type}.ci-" not in diagram
    assert f"{item_type}.merge-" not in diagram
    assert f"{item_type}.dispatch" not in diagram


@patch("auto_coder.dashboard.ui")
def test_issue_admission_reaches_mounted_detail_view(mock_ui):
    _run_admission_to_view(mock_ui, "issue", 194801)


@patch("auto_coder.dashboard.ui")
def test_pr_admission_reaches_mounted_detail_view(mock_ui):
    _run_admission_to_view(mock_ui, "pr", 194802)


@patch("auto_coder.dashboard.ui")
def test_missing_producer_emission_is_rejected_by_joined_oracle(mock_ui, monkeypatch):
    """Mutation control: unchanged business denial cannot pass without its emission."""
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = []
    collector = get_trace_collector()
    real_record = collector.record_event

    def suppress_required(kind, stage_id, origin, **kwargs):
        if stage_id == "issue.author-admission":
            return None
        return real_record(kind, stage_id, origin, **kwargs)

    monkeypatch.setattr(collector, "record_event", suppress_required)
    candidate = Candidate(type="issue", data={"number": 194805, "title": "Denied", "body": "", "labels": []}, priority=0)

    result = AutomationEngine(MagicMock(), config)._process_single_candidate_unified("owner/repo", candidate, config)

    assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
    snapshot = collector.get_snapshot(repository="owner/repo", item_type="issue", item_number=194805)
    assert not any(event.kind == EventKind.STAGE_RESULT.value and event.stage_id == "issue.author-admission" for event in snapshot.events)
    diagram = _mounted_detail(mock_ui, "issue", 194805)
    with pytest.raises(AssertionError, match="did not reach the mounted detail view"):
        _assert_required_stage_visible(diagram, "author admission")
