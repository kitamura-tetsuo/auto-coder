import os
import sys
from pathlib import Path
from typing import Any

import pytest


def test_run_graph_builder_uses_fallback_in_pytest(monkeypatch):
    from auto_coder.graphrag_index_manager import GraphRAGIndexManager

    # Ensure pytest env flag is visible
    os.environ["PYTEST_CURRENT_TEST"] = os.environ.get("PYTEST_CURRENT_TEST", "1")

    mgr = GraphRAGIndexManager()

    called = {"ok": False}

    def fake_fallback() -> dict[str, Any]:
        called["ok"] = True
        return {"nodes": [], "edges": []}

    # Patch only the instance method for precise assertion
    monkeypatch.setattr(mgr, "_fallback_python_indexing", fake_fallback)

    result = mgr._run_graph_builder()

    assert called["ok"] is True
    assert result == {"nodes": [], "edges": []}


def test_test_watcher_skips_playwright_under_pytest(tmp_path):
    # Import test_watcher_tool via sys.path like existing tests
    test_watcher_path = Path(__file__).parent.parent / "src" / "auto_coder" / "mcp_servers" / "test_watcher"
    sys.path.insert(0, str(test_watcher_path))
    try:
        from test_watcher_tool import TestWatcherTool  # type: ignore
    finally:
        # Clean up path insertion to avoid side effects on other tests
        sys.path.pop(0)

    os.environ["PYTEST_CURRENT_TEST"] = os.environ.get("PYTEST_CURRENT_TEST", "1")

    tool = TestWatcherTool(project_root=str(tmp_path))

    # Directly run; should short-circuit and not spawn external processes
    tool._run_playwright_tests(last_failed=False)

    e2e = tool.test_results.get("e2e", {})
    assert e2e.get("status") == "completed"
    assert e2e.get("total") == 0
    assert e2e.get("passed") == 0 and e2e.get("failed") == 0


def test_watcher_observer_is_daemon(tmp_path):
    # Import as above
    test_watcher_path = Path(__file__).parent.parent / "src" / "auto_coder" / "mcp_servers" / "test_watcher"
    sys.path.insert(0, str(test_watcher_path))
    try:
        from test_watcher_tool import TestWatcherTool  # type: ignore
    finally:
        sys.path.pop(0)

    tool = TestWatcherTool(project_root=str(tmp_path))
    try:
        status = tool.start_watching()
        assert status["status"] in {"started", "already_running"}
        # Observer should be present and daemonized
        assert tool.observer is not None
        assert getattr(tool.observer, "daemon", True) is True
    finally:
        tool.stop_watching()
