from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard import init_dashboard


@patch("src.auto_coder.dashboard.ui")
@patch("src.auto_coder.dashboard.get_trace_logger")
def test_dashboard_detail_page_registration_and_render(mock_get_trace_logger, mock_ui):
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)

    # Setup TraceLogger mock
    mock_logger_instance = MagicMock()
    mock_get_trace_logger.return_value = mock_logger_instance
    mock_logger_instance.get_logs.return_value = [
        {"timestamp": 1700000000, "category": "Merge Check", "message": "Check", "details": {"mergeable": True}},
        {"timestamp": 1700000001, "category": "CI Status", "message": "CI", "details": {"success": True, "in_progress": False}},
    ]

    # Capture page functions
    captured_functions = {}

    def capture_page(path):
        def decorator(func):
            captured_functions[path] = func
            return func

        return decorator

    mock_ui.page.side_effect = capture_page

    init_dashboard(app, engine)

    # Verify detail page registration
    assert "/detail/{item_type}/{item_number}" in captured_functions
    detail_page_func = captured_functions["/detail/{item_type}/{item_number}"]

    # Call detail_page_func
    detail_page_func(item_type="pr", item_number=123)

    # Verify TraceLogger called
    mock_logger_instance.get_logs.assert_called_with(item_type="pr", item_number=123)

    # Verify UI components called
    assert mock_ui.label.call_count > 0
    assert mock_ui.table.call_count > 0

    # Verify specific label content (metrics)
    # We can check if ui.label was called with expected strings
    label_calls = [str(args[0]) for args, _ in mock_ui.label.call_args_list]
    assert "True" in label_calls
    assert "Success" in label_calls

    # Verify Back button
    mock_ui.link.assert_any_call("Back to Dashboard", "/")


@patch("src.auto_coder.dashboard.ui")
def test_dashboard_main_page_search(mock_ui):
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)
    engine.get_status.return_value = {"active_workers": {}, "queue_items": []}

    captured_functions = {}

    def capture_page(path):
        def decorator(func):
            captured_functions[path] = func
            return func

        return decorator

    mock_ui.page.side_effect = capture_page

    init_dashboard(app, engine)

    main_page_func = captured_functions["/"]
    main_page_func()

    # Verify search components
    mock_ui.select.assert_called()
    mock_ui.number.assert_called()
    # Check if button "Go" exists
    button_calls = [args[0] for args, _ in mock_ui.button.call_args_list]
    assert "Go" in button_calls
