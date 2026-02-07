import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nicegui import ui

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard import init_dashboard
from src.auto_coder.trace_logger import TraceLogger, get_trace_logger


class TestTraceLogger:
    def setup_method(self):
        # Reset singleton instance before each test
        TraceLogger._instance = None

    def test_singleton(self):
        logger1 = get_trace_logger()
        logger2 = get_trace_logger()
        assert logger1 is logger2
        assert isinstance(logger1, TraceLogger)

    def test_logging(self):
        logger = get_trace_logger()
        logger.log("Category1", "Message1", details={"key": "value"})

        logs = logger.get_logs()
        assert len(logs) == 1
        assert logs[0]["category"] == "Category1"
        assert logs[0]["message"] == "Message1"
        assert logs[0]["details"] == {"key": "value"}
        assert "timestamp" in logs[0]

    def test_filtering(self):
        logger = get_trace_logger()
        logger.log("General", "Info")
        logger.log("PR", "Processing", item_type="pr", item_number=123)
        logger.log("Issue", "Analyzing", item_type="issue", item_number=456)

        # Test filtering by PR 123
        pr_logs = logger.get_logs(item_type="pr", item_number=123)
        assert len(pr_logs) == 1
        assert pr_logs[0]["message"] == "Processing"

        # Test filtering by Issue 456
        issue_logs = logger.get_logs(item_type="issue", item_number=456)
        assert len(issue_logs) == 1
        assert issue_logs[0]["message"] == "Analyzing"

        # Test filtering by non-existent item
        empty_logs = logger.get_logs(item_type="pr", item_number=999)
        assert len(empty_logs) == 0

    def test_clear_logs(self):
        logger = get_trace_logger()
        logger.log("Test", "Message")
        assert len(logger.get_logs()) == 1

        logger.clear()
        assert len(logger.get_logs()) == 0

    def test_circular_buffer(self):
        # Create a logger with small max_len for testing
        TraceLogger._instance = None
        logger = TraceLogger(max_len=3)

        logger.log("1", "One")
        logger.log("2", "Two")
        logger.log("3", "Three")
        assert len(logger.get_logs()) == 3

        logger.log("4", "Four")
        logs = logger.get_logs()
        assert len(logs) == 3
        assert logs[0]["message"] == "Two"
        assert logs[2]["message"] == "Four"


class TestDashboardReachability:
    def setup_method(self):
        # Reset TraceLogger singleton
        TraceLogger._instance = None

    def test_dashboard_endpoint_reachable(self):
        # Create a real FastAPI app
        app = FastAPI()

        # Mock AutomationEngine
        mock_engine = MagicMock(spec=AutomationEngine)
        mock_engine.get_status.return_value = {"active_workers": {}, "queue_items": []}

        # Initialize dashboard with REAL nicegui
        # Note: nicegui uses global state, so we need to be careful.
        # However, for a simple reachability test, it should be fine if run once or in isolation.
        init_dashboard(app, mock_engine)

        client = TestClient(app)

        # Test main dashboard page
        response = client.get("/dashboard/")
        assert response.status_code == 200
        # Check for some nicegui content to verify it rendered
        assert "<!DOCTYPE html>" in response.text
        assert "Auto-Coder Dashboard" in response.text or "NiceGUI" in response.text

        # Test detail page (mock data required for TraceLogger if detail page fetches it)
        # The detail page uses get_trace_logger(), so we should populate it or mock it.
        # Since we use real init_dashboard, it calls real get_trace_logger().
        # We can just populate the logger.
        logger = get_trace_logger()
        logger.log("Test", "Detail Log", item_type="pr", item_number=123)

        response = client.get("/dashboard/detail/pr/123")
        assert response.status_code == 200
        assert "Detail View" in response.text
