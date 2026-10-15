from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, EmptyPRResult, StaleJulesPRResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.github_ci_observer import ci_read_phase
from auto_coder.pr_processor import _handle_pr_merge


def _api(head_sha: str, pending: bool) -> Mock:
    api = Mock()
    api.headers = {}
    api.checks.list_for_ref.return_value = {"check_runs": []}
    runs = []
    if pending:
        runs = [{"id": 7, "workflow_id": 9, "run_attempt": 1, "head_sha": head_sha, "status": "in_progress", "conclusion": None, "name": "PR Tests"}]
    api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": runs}
    return api


def _changing_api(head_sha: str, state: dict[str, bool]) -> Mock:
    """Return GitHub-shaped CI responses whose verdict can change in place."""
    api = Mock()
    api.headers = {}
    api.checks.list_for_ref.return_value = {"check_runs": []}

    def workflow_runs(*_args, **_kwargs):
        pending = state["pending"]
        return {
            "workflow_runs": [
                {
                    "id": 17,
                    "workflow_id": 19,
                    "run_attempt": 1,
                    "head_sha": head_sha,
                    "status": "in_progress" if pending else "completed",
                    "conclusion": None if pending else "success",
                    "name": "PR Tests",
                }
            ]
        }

    api.actions.list_workflow_runs_for_repo.side_effect = workflow_runs
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

    api = _api(head_sha, pending)
    with (
        ci_read_phase(),
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=api),
        patch("auto_coder.automation_engine.LabelManager") as labels,
        patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult()),
        patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult()),
        patch("auto_coder.pr_processor._should_skip_waiting_for_jules", return_value=False),
        patch("auto_coder.pr_processor._is_jules_pr", return_value=False),
    ):
        labels.return_value.__enter__.return_value = True
        candidates = engine._get_candidates("owner/repo")

    assert [candidate.data["number"] for candidate in candidates if candidate.type == "pr"] == expected_numbers
    # Collection routes and deployment-approval effects consume one immutable
    # observation. A second read could contradict the policy decision while
    # the first result was being processed.
    assert api.checks.list_for_ref.call_count == 1
    assert api.actions.list_workflow_runs_for_repo.call_count == 1
    if not pending:
        assert candidates[0].priority == 2


