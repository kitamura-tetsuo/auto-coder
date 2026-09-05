"""Production-path regressions for Issue discovery and slot admission."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, CandidateProcessingResult, StaleJulesPRResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.util.github_action import GitHubActionsStatusResult


def _pr(number: int, *, labels: list[str] | None = None, mergeable: bool = True) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "head": {"ref": f"pr-{number}"},
        "labels": labels or [],
        "mergeable": mergeable,
        "created_at": f"2024-01-{number:02d}T00:00:00Z",
    }


def _issue(number: int, *, labels: list[str] | None = None) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "## Requirements\n- REQ-001: Implement the requested behavior.",
        "labels": labels or [],
        "created_at": "2024-02-01T00:00:00Z",
        "has_open_sub_issues": False,
        "parent_issue_number": None,
        "linked_pr_numbers": [],
    }


@pytest.fixture
def collection_dependencies():
    with (
        patch("auto_coder.automation_engine.LabelManager") as label_manager,
        patch("auto_coder.util.github_action.preload_github_actions_status"),
        patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.util.github_action._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, ids=[])),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as reject_unsafe,
        patch("auto_coder.pr_processor._close_empty_pr") as close_empty,
        patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult()),
        patch("auto_coder.pr_processor._is_jules_pr", return_value=False),
        patch("auto_coder.pr_processor._is_dependabot_pr", return_value=False),
        patch("auto_coder.pr_processor._should_skip_waiting_for_jules", return_value=False),
    ):
        label_manager.return_value.__enter__.return_value = True
        reject_unsafe.return_value = MagicMock(closed=False, metadata_error=None, authoritative_pr_data=None)
        close_empty.return_value = MagicMock(closed=False)
        yield


def _collect(github: MagicMock, prs: list[dict], issues: list[dict]):
    github.get_open_prs_json.return_value = prs
    github.get_open_issues_json.return_value = issues
    engine = AutomationEngine(github, config=AutomationConfig(env_override=False))
    return engine, engine._get_candidates("owner/repo")


def test_many_waiting_prs_do_not_starve_ordinary_issue_with_free_slot(tmp_path, collection_dependencies):
    """A REST-discovered ordinary Issue reaches admission despite many PRs."""
    github = MagicMock()
    engine, candidates = _collect(github, [_pr(number) for number in range(1, 5)], [_issue(1742)])

    github.get_open_issues_json.assert_called_once_with("owner/repo")
    issue_candidate = next(candidate for candidate in candidates if candidate.type == "issue")
    assert issue_candidate.data["number"] == 1742
    assert issue_candidate.priority == 0

    github.get_issue_dispatch_snapshot_strict.return_value = {
        **_issue(1742),
        "labels": [{"name": "implementation-ready"}],
    }
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    engine._process_single_candidate_reserved = Mock(return_value=CandidateProcessingResult(type="issue", number=1742, title="Issue 1742", success=True))

    result = engine._process_single_candidate_unified("owner/repo", issue_candidate, engine.config)

    assert result.success is True
    engine._process_single_candidate_reserved.assert_called_once()


def test_high_priority_prs_preserve_order_without_disabling_issue_discovery(collection_dependencies):
    github = MagicMock()
    _, candidates = _collect(github, [_pr(1, labels=["urgent"], mergeable=False), _pr(2, mergeable=False)], [_issue(1742), _issue(1743, labels=["urgent"])])

    assert [(candidate.type, candidate.data["number"], candidate.priority) for candidate in candidates] == [
        ("pr", 1, 4),
        ("issue", 1743, 3),
        ("pr", 2, 2),
        ("issue", 1742, 0),
    ]
    github.get_open_issues_json.assert_called_once_with("owner/repo")


def test_full_slots_defer_rest_discovered_ordinary_issue(tmp_path, collection_dependencies):
    github = MagicMock()
    engine, candidates = _collect(github, [_pr(1), _pr(2), _pr(3), _pr(4)], [_issue(1742)])
    issue_candidate = next(candidate for candidate in candidates if candidate.type == "issue")
    github.get_issue_dispatch_snapshot_strict.return_value = {
        **_issue(1742),
        "labels": [{"name": "implementation-ready"}],
    }

    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    assert slots.start_execution(ImplementationOwner("issue", 99)) is not None
    slots.reconcile = Mock()  # type: ignore[method-assign]
    engine.implementation_slots = slots
    engine._process_single_candidate_reserved = Mock()

    result = engine._process_single_candidate_unified("owner/repo", issue_candidate, engine.config)

    assert result.success is False
    assert result.actions == ["Deferred - logical implementation limit is occupied (issue:1742)"]
    engine._process_single_candidate_reserved.assert_not_called()
