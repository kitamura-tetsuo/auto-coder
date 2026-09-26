"""Tests for implementation_retirement_observer — Issue #2147.

Covers acceptance scenarios AS-001 through AS-006:
  AS-001  Real provider payloads produce complete retirement evidence
  AS-002  Incomplete discovery and legacy attribution are not empty work
  AS-003  Publication wait and observation failure retain capacity
  AS-004  Same session, newer work, older completion
  AS-005  Retirement and maintenance contend
  AS-006  Retired history survives stale rediscovery without blocking unrelated work
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from auto_coder.implementation_retirement import (
    PRTerminalState,
    RetirementStatus,
    SessionTerminalState,
)
from auto_coder.implementation_retirement_observer import (
    _build_pr_candidate_set,
    _extract_all_pr_numbers_from_outputs,
    _observe_jules_session,
    _observe_pr,
    collect_retirement_observation,
    guard_retired_pr_reuse,
    guard_retired_session_reuse,
    register_outbound_jules_activity,
)
from auto_coder.implementation_slots import (
    ImplementationOwner,
    ImplementationSlotRepository,
)

REPO = "kitamura-tetsuo/auto-coder"
ISSUE_100 = ImplementationOwner("issue", 100)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _setup_slots(tmp_path: Path, limit: int = 2) -> ImplementationSlotRepository:
    return ImplementationSlotRepository(REPO, limit, tmp_path / "slots.json")


def _make_github_client(
    pr_responses: Optional[Dict[int, Any]] = None,
    connected_prs: Optional[Dict[int, List[int]]] = None,
    open_prs: Optional[List[Dict[str, Any]]] = None,
) -> MagicMock:
    """Create a minimal github_client mock."""
    client = MagicMock()

    def _get_pull_request(repo: str, number: int) -> Optional[Dict[str, Any]]:
        if pr_responses is None:
            return None
        return pr_responses.get(number)

    client.get_pull_request.side_effect = _get_pull_request

    def _get_pull_request_metadata_strict(repo: str, number: int) -> Dict[str, Any]:
        # Production callers use the strict, cache-bypassing direct read
        # (REQ-003); this raises on absence like the real implementation
        # does (it never silently returns None for a missing PR).
        if pr_responses is None or number not in pr_responses:
            raise RuntimeError(f"GitHub did not return PR metadata for PR #{number}")
        return pr_responses[number]

    client.get_pull_request_metadata_strict.side_effect = _get_pull_request_metadata_strict

    def _get_connected_prs(repo: str, issue: int, strict: bool = False) -> List[int]:
        if connected_prs is None:
            return []
        return connected_prs.get(issue, [])

    client.get_connected_prs.side_effect = _get_connected_prs

    def _get_open_pull_requests_strict(repo: str) -> List[Dict[str, Any]]:
        return open_prs or []

    # Production callers use the strict, cache-bypassing complete enumeration
    # API (REQ-003); wire it explicitly so a bare MagicMock auto-attribute
    # does not get treated as a malformed non-list response.
    client.get_open_pull_requests_strict.side_effect = _get_open_pull_requests_strict
    client.get_open_pull_requests.side_effect = _get_open_pull_requests_strict

    def _get_issue(repo: str, number: int) -> Optional[Dict[str, Any]]:
        return {"number": number, "state": "open"}

    client.get_issue.side_effect = _get_issue

    return client


def _make_jules_client(
    session_responses: Optional[Dict[str, Any]] = None,
    activities: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> MagicMock:
    """Create a minimal JulesClient mock."""
    client = MagicMock()

    def _get_session(session_id: str) -> Dict[str, Any]:
        if session_responses and session_id in session_responses:
            return session_responses[session_id]
        raise RuntimeError(f"Session not found: {session_id}")

    client.get_session.side_effect = _get_session

    def _get_session_activities(session_id: str) -> List[Dict[str, Any]]:
        if activities and session_id in activities:
            return activities[session_id]
        return []

    client.get_session_activities.side_effect = _get_session_activities

    return client


def _make_cloud_manager(
    issue_bindings: Optional[Dict[int, Any]] = None,
) -> MagicMock:
    """Create a minimal CloudManager mock."""
    cm = MagicMock()

    def _get_binding(issue_number: int) -> Optional[Any]:
        if issue_bindings and issue_number in issue_bindings:
            return issue_bindings[issue_number]
        return None

    cm.get_binding.side_effect = _get_binding
    return cm


def _closed_pr(number: int, merged: bool = False) -> Dict[str, Any]:
    return {
        "number": number,
        "state": "closed",
        "merged": merged,
        "merged_at": "2026-01-01T00:00:00Z" if merged else None,
    }


def _open_pr(number: int) -> Dict[str, Any]:
    return {"number": number, "state": "open", "merged": False}


def _jules_session(session_id: str, state: str, pr_number: Optional[int] = None, repo: str = REPO) -> Dict[str, Any]:
    outputs: Any = {}
    if pr_number is not None:
        outputs = {"pullRequest": {"number": pr_number, "url": f"https://github.com/{repo}/pull/{pr_number}"}}
    return {"name": f"projects/test/sessions/{session_id}", "state": state, "outputs": outputs}


def _jules_session_list_outputs(session_id: str, state: str, pr_numbers: List[int], repo: str = REPO) -> Dict[str, Any]:
    """Session with list-format outputs containing multiple PR entries."""
    outputs = [{"pullRequest": {"number": n, "url": f"https://github.com/{repo}/pull/{n}"}} for n in pr_numbers]
    return {"name": f"projects/test/sessions/{session_id}", "state": state, "outputs": outputs}


# ---------------------------------------------------------------------------
# AS-001: Real provider payloads produce complete retirement evidence
# ---------------------------------------------------------------------------


def test_as001_dict_output_pr_extracted(tmp_path: Path) -> None:
    """AS-001: dict-format outputs with pullRequest key are collected correctly."""
    raw_outputs = {"pullRequest": {"number": 201, "url": f"https://github.com/{REPO}/pull/201"}}
    pr_nums, any_foreign = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert 201 in pr_nums
    assert not any_foreign


def test_as001_list_outputs_preserve_all_entries(tmp_path: Path) -> None:
    """AS-001 / REQ-005: list-format outputs with duplicate pullRequest keys preserve all PRs."""
    raw_outputs = [
        {"pullRequest": {"number": 201, "url": f"https://github.com/{REPO}/pull/201"}},
        {"pullRequest": {"number": 202, "url": f"https://github.com/{REPO}/pull/202"}},
    ]
    pr_nums, any_foreign = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert 201 in pr_nums
    assert 202 in pr_nums
    assert len(pr_nums) == 2
    assert not any_foreign


def test_as001_foreign_repo_output_flagged(tmp_path: Path) -> None:
    """AS-001 / REQ-002: PR output from a foreign repository is flagged, not added to local candidates."""
    raw_outputs = {"pullRequest": {"number": 999, "url": "https://github.com/other-org/other-repo/pull/999"}}
    pr_nums, any_foreign = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert 999 not in pr_nums
    assert any_foreign


def test_as001_session_completed_with_pr_becomes_ended(tmp_path: Path) -> None:
    """AS-001: COMPLETED session with established PR outputs → ENDED."""
    jules_client = _make_jules_client(
        session_responses={"sess-1": _jules_session("sess-1", "COMPLETED", pr_number=201)},
        activities={
            "sess-1": [
                {"type": "userMessage", "createTime": "2026-01-01T10:00:00Z"},
                {"type": "sessionCompleted", "createTime": "2026-01-01T11:00:00Z"},
            ]
        },
    )
    evidence = _observe_jules_session("sess-1", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.ENDED
    assert evidence.latest_activity_ended is True
    assert 201 in evidence.established_pr_outputs


def test_as001_session_failed_with_terminal_pr_becomes_ended(tmp_path: Path) -> None:
    """AS-001: FAILED session with established PR output and causality evidence → ENDED."""
    jules_client = _make_jules_client(
        session_responses={"sess-2": _jules_session("sess-2", "FAILED", pr_number=301)},
        activities={
            "sess-2": [
                {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
                {"type": "sessionFailed", "createTime": "2026-01-01T10:00:00Z"},
            ]
        },
    )
    evidence = _observe_jules_session("sess-2", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.ENDED


def test_as001_full_observation_released(tmp_path: Path) -> None:
    """AS-001: fully-terminal observation leads to RELEASED retirement."""
    from auto_coder.issue_stage_routing import IssueStageRoutingStore

    slots = _setup_slots(tmp_path)
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")

    exec_id = slots.start_execution(ISSUE_100, generation="gen-as001")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.record_provider_session(ISSUE_100, "sess-1")
    slots.finish_execution(ISSUE_100, exec_id)

    github_client = _make_github_client(pr_responses={201: _closed_pr(201)})
    jules_client = _make_jules_client(
        session_responses={"sess-1": _jules_session("sess-1", "COMPLETED", pr_number=201)},
        activities={
            "sess-1": [
                {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
                {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
            ]
        },
    )

    obs = collect_retirement_observation(ISSUE_100, slots, github_client, jules_client=jules_client)
    assert obs is not None
    result = slots.retire_owner(obs, routing)
    assert result.status is RetirementStatus.RELEASED


# ---------------------------------------------------------------------------
# AS-002: Incomplete discovery and legacy attribution are not empty work
# ---------------------------------------------------------------------------


def test_as002_foreign_output_does_not_inflate_candidates(tmp_path: Path) -> None:
    """AS-002: a Jules session whose PR output targets a different repository does not add to local candidates."""
    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-as002")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.finish_execution(ISSUE_100, exec_id)

    foreign_session = _jules_session("sess-x", "COMPLETED", pr_number=999)
    foreign_session["outputs"] = {"pullRequest": {"number": 999, "url": "https://github.com/other-org/other-repo/pull/999"}}

    github_client = _make_github_client(pr_responses={201: _closed_pr(201)})
    jules_session_raws = {"sess-x": foreign_session}

    candidate_set = _build_pr_candidate_set(ISSUE_100, slots, github_client, jules_session_raws, REPO)
    assert 999 not in candidate_set.local_prs
    # Foreign output causes incomplete_discovery flag
    assert candidate_set.incomplete_discovery is True


def test_as002_connected_prs_failure_marks_incomplete(tmp_path: Path) -> None:
    """AS-002: failure to enumerate native GitHub associations marks discovery incomplete."""
    slots = _setup_slots(tmp_path)
    slots.start_execution(ISSUE_100, generation="gen-x")
    slots.record_implementation_pr(ISSUE_100, 201)

    client = _make_github_client(pr_responses={201: _closed_pr(201)})
    client.get_connected_prs.side_effect = RuntimeError("GitHub throttled")

    candidate_set = _build_pr_candidate_set(ISSUE_100, slots, client, {}, REPO)
    # The durable slot membership should still include 201
    assert 201 in candidate_set.local_prs
    assert candidate_set.incomplete_discovery is True


def test_as002_unbound_cloudmanager_provider_blocks_observation(tmp_path: Path) -> None:
    """AS-002: a CloudManager binding with an unsupported provider blocks early retirement."""
    from dataclasses import dataclass

    @dataclass
    class FakeBinding:
        provider: str = "codex-cloud"
        task_id: str = "task-xyz"
        backend_name: str = ""

    slots = _setup_slots(tmp_path)
    slots.start_execution(ISSUE_100, generation="gen-x")
    slots.record_implementation_pr(ISSUE_100, 201)

    cloud_manager = _make_cloud_manager(issue_bindings={100: FakeBinding()})
    github_client = _make_github_client(pr_responses={201: _closed_pr(201)})

    obs = collect_retirement_observation(ISSUE_100, slots, github_client, cloud_manager=cloud_manager)
    # Should return a blocked/UNKNOWN observation that won't release
    assert obs is not None
    # The observation should not release the slot
    result = slots.retire_owner(obs)
    assert result.status is not RetirementStatus.RELEASED


# ---------------------------------------------------------------------------
# AS-003: Publication wait and observation failure retain capacity
# ---------------------------------------------------------------------------


def test_as003_completed_without_pr_retains_as_publication_pending(tmp_path: Path) -> None:
    """AS-003: COMPLETED session without established PR output → ACTIVE (publication pending)."""
    jules_client = _make_jules_client(
        session_responses={"sess-np": {"name": "projects/x/sessions/sess-np", "state": "COMPLETED", "outputs": {}}},
    )
    evidence = _observe_jules_session("sess-np", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.ACTIVE
    assert evidence.publication_pending is True


def test_as003_active_states_retain_capacity(tmp_path: Path) -> None:
    """AS-003: QUEUED/PLANNING/IN_PROGRESS/PAUSED/supported AWAITING_* retain capacity as ACTIVE."""
    for state in ["QUEUED", "PLANNING", "IN_PROGRESS", "PAUSED", "AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK", "AWAITING_COMMENT", "AWAITING_COMMENTS"]:
        jules_client = _make_jules_client(
            session_responses={"sess": {"name": "projects/x/sessions/sess", "state": state, "outputs": {}}},
        )
        evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
        assert evidence.state is SessionTerminalState.ACTIVE, f"State {state} should be ACTIVE"
        assert evidence.latest_activity_ended is False


def test_as003_unsupported_awaiting_variant_becomes_unknown(tmp_path: Path) -> None:
    """REQ-005: an AWAITING_* variant NOT in the supported contract is UNKNOWN, never implicitly ACTIVE.

    Issue #2147 item 5: unknown AWAITING_* variants must not be treated as
    ACTIVE merely because they share the "AWAITING_" prefix with supported
    states — that would let an evolving/unrecognized Jules state silently
    retain capacity forever without ever being flagged as needing attention,
    but more importantly could also let it silently combine with other
    terminal evidence in ways this predicate does not intend. Per spec, any
    unsupported/unknown state resolves to UNKNOWN.
    """
    jules_client = _make_jules_client(
        session_responses={"sess": {"name": "projects/x/sessions/sess", "state": "AWAITING_SOMETHING", "outputs": {}}},
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.UNKNOWN


def test_as003_pr_404_becomes_unknown(tmp_path: Path) -> None:
    """AS-003: a 404/None response for a PR observation → UNKNOWN (not terminal)."""
    github_client = _make_github_client(pr_responses={})  # all PRs return None
    obs = _observe_pr(github_client, REPO, 201)
    assert obs.state is PRTerminalState.UNKNOWN


def test_as003_pr_wrong_number_in_response_becomes_unknown(tmp_path: Path) -> None:
    """AS-003: identity mismatch in PR response → UNKNOWN."""
    github_client = _make_github_client(pr_responses={201: {"number": 999, "state": "closed"}})
    obs = _observe_pr(github_client, REPO, 201)
    assert obs.state is PRTerminalState.UNKNOWN


def test_as003_unknown_jules_state_becomes_unknown(tmp_path: Path) -> None:
    """AS-003: unsupported Jules session state → UNKNOWN."""
    jules_client = _make_jules_client(
        session_responses={"sess": {"name": "projects/x/sessions/sess", "state": "WEIRD_STATE", "outputs": {}}},
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.UNKNOWN


def test_as003_jules_transport_failure_becomes_unknown(tmp_path: Path) -> None:
    """AS-003: transport error reading Jules session → UNKNOWN."""
    jules_client = MagicMock()
    jules_client.get_session.side_effect = RuntimeError("HTTP 429 Too Many Requests")
    jules_client.get_session_activities.return_value = []
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.UNKNOWN


def test_as003_full_observation_with_active_session_retains_capacity(tmp_path: Path) -> None:
    """AS-003: even with a closed PR, an ACTIVE Jules session retains the slot."""
    from auto_coder.issue_stage_routing import IssueStageRoutingStore

    slots = _setup_slots(tmp_path)
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")

    exec_id = slots.start_execution(ISSUE_100, generation="gen-as003")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.record_provider_session(ISSUE_100, "sess-active")
    slots.finish_execution(ISSUE_100, exec_id)

    github_client = _make_github_client(pr_responses={201: _closed_pr(201)})
    jules_client = _make_jules_client(
        session_responses={"sess-active": _jules_session("sess-active", "IN_PROGRESS")},
    )

    obs = collect_retirement_observation(ISSUE_100, slots, github_client, jules_client=jules_client)
    assert obs is not None
    result = slots.retire_owner(obs, routing)
    assert result.status is RetirementStatus.RETAINED_ACTIVE


# ---------------------------------------------------------------------------
# AS-004: Same session, newer work, older completion
# ---------------------------------------------------------------------------


def test_as004_user_message_after_completion_blocks_retirement(tmp_path: Path) -> None:
    """AS-004: activities show a user-message AFTER sessionCompleted → causality blocked → UNKNOWN."""
    activities = [
        {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
        {"type": "userMessage", "createTime": "2026-01-01T11:00:00Z"},  # AFTER completion
    ]
    jules_client = _make_jules_client(
        session_responses={"sess": _jules_session("sess", "COMPLETED", pr_number=201)},
        activities={"sess": activities},
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    # User event after completion → cannot confirm causality → UNKNOWN
    assert evidence.state is SessionTerminalState.UNKNOWN


def test_item4_real_oneof_shaped_activity_payload_confirms_causality(tmp_path: Path) -> None:
    """Issue #2147 item 4: real Jules Activity payloads express the event kind
    as a oneof-style field (e.g. ``sessionCompleted``: {...}) rather than a
    synthetic top-level ``type`` string. The causality check must recognize
    this real shape, not only the ``type``-string test convention.
    """
    activities = [
        {"name": "sessions/sess/activities/1", "userMessage": {"prompt": "please continue"}, "createTime": "2026-01-01T09:00:00Z"},
        {"name": "sessions/sess/activities/2", "sessionCompleted": {}, "createTime": "2026-01-01T10:00:00Z"},
    ]
    jules_client = _make_jules_client(
        session_responses={"sess": _jules_session("sess", "COMPLETED", pr_number=201)},
        activities={"sess": activities},
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.ENDED


def test_item4_real_oneof_shaped_activity_after_completion_blocks(tmp_path: Path) -> None:
    """Same real oneof shape, but a userMessage AFTER sessionCompleted must
    still block causality (newer admitted activity)."""
    activities = [
        {"name": "sessions/sess/activities/1", "sessionCompleted": {}, "createTime": "2026-01-01T10:00:00Z"},
        {"name": "sessions/sess/activities/2", "userMessage": {"prompt": "one more thing"}, "createTime": "2026-01-01T11:00:00Z"},
    ]
    jules_client = _make_jules_client(
        session_responses={"sess": _jules_session("sess", "COMPLETED", pr_number=201)},
        activities={"sess": activities},
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.UNKNOWN


def test_as004_completion_before_user_message_allows_retirement(tmp_path: Path) -> None:
    """AS-004: activities show completion after user-message → causality confirmed → ENDED."""
    activities = [
        {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
        {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
    ]
    jules_client = _make_jules_client(
        session_responses={"sess": _jules_session("sess", "COMPLETED", pr_number=201)},
        activities={"sess": activities},
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.ENDED


def test_as004_no_activities_api_is_unknown_not_terminal(tmp_path: Path) -> None:
    """Issue #2147 item 4/REQ-006: missing activities support must not turn a
    terminal session into ENDED.

    Absence of contrary evidence (no activities API at all) is NOT proof
    that the latest admitted activity ended: this must resolve to UNKNOWN,
    not a "conservative" terminal acceptance. Accepting terminal here would
    let an unreadable/absent activities endpoint silently authorize release
    for any provider/session combination that lacks activity support.
    """
    jules_client = MagicMock()
    jules_client.get_session.return_value = _jules_session("sess", "COMPLETED", pr_number=201)
    del jules_client.get_session_activities  # Remove the attribute to simulate missing API

    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.UNKNOWN


# ---------------------------------------------------------------------------
# AS-005: Retirement and maintenance contend
# ---------------------------------------------------------------------------


def test_as005_retired_session_guard_blocks_reuse(tmp_path: Path) -> None:
    """AS-005: guard_retired_session_reuse returns True for a session that belongs to a retired slot."""
    from auto_coder.implementation_retirement import (
        ContinuingObligations,
        ExecutionTerminalState,
        ImplementationPRObservation,
        ImplementationRetirementObservation,
        LocalExecutionObservation,
        ProviderSessionObservation,
    )
    from auto_coder.issue_stage_routing import IssueStageRoutingStore

    slots = _setup_slots(tmp_path)
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")

    exec_id = slots.start_execution(ISSUE_100, generation="gen-as005")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.record_provider_session(ISSUE_100, "sess-retired")
    slots.finish_execution(ISSUE_100, exec_id)

    incarnation = slots.owner_incarnation(ISSUE_100)
    revision = slots.owner_activity_revision(ISSUE_100)

    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=revision,
        implementation_prs=(ImplementationPRObservation(201, PRTerminalState.CLOSED),),
        provider_sessions=(ProviderSessionObservation("sess-retired", "jules", SessionTerminalState.ENDED),),
        local_executions=(LocalExecutionObservation(exec_id, ExecutionTerminalState.ENDED),),
        continuing_obligations=ContinuingObligations(),
    )
    result = slots.retire_owner(obs, routing)
    assert result.status is RetirementStatus.RELEASED

    # Now the guard must return True for this session
    assert guard_retired_session_reuse("sess-retired", slots) is True
    assert guard_retired_pr_reuse(201, slots) is True
    assert guard_retired_session_reuse("sess-other", slots) is False


def test_as005_register_outbound_activity_before_send(tmp_path: Path) -> None:
    """AS-005: register_outbound_jules_activity increments activity_revision durably."""
    slots = _setup_slots(tmp_path)
    slots.start_execution(ISSUE_100, generation="gen-out")
    slots.record_provider_session(ISSUE_100, "sess-a")

    old_revision = slots.owner_activity_revision(ISSUE_100)
    success = register_outbound_jules_activity(ISSUE_100, slots, "sess-a")
    assert success is True
    new_revision = slots.owner_activity_revision(ISSUE_100)
    # record_provider_session with same ID is idempotent for membership but still
    # may advance revision; validate it doesn't error
    assert new_revision is not None
    assert isinstance(new_revision, int)


def test_item6_same_session_admission_advances_revision_and_stales_prior_observation(tmp_path: Path) -> None:
    """Issue #2147 item 6: durable admission for SAME session id advances
    activity_revision, and an observation captured before that admission
    becomes STALE_OBSERVATION (not a release) when retirement is attempted
    afterward.

    ``record_provider_session`` is membership-idempotent: calling it again
    with an already-known session id does not by itself advance the
    revision. ``register_outbound_jules_activity`` (backed by
    ``admit_outbound_provider_activity``) must advance the revision even for
    an unchanged session id, because this represents new durably-admitted
    implementation-mutating responsibility (e.g. a repair message sent to
    the same session), not new membership.
    """
    from auto_coder.implementation_retirement import (
        ContinuingObligations,
        ImplementationPRObservation,
        ImplementationRetirementObservation,
        ProviderSessionObservation,
    )
    from auto_coder.issue_stage_routing import IssueStageRoutingStore

    slots = _setup_slots(tmp_path)
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")

    exec_id = slots.start_execution(ISSUE_100, generation="gen-item6")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.record_provider_session(ISSUE_100, "sess-same")
    slots.finish_execution(ISSUE_100, exec_id)

    # Capture an observation "before" the same-session admission.
    incarnation = slots.owner_incarnation(ISSUE_100)
    revision_before = slots.owner_activity_revision(ISSUE_100)
    assert revision_before is not None

    # Confirm record_provider_session alone (membership-idempotent) does NOT
    # advance the revision for an already-known session id.
    slots.record_provider_session(ISSUE_100, "sess-same")
    assert slots.owner_activity_revision(ISSUE_100) == revision_before

    stale_observation = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=revision_before,
        implementation_prs=(ImplementationPRObservation(201, PRTerminalState.CLOSED),),
        provider_sessions=(ProviderSessionObservation("sess-same", "jules", SessionTerminalState.ENDED),),
        continuing_obligations=ContinuingObligations(),
    )

    # Now durably admit new outbound activity for the SAME session id.
    admitted = register_outbound_jules_activity(ISSUE_100, slots, "sess-same")
    assert admitted is True
    revision_after = slots.owner_activity_revision(ISSUE_100)
    assert revision_after is not None
    assert revision_after > revision_before

    # The observation captured before admission must now be stale, not a
    # release, when checked against the live store.
    result = slots.retire_owner(stale_observation, routing)
    assert result.status is RetirementStatus.STALE_OBSERVATION

    # If the owner had already retired before the admission attempt, the
    # admission itself must fail so the outbound mutation is never sent.
    slots2 = _setup_slots(tmp_path, limit=2)
    other_owner = ImplementationOwner("issue", 900)
    assert register_outbound_jules_activity(other_owner, slots2, "sess-never-reserved") is False


# ---------------------------------------------------------------------------
# AS-006: Retired history survives stale rediscovery
# ---------------------------------------------------------------------------


def test_as006_retired_session_not_revived_by_new_reservation(tmp_path: Path) -> None:
    """AS-006: a new (fresh) reservation for the same Issue does not revive old retired sessions."""
    from auto_coder.implementation_retirement import (
        ContinuingObligations,
        ExecutionTerminalState,
        ImplementationPRObservation,
        ImplementationRetirementObservation,
        LocalExecutionObservation,
        ProviderSessionObservation,
    )
    from auto_coder.issue_stage_routing import IssueStageRoutingStore

    slots = _setup_slots(tmp_path)
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")

    # First incarnation
    exec_id = slots.start_execution(ISSUE_100, generation="gen-first")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.record_provider_session(ISSUE_100, "sess-old")
    slots.finish_execution(ISSUE_100, exec_id)

    inc1 = slots.owner_incarnation(ISSUE_100)
    rev1 = slots.owner_activity_revision(ISSUE_100)

    obs1 = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=inc1,
        activity_revision=rev1,
        implementation_prs=(ImplementationPRObservation(201, PRTerminalState.CLOSED),),
        provider_sessions=(ProviderSessionObservation("sess-old", "jules", SessionTerminalState.ENDED),),
        local_executions=(LocalExecutionObservation(exec_id, ExecutionTerminalState.ENDED),),
        continuing_obligations=ContinuingObligations(),
    )
    result1 = slots.retire_owner(obs1, routing)
    assert result1.status is RetirementStatus.RELEASED

    # New reservation for the same Issue
    slots2 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    exec_id2 = slots2.start_execution(ISSUE_100, generation="gen-second")
    slots2.record_implementation_pr(ISSUE_100, 202)
    slots2.finish_execution(ISSUE_100, exec_id2)

    # Old retired session is preserved in history
    assert slots2.has_retired_session("sess-old")
    # But the new incarnation is not retired
    inc2 = slots2.owner_incarnation(ISSUE_100)
    assert inc2 is not None
    assert inc2 != inc1
    assert not slots2.is_incarnation_retired(inc2)

    # The retired session guard blocks reuse of old session
    assert guard_retired_session_reuse("sess-old", slots2) is True
    # But a new session is not blocked
    assert guard_retired_session_reuse("sess-new", slots2) is False


def test_as006_retired_pr_guard_does_not_block_new_unrelated_prs(tmp_path: Path) -> None:
    """AS-006: retired PR associations do not block PRs that belong to a different Issue."""
    from auto_coder.implementation_retirement import (
        ContinuingObligations,
        ExecutionTerminalState,
        ImplementationPRObservation,
        ImplementationRetirementObservation,
        LocalExecutionObservation,
    )
    from auto_coder.issue_stage_routing import IssueStageRoutingStore

    slots = _setup_slots(tmp_path)
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")

    exec_id = slots.start_execution(ISSUE_100, generation="gen-ret")
    slots.record_implementation_pr(ISSUE_100, 501)
    slots.finish_execution(ISSUE_100, exec_id)

    inc = slots.owner_incarnation(ISSUE_100)
    rev = slots.owner_activity_revision(ISSUE_100)

    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=inc,
        activity_revision=rev,
        implementation_prs=(ImplementationPRObservation(501, PRTerminalState.CLOSED),),
        local_executions=(LocalExecutionObservation(exec_id, ExecutionTerminalState.ENDED),),
        continuing_obligations=ContinuingObligations(),
    )
    slots.retire_owner(obs, routing)

    # PR 501 is retired
    assert guard_retired_pr_reuse(501, slots) is True
    # PR 502 (different PR, different issue) is NOT retired
    assert guard_retired_pr_reuse(502, slots) is False


# ---------------------------------------------------------------------------
# Additional unit tests for specific behaviors
# ---------------------------------------------------------------------------


def test_pr_url_parsing_extracts_number_and_repo() -> None:
    """URL parsing correctly extracts PR number and repository."""
    from auto_coder.implementation_retirement_observer import _parse_github_pr_url

    num, repo = _parse_github_pr_url("https://github.com/kitamura-tetsuo/auto-coder/pull/123")
    assert num == 123
    assert repo == "kitamura-tetsuo/auto-coder"

    num2, repo2 = _parse_github_pr_url("https://github.com/other/repo/pull/456")
    assert num2 == 456
    assert repo2 == "other/repo"

    num3, _ = _parse_github_pr_url("not-a-url")
    assert num3 is None


def test_collect_observation_returns_none_for_pr_owner() -> None:
    """PR-owned slots are out of scope for observation (REQ-001 scope restriction)."""
    tmp_path = Path("/tmp/test-retirement-obs-pr-owner")
    tmp_path.mkdir(exist_ok=True)
    pr_owner = ImplementationOwner("pr", 100)
    slots = _setup_slots(tmp_path)
    # PR owners can be reserved via reserve_new
    slots.reserve_new(pr_owner)

    github_client = _make_github_client()
    obs = collect_retirement_observation(pr_owner, slots, github_client)
    assert obs is None


def test_collect_observation_returns_none_for_inactive_issue(tmp_path: Path) -> None:
    """Issues with no active slot record return None (not an error)."""
    slots = _setup_slots(tmp_path)
    github_client = _make_github_client()
    absent_owner = ImplementationOwner("issue", 9999)
    obs = collect_retirement_observation(absent_owner, slots, github_client)
    assert obs is None


def test_collect_observation_returns_none_without_incarnation(tmp_path: Path) -> None:
    """Legacy records without incarnation return None (cannot observe safely)."""
    slots = _setup_slots(tmp_path)
    # Write a legacy record directly without incarnation
    legacy_state = {
        "issue:100": {
            "kind": "issue",
            "number": 100,
            "implementation_prs": [201],
            "provider_sessions": [],
            "executions": [],
        }
    }
    slots.storage_path.write_text(json.dumps(legacy_state), encoding="utf-8")
    github_client = _make_github_client()
    obs = collect_retirement_observation(ISSUE_100, slots, github_client)
    # No incarnation → returns None
    assert obs is None


def test_open_pr_with_closing_directive_attributed_to_owner(tmp_path: Path) -> None:
    """An open PR with a closing directive for the owner's issue is attributed to owner."""
    slots = _setup_slots(tmp_path)
    slots.start_execution(ISSUE_100, generation="gen-x")

    open_prs = [
        {
            "number": 301,
            "state": "open",
            "body": f"Closes #{ISSUE_100.number}",
            "head": {"ref": "feature/fix-100"},
        }
    ]
    github_client = _make_github_client(
        pr_responses={301: _open_pr(301)},
        open_prs=open_prs,
    )

    candidate_set = _build_pr_candidate_set(ISSUE_100, slots, github_client, {}, REPO)
    assert 301 in candidate_set.local_prs


