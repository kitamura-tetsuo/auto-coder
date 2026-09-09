from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard import init_dashboard
from src.auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


def _capture_pages(mock_ui):
    captured_functions = {}

    def capture_page(path):
        def decorator(func):
            captured_functions[path] = func
            return func

        return decorator

    mock_ui.page.side_effect = capture_page
    return captured_functions


@patch("src.auto_coder.dashboard.ui")
def test_dashboard_detail_page_registration_and_render(mock_ui):
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)

    collector = get_trace_collector()
    with collector.start_execution("owner/repo", "pr", 123, origin="test") as handle:
        collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.ci-eligibility", origin="test", outcome=Outcome.COMPLETED, facts={"success": True, "in_progress": False})
        collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.merge-delivery", origin="test", outcome=Outcome.DEFERRED, facts={"reason": "approval not completed"})
        handle.set_outcome(Outcome.COMPLETED)

    captured_functions = _capture_pages(mock_ui)

    init_dashboard(app, engine, "owner/repo")

    assert "/detail/{item_type}/{item_number}" in captured_functions
    detail_page_func = captured_functions["/detail/{item_type}/{item_number}"]

    detail_page_func(item_type="pr", item_number=123)

    # Repository is displayed and scoped from init_dashboard, not guessed.
    label_calls = [str(args[0]) for args, _ in mock_ui.label.call_args_list]
    assert any("Repository: owner/repo" in text for text in label_calls)

    # Mermaid diagram was rendered with the observed stage labels and
    # "observed order" edges, not a static workflow graph.
    mock_ui.mermaid.assert_called()
    mermaid_args = mock_ui.mermaid.call_args[0][0]
    assert "graph TD" in mermaid_args
    assert "observed order" in mermaid_args
    assert "pr.ci-eligibility" in mermaid_args
    assert "pr.merge-delivery" in mermaid_args

    # Copy button and evidence/decision tables were rendered.
    button_calls = [kwargs.get("icon") for _, kwargs in mock_ui.button.call_args_list]
    assert "content_copy" in button_calls
    assert mock_ui.table.call_count >= 2  # evidence panel + decision log

    # Back button
    mock_ui.link.assert_any_call("Back to Dashboard", "/")

    # Auto-refresh timer is registered and wired to stop on disconnect.
    assert mock_ui.timer.called
    mock_ui.context.client.on_disconnect.assert_called()


@patch("src.auto_coder.dashboard.ui")
def test_dashboard_detail_invalid_item_type_reports_without_fallback(mock_ui):
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)
    captured_functions = _capture_pages(mock_ui)

    init_dashboard(app, engine, "owner/repo")
    detail_page_func = captured_functions["/detail/{item_type}/{item_number}"]

    mock_ui.reset_mock()
    detail_page_func(item_type="commit", item_number=123)

    label_calls = [str(args[0]) for args, _ in mock_ui.label.call_args_list]
    assert any("Invalid or unresolved target" in text for text in label_calls)
    # Must not proceed to render a diagram or start polling for an invalid target.
    mock_ui.mermaid.assert_not_called()
    mock_ui.timer.assert_not_called()


@patch("src.auto_coder.dashboard.ui")
def test_dashboard_detail_unresolved_item_number_reports_without_fallback(mock_ui):
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)
    captured_functions = _capture_pages(mock_ui)

    init_dashboard(app, engine, "owner/repo")
    detail_page_func = captured_functions["/detail/{item_type}/{item_number}"]

    mock_ui.reset_mock()
    detail_page_func(item_type="pr", item_number=0)

    label_calls = [str(args[0]) for args, _ in mock_ui.label.call_args_list]
    assert any("Invalid or unresolved target" in text for text in label_calls)
    mock_ui.mermaid.assert_not_called()


@patch("src.auto_coder.dashboard.ui")
def test_dashboard_detail_different_repository_is_not_mixed_in(mock_ui):
    """AS-005: two repositories sharing an item number must not mix evidence."""
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)

    collector = get_trace_collector()
    with collector.start_execution("owner/repo-a", "pr", 42, origin="test") as handle_a:
        collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.only-in-a", origin="test", outcome=Outcome.COMPLETED)
        handle_a.set_outcome(Outcome.COMPLETED)
    with collector.start_execution("owner/repo-b", "pr", 42, origin="test") as handle_b:
        collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.only-in-b", origin="test", outcome=Outcome.COMPLETED)
        handle_b.set_outcome(Outcome.COMPLETED)

    captured_functions = _capture_pages(mock_ui)
    init_dashboard(app, engine, "owner/repo-a")
    detail_page_func = captured_functions["/detail/{item_type}/{item_number}"]

    detail_page_func(item_type="pr", item_number=42)

    mermaid_args = mock_ui.mermaid.call_args[0][0]
    assert "pr.only-in-a" in mermaid_args
    assert "pr.only-in-b" not in mermaid_args


@patch("src.auto_coder.dashboard.ui")
def test_dashboard_detail_no_executions_reports_absence_not_failure(mock_ui):
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)
    captured_functions = _capture_pages(mock_ui)

    init_dashboard(app, engine, "owner/repo")
    detail_page_func = captured_functions["/detail/{item_type}/{item_number}"]

    detail_page_func(item_type="issue", item_number=999)

    label_calls = [str(args[0]) for args, _ in mock_ui.label.call_args_list]
    assert any("No execution has been observed locally" in text for text in label_calls)
    mock_ui.mermaid.assert_not_called()


@patch("src.auto_coder.dashboard.ui")
def test_dashboard_main_page_search(mock_ui):
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)
    engine.get_status.return_value = {"active_workers": {}, "queue_items": []}

    captured_functions = _capture_pages(mock_ui)

    init_dashboard(app, engine, "owner/repo")

    main_page_func = captured_functions["/"]
    main_page_func()

    mock_ui.select.assert_called()
    mock_ui.number.assert_called()
    button_calls = [args[0] for args, _ in mock_ui.button.call_args_list]
    assert "Go" in button_calls
