from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard import init_dashboard


class MockCandidate:
    def __init__(self, type, number, priority, title):
        self.type = type
        self.data = {"number": number, "title": title}
        self.priority = priority


def test_automation_engine_get_status_structure():
    # Setup real engine with mocked dependencies
    mock_github = MagicMock()
    real_engine = AutomationEngine(mock_github)

    # Inject data into real engine
    # We need to mock the queue object to have _queue attribute
    real_engine.queue = MagicMock()
    real_engine.queue._queue = [
        MockCandidate("issue", 1, 0, "Issue 1"),
        MockCandidate("pr", 2, 7, "PR 2"),
    ]
    real_engine.queue.qsize.return_value = 2
    real_engine.active_workers = {
        0: MockCandidate("pr", 3, 3, "PR 3"),
        1: None
    }

    status = real_engine.get_status()

    # Assertions
    assert status["queue_length"] == 2
    assert len(status["queue_items"]) == 2
    assert status["queue_items"][0] == {"type": "issue", "number": 1, "priority": 0, "title": "Issue 1"}
    assert status["queue_items"][1] == {"type": "pr", "number": 2, "priority": 7, "title": "PR 2"}

    assert len(status["active_workers"]) == 2
    assert status["active_workers"][0] == {"type": "pr", "number": 3, "title": "PR 3"}
    assert status["active_workers"][1] is None


@patch("src.auto_coder.dashboard.ui")
def test_init_dashboard_registration(mock_ui):
    app = FastAPI()
    engine = MagicMock(spec=AutomationEngine)

    init_dashboard(app, engine)

    # Verify ui.page was called
    mock_ui.page.assert_called_with("/")

    # Verify ui.run_with was called
    mock_ui.run_with.assert_called()
    args, kwargs = mock_ui.run_with.call_args
    assert args[0] == app
    assert kwargs["mount_path"] == "/dashboard"
    assert kwargs["title"] == "Auto-Coder Dashboard"