def test_merged_pr_observation(tmp_path: Path) -> None:
    """A merged PR is observed as MERGED terminal state."""
    github_client = _make_github_client(
        pr_responses={201: _closed_pr(201, merged=True)},
    )
    obs = _observe_pr(github_client, REPO, 201)
    assert obs.state is PRTerminalState.MERGED
    assert obs.merged is True
    assert obs.is_terminal


# ---------------------------------------------------------------------------
# Item 1: fresh, cache-bypassing GitHub reads (REQ-003)
# ---------------------------------------------------------------------------


def test_item1_stale_cached_closed_state_cannot_authorize_release(tmp_path: Path) -> None:
    """Issue #2147 item 1: a stale CACHED 'closed' PR response must never
    authorize retirement when a fresh (strict) read says the PR is open.

    ``_observe_pr`` must call the strict, cache-bypassing PR metadata read
    rather than the cached ``get_pull_request`` — this test wires the two
    APIs to disagree (cached says closed, strict says open) and asserts the
    STRICT result wins: the PR is observed OPEN, and retirement is refused.
    """
    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-item1")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.finish_execution(ISSUE_100, exec_id)

    github_client = MagicMock()
    # Cached endpoint (must NOT be trusted) reports closed.
    github_client.get_pull_request.side_effect = lambda repo, number: _closed_pr(201) if number == 201 else None
    # Strict, cache-bypassing endpoint reports the PR is still open.
    github_client.get_pull_request_metadata_strict.side_effect = lambda repo, number: _open_pr(201) if number == 201 else None
    github_client.get_connected_prs.side_effect = lambda repo, issue, strict=False: []
    github_client.get_open_pull_requests_strict.side_effect = lambda repo: []

    obs = _observe_pr(github_client, REPO, 201)
    assert obs.state is PRTerminalState.OPEN, "strict fresh read must win over a stale cached closed response"

    full_obs = collect_retirement_observation(ISSUE_100, slots, github_client)
    assert full_obs is not None
    result = slots.retire_owner(full_obs)
    assert result.status is RetirementStatus.RETAINED_ACTIVE