def test_unavailable_ci_conflict_survives_collection_and_reaches_remediation(mock_github_client, tmp_path, monkeypatch):
    """REQ-005: an exact-head conflict remains actionable when CI is unavailable."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    head_sha = "b" * 40
    pr = {
        "number": 2503,
        "title": "conflicting PR with unavailable CI",
        "draft": False,
        "mergeable": False,
        "mergeable_state": "dirty",
        "head": {"ref": "issue-2503", "sha": head_sha},
        "labels": [],
        "body": "Closes #2503",
        "created_at": "2026-10-14T00:00:00Z",
        "user": {"login": "allowed"},
    }
    mock_github_client.get_open_prs_json.return_value = [pr]
    mock_github_client.get_open_issues_json.return_value = []
    api = _api(head_sha, False)
    api.checks.list_for_ref.side_effect = RuntimeError("provider unavailable")
    api.actions.list_workflow_runs_for_repo.side_effect = RuntimeError("provider unavailable")
    config = AutomationConfig()
    config.PR_AUTHOR_ALLOWLIST = []
    engine = AutomationEngine(mock_github_client, config=config)

    with (
        ci_read_phase(),
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=api),
        patch("auto_coder.automation_engine.LabelManager") as labels,
        patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult()),
        patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult()),
        patch("auto_coder.pr_processor._should_skip_waiting_for_jules", return_value=False),
        patch("auto_coder.pr_processor._is_jules_pr", return_value=False),
    ):
        labels.return_value.__enter__.return_value = True
        candidates = engine._get_candidates("owner/repo")

    candidate = next(item for item in candidates if item.type == "pr")
    with (
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=False),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": False, "merge_state_status": "dirty"}),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
        patch("auto_coder.pr_processor._start_mergeability_remediation", return_value=["entered remediation"]) as remediation,
        patch("auto_coder.util.github_action.trigger_workflow_dispatch") as dispatch,
        patch("auto_coder.pr_processor.get_detailed_checks_from_history") as failure_repair,
        patch("auto_coder.pr_processor._merge_pr") as merge,
    ):
        unsafe.return_value = Mock(metadata_error=None, actions=[], authoritative_pr_data=candidate.data, closed=False)
        actions = _handle_pr_merge(mock_github_client, "owner/repo", candidate.data, config, {})

    assert actions == ["PR #2503 is not mergeable (state: dirty)", "entered remediation"]
    remediation.assert_called_once()
    dispatch.assert_not_called()
    failure_repair.assert_not_called()
    merge.assert_not_called()


def test_pending_candidate_recovers_and_reconstructed_engine_processes_durable_watch_once(mock_github_client, tmp_path, monkeypatch):
    """REQ-004: evidence and durable ownership survive both controller lifetimes."""
    invalidation_db = tmp_path / "invalidations.sqlite3"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(invalidation_db))
    head_sha = "c" * 40
    pr = {
        "number": 2506,
        "title": "same-head CI transition",
        "draft": False,
        "mergeable": False,
        "mergeable_state": "dirty",
        "head": {"ref": "issue-2506", "sha": head_sha},
        "labels": [],
        "body": "Closes #2506",
        "created_at": "2026-10-14T00:00:00Z",
        "user": {"login": "allowed"},
    }
    mock_github_client.get_open_prs_json.return_value = [pr]
    mock_github_client.get_open_issues_json.return_value = []
    state = {"pending": True}
    api = _changing_api(head_sha, state)
    config = AutomationConfig()
    config.PR_AUTHOR_ALLOWLIST = []
    first_engine = AutomationEngine(mock_github_client, config=config)
    label_context = Mock()
    label_context.__enter__ = Mock(return_value=True)
    label_context.__exit__ = Mock(return_value=False)

    with (
        patch("auto_coder.util.github_action.get_ghapi_client", return_value=api),
        patch("auto_coder.automation_engine.LabelManager", return_value=label_context),
        patch("auto_coder.pr_processor.LabelManager", return_value=label_context),
        patch("auto_coder.pr_processor._close_empty_pr", return_value=EmptyPRResult()),
        patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=StaleJulesPRResult()),
        patch("auto_coder.pr_processor._should_skip_waiting_for_jules", return_value=False),
        patch("auto_coder.pr_processor._is_jules_pr", return_value=False),
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._merge_pr") as merge,
        patch("auto_coder.pr_processor.get_detailed_checks_from_history") as failure_repair,
    ):
        unsafe.return_value = Mock(metadata_error=None, actions=[], authoritative_pr_data=pr, closed=False)

        # The ordinary handler is the production owner that registers and
        # schedules the durable watch while this same head is pending.
        with ci_read_phase():
            pending_actions = _handle_pr_merge(mock_github_client, "owner/repo", pr, config, {})
        assert any("still in progress" in action for action in pending_actions)
        assert first_engine.invalidations.seconds_until_next_ci("owner/repo") is not None

        pending_candidates = first_engine._get_candidates("owner/repo")
        assert [item for item in pending_candidates if item.type == "pr"] == []

        state["pending"] = False
        recovered_candidates = first_engine._get_candidates("owner/repo")
        recovered_prs = [item for item in recovered_candidates if item.type == "pr"]
        assert len(recovered_prs) == 1
        assert recovered_prs[0].priority == 2

        # Reconstruct against the same real SQLite store. Selection remains
        # live, and the selected-candidate boundary reaches the real handler
        # policy exactly once without dispatching or merging.
        reconstructed = AutomationEngine(mock_github_client, config=config)
        assert reconstructed.invalidations.seconds_until_next_ci("owner/repo") is not None
        rebuilt_candidates = reconstructed._get_candidates("owner/repo")
        rebuilt_pr = next(item for item in rebuilt_candidates if item.type == "pr")

        with (
            patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": False, "merge_state_status": "dirty"}),
            patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
            patch("auto_coder.pr_processor._start_mergeability_remediation", return_value=["entered remediation"]) as remediation,
            patch("auto_coder.pr_processor._link_codex_cloud_pr_to_issue", return_value=None),
            patch("auto_coder.pr_processor._link_jules_pr_to_issue", return_value=True),
            patch("auto_coder.util.github_action.trigger_workflow_dispatch") as dispatch,
        ):
            processed = reconstructed._process_single_candidate_unified("owner/repo", rebuilt_pr, config)

    assert processed.error is None
    assert "entered remediation" in processed.actions
    remediation.assert_called_once()
    dispatch.assert_not_called()
    failure_repair.assert_not_called()
    merge.assert_not_called()
