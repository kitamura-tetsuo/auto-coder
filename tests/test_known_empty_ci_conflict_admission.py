from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, EmptyPRResult, StaleJulesPRResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.github_ci_observer import ci_read_phase


def _api(head_sha: str, pending: bool) -> Mock:
    api = Mock()
    api.headers = {}
    api.checks.list_for_ref.return_value = {"check_runs": []}
    runs = []
    if pending:
        runs = [{"id": 7, "workflow_id": 9, "run_attempt": 1, "head_sha": head_sha, "status": "in_progress", "conclusion": None, "name": "PR Tests"}]
    api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": runs}
    return api


@pytest.mark.parametrize(("pending", "expected_numbers"), [(False, [2497]), (True, [])])
def test_real_ci_decoding_controls_conflicting_candidate_admission(mock_github_client, pending, expected_numbers):
    """Real observation decoding must admit only exact-head known-empty conflicts."""
    head_sha = "a" * 40
    pr = {
        "number": 2497,
        "title": "conflicting PR",
        "draft": False,
        "mergeable": False,
        "mergeable_state": "dirty",
        "head": {"ref": "feature", "sha": head_sha},
        "labels": [],
        "body": "Closes #2496",
        "created_at": "2026-10-14T00:00:00Z",
        "user": {"login": "allowed"},
    }
    mock_github_client.get_open_prs_json.return_value = [pr]
    mock_github_client.get_open_issues_json.return_value = []
    config = AutomationConfig()
    config.PR_AUTHOR_ALLOWLIST = []
    engine = AutomationEngine(mock_github_client, config=config)

    with (
        ci_read_phase(),
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=_api(head_sha, pending)),
        patch("auto_coder.automation_engine.LabelManager") as labels,
        patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult()),
        patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult()),
        patch("auto_coder.pr_processor._should_skip_waiting_for_jules", return_value=False),
        patch("auto_coder.pr_processor._is_jules_pr", return_value=False),
    ):
        labels.return_value.__enter__.return_value = True
        candidates = engine._get_candidates("owner/repo")

    assert [candidate.data["number"] for candidate in candidates if candidate.type == "pr"] == expected_numbers
    if not pending:
        assert candidates[0].priority == 2