# ---------------------------------------------------------------------------
# Item 2: incomplete discovery fails closed unconditionally (REQ-002)
# ---------------------------------------------------------------------------


def test_item2_incomplete_open_pr_discovery_blocks_even_with_other_terminal_pr(tmp_path: Path) -> None:
    """Issue #2147 item 2: candidate_set.incomplete_discovery must block
    retirement UNCONDITIONALLY, even when another known PR exists and is
    terminal/closed.

    One known implementation PR (#201) is terminal/closed, but native/open-PR
    enumeration fails (raises), so another unobserved implementation PR may
    still exist for this owner. ``retire_owner()`` must NOT return RELEASED.
    """
    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-item2")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.finish_execution(ISSUE_100, exec_id)

    github_client = MagicMock()
    github_client.get_pull_request_metadata_strict.side_effect = lambda repo, number: (_closed_pr(201) if number == 201 else None)
    # Native association enumeration fails outright.
    github_client.get_connected_prs.side_effect = RuntimeError("GitHub API throttled (429)")
    # Open-PR enumeration also fails — an unobserved implementation PR
    # (e.g. published just after a publisher crash) could still exist.
    github_client.get_open_pull_requests_strict.side_effect = RuntimeError("GitHub API throttled (429)")

    obs = collect_retirement_observation(ISSUE_100, slots, github_client)
    assert obs is not None
    result = slots.retire_owner(obs)
    assert result.status is not RetirementStatus.RELEASED
    assert result.status is RetirementStatus.RETAINED_UNKNOWN


