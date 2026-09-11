from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard import init_dashboard
from src.auto_coder.implementation_slots import (
    ImplementationOwner,
    ImplementationSlotRepository,
    ImplementationSlotSnapshot,
)


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
    real_engine.active_workers = {0: MockCandidate("pr", 3, 3, "PR 3"), 1: None}

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

    init_dashboard(app, engine, "owner/repo")

    # Verify ui.page was called
    mock_ui.page.assert_any_call("/")

    # Verify ui.run_with was called
    mock_ui.run_with.assert_called()
    args, kwargs = mock_ui.run_with.call_args
    assert args[0] == app
    assert kwargs["mount_path"] == "/dashboard"
    assert kwargs["title"] == "Auto-Coder Dashboard"


def test_get_implementation_slot_snapshot_lazily_binds_the_repository_bound_store(tmp_path):
    """REQ-001 of Issue #1993: before any worker has run, `implementation_slots`
    is `None`. `get_implementation_slot_snapshot` must still resolve and
    observe this engine's own effective store for the requested repository
    (via `_get_implementation_slots`'s existing lazy-initialization), not
    substitute a default or another repository's store."""
    real_engine = AutomationEngine(MagicMock())
    assert real_engine.implementation_slots is None

    with patch("src.auto_coder.automation_engine.ImplementationSlotRepository") as mock_repo_class:
        mock_repo_class.side_effect = lambda repo_name, limit: ImplementationSlotRepository(repo_name, limit, tmp_path / "slots.json")

        observation = real_engine.get_implementation_slot_snapshot("owner/repo")

    assert isinstance(observation, ImplementationSlotSnapshot)
    assert observation.repository == "owner/repo"
    assert observation.owners == ()
    assert observation.normal_usage == 0
    # The now-bound repository instance is cached for later admission/
    # lifecycle calls, not just this diagnostic read.
    assert real_engine.implementation_slots is not None
    assert real_engine.implementation_slots.repo_name == "owner/repo"


def test_get_implementation_slot_snapshot_observes_real_recorded_ownership(tmp_path):
    """The wrapper returns the same coherent observation the underlying
    repository's own `snapshot()` produces for real recorded ownership."""
    real_engine = AutomationEngine(MagicMock())
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 321)
    assert slots.start_execution(owner) is not None
    real_engine.implementation_slots = slots

    observation = real_engine.get_implementation_slot_snapshot("owner/repo")

    assert isinstance(observation, ImplementationSlotSnapshot)
    assert [o.owner_key for o in observation.owners] == ["issue:321"]
    assert observation.normal_usage == 1
    assert observation.normal_available == 1


def test_get_implementation_slot_snapshot_never_rebinds_to_a_different_repository(tmp_path):
    """A diagnostic read for a repository OTHER than the one this controller
    is already bound to must observe that other repository's own store
    without mutating `self.implementation_slots` (REQ-006 of Issue #1993):
    a read-only dashboard observation must never rebind the controller, and
    its subsequent lifecycle target must remain the original repository."""
    real_engine = AutomationEngine(MagicMock())
    bound_slots = ImplementationSlotRepository("owner/repo-b", 2, tmp_path / "repo-b-slots.json")
    bound_owner = ImplementationOwner("issue", 111)
    assert bound_slots.start_execution(bound_owner) is not None
    real_engine.implementation_slots = bound_slots

    other_slots = ImplementationSlotRepository("owner/repo-a", 2, tmp_path / "repo-a-slots.json")
    other_owner = ImplementationOwner("issue", 222)
    assert other_slots.start_execution(other_owner) is not None

    with patch("src.auto_coder.automation_engine.ImplementationSlotRepository") as mock_repo_class:
        mock_repo_class.side_effect = lambda repo_name, limit: ImplementationSlotRepository(repo_name, limit, tmp_path / "repo-a-slots.json")

        observation = real_engine.get_implementation_slot_snapshot("owner/repo-a")

    # The observation itself is correct for the requested (mismatched) repo...
    assert isinstance(observation, ImplementationSlotSnapshot)
    assert observation.repository == "owner/repo-a"
    assert [o.owner_key for o in observation.owners] == ["issue:222"]
    # ...but the controller's actual binding and subsequent lifecycle target
    # remain untouched: still the original repository/instance, not
    # replaced, and no fallback/alternate store was substituted for it.
    assert real_engine.implementation_slots is bound_slots
    assert real_engine.implementation_slots.repo_name == "owner/repo-b"
    assert real_engine._get_implementation_slots("owner/repo-b") is bound_slots
    resumed = real_engine.get_implementation_slot_snapshot("owner/repo-b")
    assert [o.owner_key for o in resumed.owners] == ["issue:111"]
