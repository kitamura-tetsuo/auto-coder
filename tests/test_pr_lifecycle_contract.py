"""Maintained production-origin contracts for PR lifecycle extraction.

These tests deliberately enter through production functions and replace only
GitHub/provider/Git effect boundaries.  They are selectable as a stable group
with ``-m pr_lifecycle_contract``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.ci_observation import (
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from auto_coder.pr_processor import _handle_pr_merge
from auto_coder.util.github_action import _check_github_actions_status

pytestmark = pytest.mark.pr_lifecycle_contract

REPOSITORY = "owner/repo"
PR_NUMBER = 2501
H1 = "1" * 40
H2 = "2" * 40


def _snapshot(
    availability: ObservationAvailability,
    conclusion: CIConclusion | None = None,
    *,
    head_sha: str = H1,
    attempt: int = 1,
) -> CIObservationSnapshot:
    facts = ()
    if conclusion is not None:
        facts = (
            WorkflowObservation(
                WorkflowExecutionIdentity("pr-tests", "701", attempt),
                conclusion,
                "PR Tests",
                workflow_path=".github/workflows/pr-tests.yml",
            ),
        )
    return CIObservationSnapshot(
        ObservationSubject("https://api.github.test", REPOSITORY, PR_NUMBER, head_sha),
        ObservationRequest("github-actions", "checks+workflows"),
        f"cycle-{availability.value}-{attempt}",
        0,
        availability,
        facts,
        unavailable_reason="provider unavailable" if availability is ObservationAvailability.UNAVAILABLE else None,
    )


@pytest.mark.parametrize(
    ("snapshot", "success", "pending", "error", "ids"),
    [
        (_snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS), True, False, None, [701]),
        (_snapshot(ObservationAvailability.KNOWN, CIConclusion.PENDING), False, True, None, [701]),
        (_snapshot(ObservationAvailability.KNOWN_EMPTY), False, True, "No current CI observations", []),
        (_snapshot(ObservationAvailability.UNAVAILABLE), False, False, "provider unavailable", []),
        (_snapshot(ObservationAvailability.SUPERSEDED), False, False, "superseded", []),
    ],
)
def test_real_ci_interpreter_preserves_non_authorizing_observations(snapshot, success, pending, error, ids):
    """REQ-001/002: GitHub-shaped observations reach the real interpreter."""
    client = SimpleNamespace(token="controlled-token")
    pr_data = {"number": PR_NUMBER, "head": {"sha": H1}}

    with patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()), patch("auto_coder.util.github_action.observe_ci", return_value=snapshot):
        result = _check_github_actions_status(REPOSITORY, pr_data, AutomationConfig(), client)

    assert result.success is success
    assert result.in_progress is pending
    assert result.error == error
    assert result.ids == ids
    assert result.observation is snapshot


def test_real_ci_interpreter_uses_only_newest_workflow_attempt():
    """REQ-002: a pending H2 observation supersedes an older green attempt."""
    old = _snapshot(ObservationAvailability.KNOWN, CIConclusion.SUCCESS).facts[0]
    new = WorkflowObservation(
        WorkflowExecutionIdentity("pr-tests", "701", 2),
        CIConclusion.PENDING,
        "PR Tests",
        workflow_path=".github/workflows/pr-tests.yml",
    )
    snapshot = CIObservationSnapshot(
        ObservationSubject("https://api.github.test", REPOSITORY, PR_NUMBER, H2),
        ObservationRequest("github-actions", "checks+workflows"),
        "cycle-h2",
        0,
        ObservationAvailability.KNOWN,
        (old, new),
    )
    client = SimpleNamespace(token="controlled-token")

    with patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()), patch("auto_coder.util.github_action.observe_ci", return_value=snapshot):
        result = _check_github_actions_status(REPOSITORY, {"number": PR_NUMBER, "head": {"sha": H2}}, AutomationConfig(), client)

    assert result.success is False
    assert result.in_progress is True
    assert result.ids == [701]


@pytest.mark.parametrize(
    ("enabled", "exhausted", "expected_calls", "expected_text"),
    [
        (True, False, 1, "controlled repair boundary"),
        (False, False, 0, "not mergeable"),
        (True, True, 0, "repair allowance exhausted"),
    ],
)
def test_direct_processing_routes_conflicts_only_to_authorized_effect_boundary(tmp_path, monkeypatch, enabled, exhausted, expected_calls, expected_text):
    """REQ-001/002: direct processing selects the existing conflict boundary."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = enabled
    pr_data = {
        "number": PR_NUMBER,
        "body": "Closes #2501",
        "labels": [],
        "head": {"ref": "issue-2501", "sha": H1},
    }
    exhaustion = SimpleNamespace(is_exhausted=True, exhausted_blocker_ids=("conflict",)) if exhausted else None
    client = MagicMock()

    with (
        patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": False, "merge_state_status": "dirty"}),
        patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=exhaustion),
        patch("auto_coder.pr_processor.publish_exhaustion_comment_deduped"),
        patch("auto_coder.pr_processor._start_mergeability_remediation", return_value=["controlled repair boundary"]) as repair,
    ):
        unsafe.return_value = SimpleNamespace(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, REPOSITORY, pr_data, config, {})

    assert repair.call_count == expected_calls
    assert any(expected_text in action for action in actions)