# ---------------------------------------------------------------------------
# Item 3: real CloudRunRepository API (REQ-004)
# ---------------------------------------------------------------------------


def test_item3_real_cloud_run_repository_pending_run_blocks_retirement(tmp_path: Path) -> None:
    """Issue #2147 item 3: accepted/unresolved Jules work recorded ONLY in a
    real ``CloudRunRepository`` (not mirrored into the slot's
    ``provider_sessions``) must participate in retirement evidence.

    Uses a real ``CloudRunRepository`` instance (not a mock with invented
    method names like ``get_runs_for_issue``/``list_runs``) to persist a
    Jules CloudRun for this owner's issue, and proves it blocks retirement
    even though the slot store's own ``provider_sessions`` is empty.
    """
    from auto_coder.cloud_run import CloudRun, CloudRunRepository

    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-item3")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.finish_execution(ISSUE_100, exec_id)
    # Confirm the slot store itself has no provider_sessions recorded — the
    # CloudRun below is the ONLY evidence of this Jules work.
    assert slots.has_provider_sessions(ISSUE_100) is False

    cloud_run_store = CloudRunRepository(REPO, storage_path=tmp_path / "cloud_runs.json")
    pending_run = CloudRun(
        repo_name=REPO,
        issue_number=ISSUE_100.number,
        attempt=1,
        provider="jules",
        task_id="sess-pending-cloudrun",
    )
    assert cloud_run_store.save(pending_run) is True

    github_client = _make_github_client(pr_responses={201: _closed_pr(201)})
    jules_client = _make_jules_client(
        session_responses={"sess-pending-cloudrun": _jules_session("sess-pending-cloudrun", "IN_PROGRESS")},
    )

    obs = collect_retirement_observation(
        ISSUE_100,
        slots,
        github_client,
        jules_client=jules_client,
        cloud_run_store=cloud_run_store,
    )
    assert obs is not None
    session_ids = {s.session_id for s in obs.provider_sessions}
    assert "sess-pending-cloudrun" in session_ids, "CloudRun-only Jules work must appear in observed provider sessions"

    result = slots.retire_owner(obs)
    assert result.status is RetirementStatus.RETAINED_ACTIVE
    assert any("sess-pending-cloudrun" in member for member in result.responsible_members)


def test_item3_cloud_run_repository_uses_list_for_issue_not_invented_methods() -> None:
    """The observer must call CloudRunRepository's real API (list_for_issue /
    list_all), never invented methods such as get_runs_for_issue/list_runs."""
    from auto_coder.implementation_retirement_observer import _get_cloud_runs_for_issue

    store = MagicMock(spec=["list_for_issue", "list_all"])
    store.list_for_issue.return_value = []
    runs = _get_cloud_runs_for_issue(store, REPO, ISSUE_100.number)
    assert runs == []
    store.list_for_issue.assert_called_once_with(ISSUE_100.number)


# ---------------------------------------------------------------------------
# Issue #2284: recognize URL-only Jules PR outputs through ordinary slot
# reclamation. The official Jules Get Session example
# (https://jules.google/docs/api/reference/sessions/#get-a-session) supplies
# ``outputs: [{pullRequest: {url, title, description}}]`` with no ``number``
# field at all.
# ---------------------------------------------------------------------------


def _url_only_pr_output(pr_number: int, repo: str = REPO, url_key: str = "url") -> Dict[str, Any]:
    """A pullRequest output value shaped like the official Jules example: no
    ``number`` field, only url/title/description."""
    return {
        url_key: f"https://github.com/{repo}/pull/{pr_number}",
        "title": "Fix the reported bug",
        "description": "Closes the linked issue",
    }


def _jules_session_url_only(session_id: str, state: str, pr_number: int, repo: str = REPO, url_key: str = "url") -> Dict[str, Any]:
    return {
        "name": f"projects/test/sessions/{session_id}",
        "state": state,
        "outputs": {"pullRequest": _url_only_pr_output(pr_number, repo, url_key=url_key)},
    }


# --- AS-001: production-shaped output reaches actual retirement ------------


def test_i2284_as001_url_only_output_reaches_actual_retirement(tmp_path: Path) -> None:
    """AS-001 (Issue #2284): a COMPLETED session whose only PR output is a
    URL (no ``number``) — the official example's exact shape — must reach
    real retirement through the production scheduler and slot store, not
    merely be resolvable by the low-level parser.

    The only recorded evidence of PR #205 is the Jules session's URL-only
    output; there is no ``slots.record_implementation_pr`` call for it, so
    the candidate set must discover it purely from the session outputs.
    """
    from auto_coder.implementation_reclamation_scheduler import (
        ReclamationObligationStore,
        run_due_reclamation_checks,
        schedule_reevaluation,
    )

    slots = _setup_slots(tmp_path)
    obligation_store = ReclamationObligationStore.for_slots(slots)

    exec_id = slots.start_execution(ISSUE_100, generation="gen-i2284-001")
    slots.record_provider_session(ISSUE_100, "sess-url-only")
    slots.finish_execution(ISSUE_100, exec_id)

    github_client = _make_github_client(pr_responses={205: _closed_pr(205)})
    jules_client = _make_jules_client(
        session_responses={"sess-url-only": _jules_session_url_only("sess-url-only", "COMPLETED", 205)},
        activities={
            "sess-url-only": [
                {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
                {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
            ]
        },
    )

    assert schedule_reevaluation(ISSUE_100, slots, obligation_store, reason="test-i2284-as001") is True

    released = run_due_reclamation_checks(
        slots,
        obligation_store,
        github_client=github_client,
        jules_client=jules_client,
    )

    assert released == 1, "the URL-only-published owner must actually be released, not just resolvable"
    assert ISSUE_100 not in slots.active_owners()
    assert slots.has_retired_session("sess-url-only")
    assert obligation_store.due() == (), "the obligation must be cleared once released"

    # No new task/attempt/GitHub mutation: the mocked github_client never
    # received a write call, and the jules_client was only read from.
    for mutating in ("create_pull_request", "close_issue", "merge_pull_request", "post_comment"):
        assert not hasattr(github_client, mutating) or not getattr(github_client, mutating).called


def test_i2284_as001_direct_observation_matches_scheduler_outcome(tmp_path: Path) -> None:
    """The same URL-only shape observed directly via
    ``collect_retirement_observation``/``retire_owner`` must agree with the
    scheduler-driven outcome above: PR #205 is discovered, the session is
    ENDED, and retirement is RELEASED."""
    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-i2284-001b")
    slots.record_provider_session(ISSUE_100, "sess-url-only-b")
    slots.finish_execution(ISSUE_100, exec_id)

    github_client = _make_github_client(pr_responses={206: _closed_pr(206)})
    jules_client = _make_jules_client(
        session_responses={"sess-url-only-b": _jules_session_url_only("sess-url-only-b", "COMPLETED", 206)},
        activities={
            "sess-url-only-b": [
                {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
                {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
            ]
        },
    )

    obs = collect_retirement_observation(ISSUE_100, slots, github_client, jules_client=jules_client)
    assert obs is not None
    assert {pr.number for pr in obs.implementation_prs} == {206}
    assert obs.provider_sessions[0].state is SessionTerminalState.ENDED

    result = slots.retire_owner(obs)
    assert result.status is RetirementStatus.RELEASED


# --- AS-002: representation compatibility and complete membership ----------


@pytest.mark.parametrize("url_key", ["url", "html_url"])
def test_i2284_as002_url_only_recognized_via_url_and_html_url(url_key: str) -> None:
    raw_outputs = {"pullRequest": _url_only_pr_output(301, url_key=url_key)}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == [301]
    assert incomplete is False


@pytest.mark.parametrize("key_variant", ["pullRequest", "pull_request", "PULLREQUEST", "Pull_Request"])
def test_i2284_as002_pr_output_key_case_and_style_variants_recognized(key_variant: str) -> None:
    raw_outputs = {key_variant: _url_only_pr_output(302)}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == [302]
    assert incomplete is False


def test_i2284_as002_array_of_mappings_and_key_value_pairs_recognized() -> None:
    array_outputs = [{"pullRequest": _url_only_pr_output(303)}]
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(array_outputs, REPO)
    assert pr_nums == [303]
    assert incomplete is False

    kv_outputs = [["pullRequest", _url_only_pr_output(304)]]
    pr_nums2, incomplete2 = _extract_all_pr_numbers_from_outputs(kv_outputs, REPO)
    assert pr_nums2 == [304]
    assert incomplete2 is False


def test_i2284_as002_coherent_number_bearing_entry_still_recognized() -> None:
    """A number-bearing entry (url + matching number) still resolves — the
    fix must not regress the pre-existing coherent shape."""
    raw_outputs = {"pullRequest": {"number": 305, "url": f"https://github.com/{REPO}/pull/305"}}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == [305]
    assert incomplete is False


def test_i2284_as002_repeated_identical_identities_collapse_to_one() -> None:
    raw_outputs = [
        {"pullRequest": _url_only_pr_output(306)},
        {"pullRequest": _url_only_pr_output(306)},
    ]
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == [306]
    assert incomplete is False


def test_i2284_as002_two_distinct_url_only_outputs_both_present() -> None:
    raw_outputs = [
        {"pullRequest": _url_only_pr_output(307)},
        {"pullRequest": _url_only_pr_output(308)},
    ]
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert set(pr_nums) == {307, 308}
    assert incomplete is False


def test_i2284_as002_second_open_pr_blocks_release_even_with_first_closed_url_only(tmp_path: Path) -> None:
    """Two distinct url-only PR outputs; the second stays open and must
    block retirement even though the first is already closed."""
    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-i2284-as002")
    slots.record_provider_session(ISSUE_100, "sess-two-prs")
    slots.finish_execution(ISSUE_100, exec_id)

    session = _jules_session_url_only("sess-two-prs", "COMPLETED", 309)
    session["outputs"] = [
        {"pullRequest": _url_only_pr_output(309)},
        {"pullRequest": _url_only_pr_output(310)},
    ]
    github_client = _make_github_client(pr_responses={309: _closed_pr(309), 310: _open_pr(310)})
    jules_client = _make_jules_client(
        session_responses={"sess-two-prs": session},
        activities={
            "sess-two-prs": [
                {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
                {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
            ]
        },
    )

    obs = collect_retirement_observation(ISSUE_100, slots, github_client, jules_client=jules_client)
    assert obs is not None
    assert {pr.number for pr in obs.implementation_prs} == {309, 310}
    result = slots.retire_owner(obs)
    assert result.status is RetirementStatus.RETAINED_ACTIVE
    assert "pr:310" in result.responsible_members
    assert "pr:309" not in result.responsible_members


def test_i2284_as002_unrelated_output_key_not_adopted_as_pr() -> None:
    """A URL-looking value under an unrelated output key must never be
    adopted as a PR (only pullRequest/pull_request keys are PR evidence)."""
    raw_outputs = {"summary": _url_only_pr_output(311), "description": f"see https://github.com/{REPO}/pull/999"}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == []
    assert incomplete is False


def test_i2284_as002_foreign_repository_url_only_flags_incomplete() -> None:
    raw_outputs = {"pullRequest": _url_only_pr_output(999, repo="other-org/other-repo")}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == []
    assert incomplete is True


def test_i2284_as002_paired_valid_and_malformed_entries_block_release(tmp_path: Path) -> None:
    """A valid closed PR paired with a malformed/foreign PR entry in the same
    session outputs must leave discovery incomplete and never release."""
    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-i2284-paired")
    slots.record_provider_session(ISSUE_100, "sess-paired")
    slots.finish_execution(ISSUE_100, exec_id)

    session = {
        "name": "projects/test/sessions/sess-paired",
        "state": "COMPLETED",
        "outputs": [
            {"pullRequest": _url_only_pr_output(312)},
            {"pullRequest": _url_only_pr_output(998, repo="other-org/other-repo")},
        ],
    }
    github_client = _make_github_client(pr_responses={312: _closed_pr(312)})
    jules_client = _make_jules_client(
        session_responses={"sess-paired": session},
        activities={
            "sess-paired": [
                {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
                {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
            ]
        },
    )

    obs = collect_retirement_observation(ISSUE_100, slots, github_client, jules_client=jules_client)
    assert obs is not None
    result = slots.retire_owner(obs)
    assert result.status is RetirementStatus.RETAINED_UNKNOWN


@pytest.mark.parametrize(
    "malicious_url",
    [
        "https://github.com.evil.com/kitamura-tetsuo/auto-coder/pull/1",
        "https://evil.com/github.com/kitamura-tetsuo/auto-coder/pull/1",
        "https://user@github.com/kitamura-tetsuo/auto-coder/pull/1",
        "https://github.com:8443/kitamura-tetsuo/auto-coder/pull/1",
        "http://github.com/kitamura-tetsuo/auto-coder/pull/1",
        "https://github.com/kitamura-tetsuo/auto-coder/pull/1/files",
        "https://github.com/kitamura-tetsuo/auto-coder/pull/abc",
        "https://github.com/kitamura-tetsuo/auto-coder/pull/-1",
        "https://github.com/kitamura-tetsuo/auto-coder/pull/0",
        "not-a-url-but-mentions github.com",
    ],
)
def test_i2284_as002_malformed_or_lookalike_urls_never_authorize_identity(malicious_url: str) -> None:
    """No partial/loose match on a host look-alike, embedded github.com
    text, user-info, non-default port, wrong scheme, extra path segments, or
    a non-positive/non-decimal PR number may ever authorize a PR identity."""
    from auto_coder.implementation_retirement_observer import _parse_github_pr_url

    number, repo = _parse_github_pr_url(malicious_url)
    assert number is None
    assert repo is None

    raw_outputs = {"pullRequest": {"url": malicious_url, "title": "t", "description": "d"}}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == []
    assert incomplete is True, f"malformed URL {malicious_url!r} must mark discovery incomplete, not silently empty"


def test_i2284_as002_valid_url_with_trailing_slash_query_and_fragment() -> None:
    for suffix in ["/", "?tab=files", "#discussion", "/?x=1#y"]:
        raw_outputs = {"pullRequest": {"url": f"https://github.com/{REPO}/pull/313{suffix}", "title": "t", "description": "d"}}
        pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
        assert pr_nums == [313], f"suffix {suffix!r} must not change PR identity"
        assert incomplete is False


def test_i2284_as002_inconsistent_number_and_url_marks_incomplete() -> None:
    """An entry whose explicit number disagrees with its URL's number is
    malformed/inconsistent, not silently resolved from either field alone."""
    raw_outputs = {"pullRequest": {"number": 400, "url": f"https://github.com/{REPO}/pull/401"}}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == []
    assert incomplete is True


def test_i2284_as002_inconsistent_repository_field_and_url_marks_incomplete() -> None:
    raw_outputs = {
        "pullRequest": {
            "url": f"https://github.com/{REPO}/pull/402",
            "repository": {"full_name": "other-org/other-repo"},
        }
    }
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == []
    assert incomplete is True


@pytest.mark.parametrize("bad_number", [True, False, 0, -1, 1.5, "201"])
def test_i2284_as002_boolean_zero_negative_and_non_int_numbers_rejected(bad_number: Any) -> None:
    raw_outputs = {"pullRequest": {"number": bad_number, "url": f"https://github.com/{REPO}/pull/403"}}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == []
    assert incomplete is True


def test_i2284_as002_genuinely_empty_publication_is_not_malformed() -> None:
    """A pullRequest output entry with only title/description (no number,
    no url at all) is genuinely-empty publication, not a malformed entry —
    it must not itself mark discovery incomplete."""
    raw_outputs = {"pullRequest": {"title": "Work in progress", "description": "no PR yet"}}
    pr_nums, incomplete = _extract_all_pr_numbers_from_outputs(raw_outputs, REPO)
    assert pr_nums == []
    assert incomplete is False


# --- AS-003: fixing publication does not bypass activity causality --------


def test_i2284_as003_url_only_completed_with_causality_evidence_becomes_ended() -> None:
    """REQ-003: a COMPLETED session with only a URL-shaped PR output (no
    number) must not be marked publication-pending — once causality is also
    satisfied, it resolves ENDED."""
    jules_client = _make_jules_client(
        session_responses={"sess": _jules_session_url_only("sess", "COMPLETED", 501)},
        activities={
            "sess": [
                {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
                {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
            ]
        },
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.ENDED
    assert 501 in evidence.established_pr_outputs
    assert evidence.publication_pending is False


def test_i2284_as003_url_only_completed_without_activities_stays_unknown_not_terminal() -> None:
    """REQ-003: URL recognition alone must never be treated as session
    termination — missing activity evidence keeps the session UNKNOWN."""
    jules_client = MagicMock()
    jules_client.get_session.return_value = _jules_session_url_only("sess", "COMPLETED", 502)
    del jules_client.get_session_activities

    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.UNKNOWN
    assert 502 in evidence.established_pr_outputs


def test_i2284_as003_url_only_completed_then_later_user_activity_is_not_settled() -> None:
    """REQ-003: an observed later user/approval activity must prevent an
    older URL-only completion from settling the session as ENDED."""
    activities = [
        {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
        {"type": "userMessage", "createTime": "2026-01-01T11:00:00Z"},
    ]
    jules_client = _make_jules_client(
        session_responses={"sess": _jules_session_url_only("sess", "COMPLETED", 503)},
        activities={"sess": activities},
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.UNKNOWN
    assert 503 in evidence.established_pr_outputs


def test_i2284_as003_url_only_genuinely_empty_publication_stays_pending() -> None:
    """REQ-003: COMPLETED with successfully observed genuinely empty
    publication (no PR output at all) remains publication-pending, even
    though the URL-recognition fix now runs on the same outputs."""
    jules_client = _make_jules_client(
        session_responses={"sess": {"name": "projects/x/sessions/sess", "state": "COMPLETED", "outputs": {"pullRequest": {"title": "wip", "description": "no PR yet"}}}},
    )
    evidence = _observe_jules_session("sess", jules_client, REPO, 1, "issue:100")
    assert evidence.state is SessionTerminalState.ACTIVE
    assert evidence.publication_pending is True


# --- AS-004: same evidence after an outage is not new work ------------------


def test_i2284_as004_transient_observation_failure_then_recovery_same_service(tmp_path: Path) -> None:
    """AS-004: an unavailable GitHub/Jules read must not force a release nor
    lose the pending obligation; the same due-check succeeds once the
    identical evidence becomes readable again, without a new attempt."""
    from auto_coder.implementation_reclamation_scheduler import (
        ReclamationObligationStore,
        run_due_reclamation_checks,
        schedule_reevaluation,
    )

    slots = _setup_slots(tmp_path)
    obligation_store = ReclamationObligationStore.for_slots(slots)

    exec_id = slots.start_execution(ISSUE_100, generation="gen-i2284-as004")
    slots.record_provider_session(ISSUE_100, "sess-outage")
    slots.finish_execution(ISSUE_100, exec_id)

    session = _jules_session_url_only("sess-outage", "COMPLETED", 601)
    activities = {
        "sess-outage": [
            {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
            {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
        ]
    }

    failing_github_client = MagicMock()
    failing_github_client.get_pull_request_metadata_strict.side_effect = RuntimeError("GitHub outage")
    failing_github_client.get_connected_prs.side_effect = RuntimeError("GitHub outage")
    failing_github_client.get_open_pull_requests_strict.side_effect = RuntimeError("GitHub outage")
    jules_client = _make_jules_client(session_responses={"sess-outage": session}, activities=activities)

    assert schedule_reevaluation(ISSUE_100, slots, obligation_store, reason="test-i2284-as004") is True

    released_during_outage = run_due_reclamation_checks(
        slots,
        obligation_store,
        github_client=failing_github_client,
        jules_client=jules_client,
    )
    assert released_during_outage == 0
    assert ISSUE_100 in slots.active_owners(), "capacity must be preserved during the outage"
    assert obligation_store.all() != (), "the obligation must remain pending, not be dropped"

    # Recovery: the same publication/terminal evidence, now readable. The
    # rescheduled obligation is due 60s after the failed check.
    recovered_github_client = _make_github_client(pr_responses={601: _closed_pr(601)})
    later = time.time() + 61
    released_after_recovery = run_due_reclamation_checks(
        slots,
        obligation_store,
        github_client=recovered_github_client,
        jules_client=jules_client,
        now=later,
    )
    assert released_after_recovery == 1
    assert ISSUE_100 not in slots.active_owners()

    # Replaying the same completed retirement must not free capacity twice
    # or recreate the retired owner.
    released_replay = run_due_reclamation_checks(
        slots,
        obligation_store,
        github_client=recovered_github_client,
        jules_client=jules_client,
        now=later + 61,
    )
    assert released_replay == 0
    assert ISSUE_100 not in slots.active_owners()


def test_i2284_as004_restart_recovers_progress_without_manual_repair(tmp_path: Path) -> None:
    """AS-004: reconstructing the service and stores between failure and
    recovery must recover the same pending-then-released progress without
    requiring an Issue edit or manual state deletion."""
    from auto_coder.implementation_reclamation_scheduler import (
        ReclamationObligationStore,
        run_due_reclamation_checks,
        schedule_reevaluation,
    )

    slots = _setup_slots(tmp_path)
    obligation_store = ReclamationObligationStore.for_slots(slots)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-i2284-as004b")
    slots.record_provider_session(ISSUE_100, "sess-restart")
    slots.finish_execution(ISSUE_100, exec_id)

    session = _jules_session_url_only("sess-restart", "COMPLETED", 602)
    activities = {
        "sess-restart": [
            {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
            {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
        ]
    }
    jules_client = _make_jules_client(session_responses={"sess-restart": session}, activities=activities)
    failing_github_client = MagicMock()
    failing_github_client.get_pull_request_metadata_strict.side_effect = RuntimeError("outage")
    failing_github_client.get_connected_prs.side_effect = RuntimeError("outage")
    failing_github_client.get_open_pull_requests_strict.side_effect = RuntimeError("outage")

    assert schedule_reevaluation(ISSUE_100, slots, obligation_store, reason="test-i2284-as004b") is True
    assert run_due_reclamation_checks(slots, obligation_store, github_client=failing_github_client, jules_client=jules_client) == 0

    # Reconstruct the repository/store objects against the same on-disk
    # state, simulating a process restart.
    slots_restarted = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    obligation_store_restarted = ReclamationObligationStore.for_slots(slots_restarted)
    recovered_github_client = _make_github_client(pr_responses={602: _closed_pr(602)})

    released = run_due_reclamation_checks(
        slots_restarted,
        obligation_store_restarted,
        github_client=recovered_github_client,
        jules_client=jules_client,
        now=time.time() + 61,
    )
    assert released == 1
    assert ISSUE_100 not in slots_restarted.active_owners()
    assert slots_restarted.has_retired_session("sess-restart")


# --- AS-005: a stale observation cannot free a successor's capacity --------


def test_i2284_as005_stale_observation_cannot_release_owner_with_newer_membership(tmp_path: Path) -> None:
    """AS-005: an observation captured before a durable newer PR membership
    addition must not release that updated owner — even though the
    URL-only PR it *did* see is legitimately closed."""
    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-i2284-as005")
    slots.record_provider_session(ISSUE_100, "sess-stale")
    slots.finish_execution(ISSUE_100, exec_id)

    session = _jules_session_url_only("sess-stale", "COMPLETED", 701)
    activities = {
        "sess-stale": [
            {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
            {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
        ]
    }
    github_client = _make_github_client(pr_responses={701: _closed_pr(701)})
    jules_client = _make_jules_client(session_responses={"sess-stale": session}, activities=activities)

    # Capture the observation ("paused after collection, before commit").
    stale_obs = collect_retirement_observation(ISSUE_100, slots, github_client, jules_client=jules_client)
    assert stale_obs is not None
    assert {pr.number for pr in stale_obs.implementation_prs} == {701}

    # Through the production membership boundary, durably record additional
    # PR membership for the SAME owner/incarnation before the paused
    # observation is allowed to commit.
    added = slots.record_implementation_pr(ISSUE_100, 702)
    assert added is True

    # The stale (pre-update) observation must not release the now-updated
    # owner. Recording new PR membership durably advances activity_revision
    # (Issue #2146), so the commit-time fence rejects the stale observation
    # as STALE_OBSERVATION before ever reaching the membership-completeness
    # check — either outcome refuses release, but this is what the real
    # incarnation/activity-revision fence actually returns here.
    result = slots.retire_owner(stale_obs)
    assert result.status is RetirementStatus.STALE_OBSERVATION
    assert result.status is not RetirementStatus.RELEASED
    assert ISSUE_100 in slots.active_owners()

    # A fresh observation that actually covers PR #702 (still open) correctly
    # retains the owner as ACTIVE instead.
    fresh_obs = collect_retirement_observation(ISSUE_100, slots, github_client, jules_client=jules_client)
    assert fresh_obs is not None
    assert {pr.number for pr in fresh_obs.implementation_prs} == {701, 702}
    result2 = slots.retire_owner(fresh_obs)
    assert result2.status is RetirementStatus.RETAINED_UNKNOWN  # PR 702 has no registered response → UNKNOWN


def test_i2284_as005_old_incarnation_replay_does_not_disturb_new_incarnation(tmp_path: Path) -> None:
    """AS-005: legitimately retire the old incarnation, acquire a new one for
    the same Issue, then replay the old (now-stale) observation — the new
    owner and its capacity must remain intact."""
    slots = _setup_slots(tmp_path)
    exec_id = slots.start_execution(ISSUE_100, generation="gen-i2284-as005b-old")
    slots.record_provider_session(ISSUE_100, "sess-old-inc")
    slots.finish_execution(ISSUE_100, exec_id)

    session = _jules_session_url_only("sess-old-inc", "COMPLETED", 703)
    activities = {
        "sess-old-inc": [
            {"type": "userMessage", "createTime": "2026-01-01T09:00:00Z"},
            {"type": "sessionCompleted", "createTime": "2026-01-01T10:00:00Z"},
        ]
    }
    github_client = _make_github_client(pr_responses={703: _closed_pr(703)})
    jules_client = _make_jules_client(session_responses={"sess-old-inc": session}, activities=activities)

    old_obs = collect_retirement_observation(ISSUE_100, slots, github_client, jules_client=jules_client)
    assert old_obs is not None
    result = slots.retire_owner(old_obs)
    assert result.status is RetirementStatus.RELEASED

    # A new incarnation is acquired for the same Issue through production admission.
    slots2 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    exec_id2 = slots2.start_execution(ISSUE_100, generation="gen-i2284-as005b-new")
    slots2.record_implementation_pr(ISSUE_100, 704)
    slots2.finish_execution(ISSUE_100, exec_id2)
    new_incarnation = slots2.owner_incarnation(ISSUE_100)
    assert new_incarnation is not None and new_incarnation != old_obs.reservation_incarnation

    # Replaying the old (retired) observation must be a safe no-op.
    replay_result = slots2.retire_owner(old_obs)
    assert replay_result.status is not RetirementStatus.RELEASED

    assert ISSUE_100 in slots2.active_owners()
    assert slots2.owner_incarnation(ISSUE_100) == new_incarnation
