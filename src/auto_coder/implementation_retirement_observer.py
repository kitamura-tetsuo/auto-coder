"""Evidence collection for terminal PR-backed implementation slot retirement.

Derives authoritative ImplementationRetirementObservation from fresh GitHub PR
metadata, individual Jules session reads, CloudManager/CloudRun bindings, and
local execution liveness checks (Issue #2147).

Retirement scope: ordinary Issue-owned reservations with Jules or local work.
This module is strictly read-only — it does not close/reopen/merge Issues or
PRs, send Jules messages, approve plans, or perform any write operations.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Tuple

from .implementation_retirement import (
    ContinuingObligations,
    ExecutionTerminalState,
    ImplementationPRObservation,
    ImplementationRetirementObservation,
    LocalExecutionObservation,
    ProviderSessionObservation,
    PRTerminalState,
    SessionTerminalState,
)
from .implementation_slots import (
    ImplementationOwner,
    ImplementationSlotRepository,
    ImplementationSlotUnavailable,
)
from .issue_context import (
    extract_lifecycle_branch_issue_number,
    extract_lifecycle_directive_issue_references,
)
from .logger_config import get_logger

if TYPE_CHECKING:
    from .cloud_manager import CloudManager
    from .cloud_run import CloudRunRepository

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Jules active states — REQ-005: these retain capacity as ACTIVE.
# ---------------------------------------------------------------------------
_JULES_ACTIVE_STATES = frozenset(
    {
        "QUEUED",
        "PLANNING",
        "IN_PROGRESS",
        "PAUSED",
        "AWAITING_PLAN_APPROVAL",
        "AWAITING_USER_FEEDBACK",
        "AWAITING_COMMENT",
        "AWAITING_COMMENTS",
    }
)

# ---------------------------------------------------------------------------
# Jules terminal states — COMPLETED and FAILED are *candidates* only; they
# must also have established publication (REQ-005).
# ---------------------------------------------------------------------------
_JULES_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED"})


# ---------------------------------------------------------------------------
# PR output extraction — REQ-005: preserve EVERY pullRequest/pull_request
# output in both mapping and list payloads; never flatten away an additional
# output entry.
# ---------------------------------------------------------------------------


def _extract_all_pr_numbers_from_outputs(raw_outputs: Any, expected_repo: str) -> Tuple[List[int], bool]:
    """Return all local PR numbers found in a Jules session's raw outputs.

    Reads the full raw payload directly (not the flattened single-PR view from
    ``normalize_session_outputs``) to preserve every PR output entry.

    Returns (pr_numbers, any_foreign) where any_foreign is True when at least
    one output references a different repository.
    """
    pr_numbers: List[int] = []
    any_foreign = False

    if isinstance(raw_outputs, dict):
        entries: List[Any] = list(raw_outputs.values())
    elif isinstance(raw_outputs, list):
        # List of single-entry dicts or [key, value] pairs
        entries = []
        for item in raw_outputs:
            if isinstance(item, dict):
                entries.extend(item.values())
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                entries.append(item[1])
    else:
        return pr_numbers, any_foreign

    for entry in entries:
        number, is_foreign = _resolve_pr_output_entry(entry, expected_repo)
        if is_foreign:
            any_foreign = True
        if number is not None:
            pr_numbers.append(number)

    return pr_numbers, any_foreign


def _resolve_pr_output_entry(entry: Any, expected_repo: str) -> Tuple[Optional[int], bool]:
    """Resolve one output entry to (pr_number, is_foreign).

    Returns (None, False) for malformed/unresolvable entries.
    Returns (None, True) for entries that resolve to a different repository.
    Returns (number, False) for local PRs.
    """
    if isinstance(entry, dict):
        number = entry.get("number")
        # Validate repository identity
        repo_name = _extract_repo_from_pr_dict(entry)
        if repo_name is not None:
            if repo_name.lower() != expected_repo.lower():
                return None, True
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            return None, False
        return number, False

    if isinstance(entry, str) and "github.com" in entry:
        number, repo_name = _parse_github_pr_url(entry)
        if repo_name is not None and repo_name.lower() != expected_repo.lower():
            return None, True
        return number, False

    return None, False


def _extract_repo_from_pr_dict(pr_dict: Dict[str, Any]) -> Optional[str]:
    """Extract repository full_name from a PR output dict, if present."""
    repo = pr_dict.get("repository") or pr_dict.get("base", {}).get("repo")
    if isinstance(repo, dict):
        full_name = repo.get("full_name") or repo.get("name")
        if isinstance(full_name, str) and full_name:
            return full_name
    url = pr_dict.get("url") or pr_dict.get("html_url")
    if isinstance(url, str) and "github.com" in url:
        _, repo_name = _parse_github_pr_url(url)
        return repo_name
    return None


def _parse_github_pr_url(url: str) -> Tuple[Optional[int], Optional[str]]:
    """Parse a GitHub PR URL into (pr_number, repo_full_name)."""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if m:
        try:
            return int(m.group(2)), m.group(1)
        except ValueError:
            pass
    return None, None


# ---------------------------------------------------------------------------
# PR candidate set construction — REQ-002
# ---------------------------------------------------------------------------


@dataclass
class _PRCandidateSet:
    """Accumulated PR candidate set with attribution tracking."""

    local_prs: List[int] = field(default_factory=list)
    # PR numbers whose attribution is contradictory → must become UNKNOWN
    contradicted_prs: List[int] = field(default_factory=list)
    # PR numbers with incomplete required enumeration
    incomplete_discovery: bool = False


def _build_pr_candidate_set(
    owner: ImplementationOwner,
    slots: ImplementationSlotRepository,
    github_client: Any,
    jules_session_raws: Dict[str, Dict[str, Any]],
    expected_repo: str,
) -> _PRCandidateSet:
    """Collect all PR candidates that must participate in the terminality check.

    Sources (REQ-002):
    1. Durable implementation PR membership from the slot store.
    2. Complete native GitHub Development/closing associations to the source Issue.
    3. Every PR output of each positively bound Jules session.
    4. Current open PR discovery with restricted attribution.
    """
    result = _PRCandidateSet()
    seen: set[int] = set()

    def _add(pr_num: int) -> None:
        if pr_num not in seen:
            seen.add(pr_num)
            result.local_prs.append(pr_num)

    # 1. Durable slot membership
    with slots._state_lock():
        record = slots._read().get(owner.key, {})
    stored_prs_raw = record.get("implementation_prs", [])
    if isinstance(stored_prs_raw, list):
        for n in stored_prs_raw:
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                _add(n)

    # 2. Native GitHub Development/closing associations
    try:
        connected = _safe_get_connected_prs(github_client, expected_repo, owner.number)
        for pr_num in connected:
            _add(pr_num)
    except Exception as exc:
        logger.warning(f"Cannot enumerate native GitHub associations for {owner.key}: {exc}; " "marking discovery incomplete")
        result.incomplete_discovery = True

    # 3. Every PR output from each bound Jules session
    for session_id, raw_session in jules_session_raws.items():
        raw_outputs = raw_session.get("outputs", {})
        pr_nums, any_foreign = _extract_all_pr_numbers_from_outputs(raw_outputs, expected_repo)
        for n in pr_nums:
            _add(n)
        if any_foreign:
            logger.debug(f"Session {session_id} has foreign-repository PR output; " "adding it to contradicted set")
            # Do not add the foreign number to local_prs; mark investigation incomplete
            result.incomplete_discovery = True

    # 4. Open PR discovery with restricted attribution
    try:
        open_prs = _safe_get_open_pull_requests(github_client, expected_repo)
        for pr_data in open_prs:
            pr_num_raw = pr_data.get("number")
            if isinstance(pr_num_raw, bool) or not isinstance(pr_num_raw, int) or pr_num_raw <= 0:
                continue
            if pr_num_raw in seen:
                continue
            attribution = _classify_open_pr_attribution(pr_num_raw, pr_data, owner, expected_repo, github_client)
            if attribution == "owned":
                _add(pr_num_raw)
            elif attribution == "contradicted":
                if pr_num_raw not in result.contradicted_prs:
                    result.contradicted_prs.append(pr_num_raw)
    except _DiscoveryIncomplete as exc:
        logger.warning(f"Open PR discovery incomplete for {owner.key}: {exc}; " "marking incomplete")
        result.incomplete_discovery = True
    except Exception as exc:
        logger.warning(f"Open PR discovery failed for {owner.key}: {exc}; marking incomplete")
        result.incomplete_discovery = True

    return result


class _DiscoveryIncomplete(RuntimeError):
    """Raised when open-PR discovery cannot guarantee complete enumeration."""


def _safe_get_connected_prs(github_client: Any, repo_name: str, issue_number: int) -> List[int]:
    """Return native GitHub Development/closing connections for issue_number.

    REQ-003: retirement evidence must never be authorized from a cached
    response, so this always requests a strict, cache-bypassing read when the
    client supports it. A client that lacks the strict parameter entirely
    (no ``get_connected_prs`` at all) contributes nothing — the caller treats
    an exception here as incomplete discovery, but an absent API is not by
    itself an error, so this returns an empty list and callers rely on other
    candidate sources.
    """
    getter = getattr(github_client, "get_connected_prs", None)
    if not callable(getter):
        return []
    try:
        result = getter(repo_name, issue_number, strict=True)
    except TypeError:
        # Client does not support the strict keyword at all — this is not a
        # fresh read, so treat it as a failed discovery rather than silently
        # falling back to a cached/non-strict result (REQ-003).
        raise
    if not isinstance(result, (list, tuple, set, frozenset)):
        raise _DiscoveryIncomplete("get_connected_prs(strict=True) returned non-collection")
    return [n for n in result if isinstance(n, int) and not isinstance(n, bool) and n > 0]


def _safe_get_open_pull_requests(github_client: Any, repo_name: str) -> List[Dict[str, Any]]:
    """Return open PRs via a strict, cache-bypassing complete enumeration (REQ-003).

    Prefers ``get_open_pull_requests_strict`` (fresh, complete pagination).
    Falls back to ``get_open_pull_requests`` only when the strict API does not
    exist on this client at all; any read failure, throttling, malformed
    result, or incomplete pagination raises ``_DiscoveryIncomplete`` so the
    caller marks discovery incomplete rather than treating an empty/partial
    result as a complete candidate set.
    """
    strict_getter = getattr(github_client, "get_open_pull_requests_strict", None)
    if callable(strict_getter):
        try:
            result = strict_getter(repo_name)
        except Exception as exc:
            raise _DiscoveryIncomplete(str(exc)) from exc
        if not isinstance(result, (list, tuple)):
            raise _DiscoveryIncomplete("get_open_pull_requests_strict returned non-list")
        return list(result)

    getter = getattr(github_client, "get_open_pull_requests", None)
    if not callable(getter):
        return []
    try:
        result = getter(repo_name)
    except Exception as exc:
        raise _DiscoveryIncomplete(str(exc)) from exc
    if not isinstance(result, (list, tuple)):
        raise _DiscoveryIncomplete("get_open_pull_requests returned non-list")
    return list(result)


def _classify_open_pr_attribution(
    pr_num: int,
    pr_data: Dict[str, Any],
    owner: ImplementationOwner,
    expected_repo: str,
    github_client: Any,
) -> str:
    """Return 'owned', 'contradicted', or 'unrelated' for one open PR.

    Only an explicit closing directive for the exact local Issue, an
    Issue-bearing head-branch marker, a native association, or an established
    durable provider association may attribute ownership (REQ-002).
    """
    body = pr_data.get("body") or ""
    head = pr_data.get("head") or {}
    branch = head.get("ref") if isinstance(head, dict) else ""

    owners_found: set[int] = set()

    # Closing directives
    for qualifier, candidate in extract_lifecycle_directive_issue_references(str(body)):
        if qualifier is not None:
            try:
                c_owner, c_name = qualifier.split("/")
                e_owner, e_name = expected_repo.split("/")
                if c_owner.lower() != e_owner.lower() or c_name.lower() != e_name.lower():
                    continue
            except ValueError:
                continue
        if candidate == owner.number:
            owners_found.add(candidate)
        elif candidate != pr_num:
            # Confirmed Issue from this repo but a different owner
            try:
                issue = _safe_get_issue(github_client, expected_repo, candidate)
                if issue is not None and not _is_pull_request(issue):
                    owners_found.add(candidate)
            except Exception:
                pass

    # Branch marker
    branch_candidate = extract_lifecycle_branch_issue_number(str(branch or ""))
    if branch_candidate is not None and branch_candidate != pr_num:
        if branch_candidate == owner.number:
            owners_found.add(branch_candidate)

    # Native association
    native_connected = _safe_get_connected_prs(github_client, expected_repo, owner.number)
    if pr_num in native_connected:
        owners_found.add(owner.number)

    if not owners_found:
        return "unrelated"

    # Contradictory: multiple distinct owners found
    distinct = {n for n in owners_found if n != owner.number}
    if distinct and owner.number in owners_found:
        return "contradicted"

    if owner.number in owners_found and not distinct:
        return "owned"

    # Only foreign/contradictory owners found
    return "unrelated"


def _safe_get_issue(github_client: Any, repo_name: str, issue_number: int) -> Optional[Dict[str, Any]]:
    """Get an issue, returning None for definitive absence."""
    getter = getattr(github_client, "get_issue", None)
    if not callable(getter):
        return None
    try:
        result = getter(repo_name, issue_number)
        if isinstance(result, dict):
            return result
        return None
    except Exception:
        return None


def _is_pull_request(issue_data: Dict[str, Any]) -> bool:
    """Return True if the object is actually a pull request."""
    return issue_data.get("pull_request") is not None


# ---------------------------------------------------------------------------
# Individual PR observation — REQ-003
# ---------------------------------------------------------------------------


def _observe_pr(github_client: Any, repo_name: str, pr_number: int) -> ImplementationPRObservation:
    """Return a fresh, identity-checked observation for one PR (REQ-003).

    Missing/404/denied/throttled/malformed/wrong-repo/wrong-number → UNKNOWN.
    Never uses cached responses, listing omissions, or Issue state.

    Uses ``get_pull_request_metadata_strict`` — a cache-bypassing direct read
    — rather than ``get_pull_request``, which is backed by a reusable cache
    (see ``util/gh_cache.py``) and could return a stale ``closed`` snapshot
    after the PR reopened. A client that lacks the strict API entirely falls
    back to ``get_pull_request`` only so this function still degrades
    gracefully for legacy/test doubles that never exercise this specific
    freshness guarantee; production ``GitHubClient`` always has the strict
    method.
    """
    try:
        strict_getter = getattr(github_client, "get_pull_request_metadata_strict", None)
        if callable(strict_getter):
            try:
                pr_data = strict_getter(repo_name, pr_number)
            except Exception as exc:
                logger.warning(f"Strict transport/read failure for PR #{pr_number}: {exc} → UNKNOWN")
                return ImplementationPRObservation(pr_number, PRTerminalState.UNKNOWN)
        else:
            getter = getattr(github_client, "get_pull_request", None)
            if not callable(getter):
                logger.warning(f"github_client has no PR read method; PR #{pr_number} → UNKNOWN")
                return ImplementationPRObservation(pr_number, PRTerminalState.UNKNOWN)
            pr_data = getter(repo_name, pr_number)
    except Exception as exc:
        logger.warning(f"Transport/read failure for PR #{pr_number}: {exc} → UNKNOWN")
        return ImplementationPRObservation(pr_number, PRTerminalState.UNKNOWN)

    if pr_data is None:
        # get_pull_request returns None for 404 / absent
        return ImplementationPRObservation(pr_number, PRTerminalState.UNKNOWN)

    if not isinstance(pr_data, dict):
        logger.warning(f"Malformed PR #{pr_number} response (not dict) → UNKNOWN")
        return ImplementationPRObservation(pr_number, PRTerminalState.UNKNOWN)

    # Identity check: wrong number
    returned_number = pr_data.get("number")
    if returned_number != pr_number:
        logger.warning(f"Identity mismatch for PR #{pr_number}: response.number={returned_number!r} → UNKNOWN")
        return ImplementationPRObservation(pr_number, PRTerminalState.UNKNOWN)

    # Identity check: wrong repository
    returned_repo = _extract_repo_from_pr_dict(pr_data)
    if returned_repo is not None and returned_repo.lower() != repo_name.lower():
        logger.warning(f"Repository mismatch for PR #{pr_number}: " f"expected {repo_name!r}, got {returned_repo!r} → UNKNOWN")
        return ImplementationPRObservation(pr_number, PRTerminalState.UNKNOWN)

    merged = pr_data.get("merged") is True or pr_data.get("merged_at") is not None
    raw_state = str(pr_data.get("state", "")).lower()

    if merged:
        return ImplementationPRObservation(pr_number, PRTerminalState.MERGED, merged=True)
    if raw_state == "closed":
        return ImplementationPRObservation(pr_number, PRTerminalState.CLOSED, merged=False)
    if raw_state == "open":
        return ImplementationPRObservation(pr_number, PRTerminalState.OPEN)

    return ImplementationPRObservation(pr_number, PRTerminalState.UNKNOWN)


# ---------------------------------------------------------------------------
# Jules session ownership resolution — REQ-004
# ---------------------------------------------------------------------------


@dataclass
class _JulesOwnershipResolution:
    """Outcome of resolving Jules session ownership for one owner."""

    # Session IDs positively attributed to this owner (via CloudManager/CloudRun)
    attributed_sessions: List[str] = field(default_factory=list)
    # Whether ownership resolution is blocked (unsupported/mixed/conflicting)
    blocked: bool = False
    blocked_reason: str = ""


def _resolve_jules_session_ownership(
    owner: ImplementationOwner,
    slots: ImplementationSlotRepository,
    cloud_manager: Optional[Any],
    cloud_run_store: Optional[Any],
    expected_repo: str,
) -> _JulesOwnershipResolution:
    """Resolve all Jules sessions positively attributed to *owner* (REQ-004).

    Uses durable launch/provider bindings from CloudManager and CloudRun,
    corroborated by repository/source context. Does not guess from numeric
    session IDs or PR URLs.
    """
    resolution = _JulesOwnershipResolution()

    # 1. Slot's own provider_sessions list
    with slots._state_lock():
        record = slots._read().get(owner.key, {})
    stored_sessions_raw = record.get("provider_sessions", [])
    if not isinstance(stored_sessions_raw, list):
        resolution.blocked = True
        resolution.blocked_reason = "Cannot parse stored provider_sessions"
        return resolution

    for sid in stored_sessions_raw:
        if not isinstance(sid, str) or not sid:
            resolution.blocked = True
            resolution.blocked_reason = f"Invalid session id in slot store: {sid!r}"
            return resolution

    slot_sessions: List[str] = list(stored_sessions_raw)

    # 2. CloudManager binding
    cm_session: Optional[str] = None
    if cloud_manager is not None:
        try:
            binding = cloud_manager.get_binding(owner.number)
            if binding is not None:
                if binding.provider and binding.provider.lower() not in ("jules", ""):
                    # Non-Jules provider: blocks this early-release path (REQ-004)
                    resolution.blocked = True
                    resolution.blocked_reason = f"Unsupported provider {binding.provider!r} in CloudManager " f"for owner {owner.key}"
                    return resolution
                if binding.task_id:
                    cm_session = binding.task_id
        except Exception as exc:
            resolution.blocked = True
            resolution.blocked_reason = f"Cannot read CloudManager for owner {owner.key}: {exc}"
            return resolution

    # 3. CloudRun records (non-Jules provider detection)
    if cloud_run_store is not None:
        try:
            runs = _get_cloud_runs_for_issue(cloud_run_store, expected_repo, owner.number)
            for run in runs:
                provider = getattr(run, "provider", "") or ""
                task_id = getattr(run, "task_id", "") or ""
                if provider.lower() not in ("jules", ""):
                    resolution.blocked = True
                    resolution.blocked_reason = f"Non-Jules provider {provider!r} found in CloudRun " f"for owner {owner.key}"
                    return resolution
                # Include pending CloudRun sessions not yet in slot's session array
                if task_id and task_id not in slot_sessions:
                    slot_sessions.append(task_id)
        except Exception as exc:
            resolution.blocked = True
            resolution.blocked_reason = f"Cannot read CloudRun store for owner {owner.key}: {exc}"
            return resolution

    # Merge slot sessions and CloudManager session
    all_sessions: List[str] = list(slot_sessions)
    if cm_session and cm_session not in all_sessions:
        all_sessions.append(cm_session)

    resolution.attributed_sessions = all_sessions
    return resolution


def _get_cloud_runs_for_issue(cloud_run_store: Any, repo_name: str, issue_number: int) -> List[Any]:
    """Return CloudRun records for an issue using CloudRunRepository's real API.

    ``CloudRunRepository`` (see ``cloud_run.py``) exposes ``list_for_issue``
    (issue-scoped, preferred) and ``list_all`` — there is no
    ``get_runs_for_issue``/``list_runs`` method on the real class. Calling a
    nonexistent method would silently return an empty list via ``getattr``
    and make accepted/unresolved Jules work recorded only in CloudRunRepository
    invisible to retirement evidence (REQ-004).
    """
    issue_getter = getattr(cloud_run_store, "list_for_issue", None)
    if callable(issue_getter):
        return list(issue_getter(issue_number) or [])
    # Fall back to the full listing only when the issue-scoped API is absent.
    lister = getattr(cloud_run_store, "list_all", None)
    if callable(lister):
        return [r for r in (lister() or []) if getattr(r, "issue_number", None) == issue_number]
    return []


# ---------------------------------------------------------------------------
# Jules session liveness — REQ-005, REQ-006
# ---------------------------------------------------------------------------


@dataclass
class _JulesSessionEvidence:
    """Evidence for one Jules session's lifecycle."""

    session_id: str
    state: SessionTerminalState
    latest_activity_ended: bool = True
    # Whether we have established PR publication for this session
    established_pr_outputs: List[int] = field(default_factory=list)
    # Whether publication is not yet established (COMPLETED without outputs)
    publication_pending: bool = False


def _observe_jules_session(
    session_id: str,
    jules_client: Any,
    expected_repo: str,
    activity_revision: int,
    owner_key: str,
) -> _JulesSessionEvidence:
    """Observe one Jules session individually (REQ-005).

    Reads the session directly, not from the cached full-session list.
    COMPLETED/FAILED are terminal candidates only when publication is established
    and the latest admitted activity has positively ended (REQ-005, REQ-006).
    """
    try:
        raw_session = jules_client.get_session(session_id)
    except Exception as exc:
        logger.warning(f"Cannot read Jules session {session_id} for {owner_key}: {exc} → UNKNOWN")
        return _JulesSessionEvidence(session_id=session_id, state=SessionTerminalState.UNKNOWN)

    if not isinstance(raw_session, dict):
        logger.warning(f"Malformed Jules session {session_id} for {owner_key}: " f"expected dict, got {type(raw_session).__name__} → UNKNOWN")
        return _JulesSessionEvidence(session_id=session_id, state=SessionTerminalState.UNKNOWN)

    raw_state = raw_session.get("state")
    if not isinstance(raw_state, str):
        logger.warning(f"Jules session {session_id} for {owner_key}: missing/invalid state → UNKNOWN")
        return _JulesSessionEvidence(session_id=session_id, state=SessionTerminalState.UNKNOWN)

    # Explicitly supported active states retain capacity immediately (REQ-005).
    # Any AWAITING_* variant NOT in the supported contract (_JULES_ACTIVE_STATES)
    # must fall through to UNKNOWN below, never be implicitly treated as ACTIVE
    # merely because it happens to start with "AWAITING_".
    if raw_state in _JULES_ACTIVE_STATES:
        return _JulesSessionEvidence(
            session_id=session_id,
            state=SessionTerminalState.ACTIVE,
            latest_activity_ended=False,
        )

    if raw_state not in _JULES_TERMINAL_STATES:
        # Unsupported/unknown state (including an unknown AWAITING_* variant
        # not in the supported contract) → UNKNOWN, never implicitly ACTIVE.
        logger.warning(f"Jules session {session_id} for {owner_key}: " f"unsupported state {raw_state!r} → UNKNOWN")
        return _JulesSessionEvidence(session_id=session_id, state=SessionTerminalState.UNKNOWN)

    # raw_state is COMPLETED or FAILED — extract PR publication evidence
    raw_outputs = raw_session.get("outputs", {})
    pr_numbers, any_foreign = _extract_all_pr_numbers_from_outputs(raw_outputs, expected_repo)
    established_prs = [n for n in pr_numbers if n > 0]

    if raw_state == "COMPLETED" and not established_prs:
        # COMPLETED without established publication → waiting-for-publication (REQ-005)
        logger.debug(f"Jules session {session_id} COMPLETED without established PR outputs; " "retaining as publication pending")
        return _JulesSessionEvidence(
            session_id=session_id,
            state=SessionTerminalState.ACTIVE,
            latest_activity_ended=False,
            publication_pending=True,
        )

    # COMPLETED with PRs or FAILED with established terminal PRs:
    # Check activity causality (REQ-006)
    activity_ended = _check_activity_causality(session_id, jules_client, owner_key, activity_revision, raw_session)

    return _JulesSessionEvidence(
        session_id=session_id,
        state=SessionTerminalState.ENDED if activity_ended else SessionTerminalState.UNKNOWN,
        latest_activity_ended=activity_ended,
        established_pr_outputs=established_prs,
    )


def _check_activity_causality(
    session_id: str,
    jules_client: Any,
    owner_key: str,
    activity_revision: int,
    raw_session: Dict[str, Any],
) -> bool:
    """Determine whether the observed terminal state is attributable to the
    current admitted activity (REQ-006).

    A terminal result captured before a later resume/repair/plan-approval must
    not settle the newer activity. Without a later assignment, a fresh complete
    confirmation may establish terminality.

    When activities are not available or ambiguous, returns False (UNKNOWN).
    """
    activities_getter = getattr(jules_client, "get_session_activities", None)
    if not callable(activities_getter):
        # No activities support at all. REQ-006/Issue #2147 item 4: absence of
        # contrary evidence is NOT proof the latest admitted activity ended,
        # so this must be UNKNOWN rather than a conservative terminal accept.
        logger.debug(f"Jules session {session_id}: activities API not available → UNKNOWN causality")
        return False

    try:
        activities = activities_getter(session_id)
    except Exception as exc:
        logger.warning(f"Cannot fetch activities for Jules session {session_id}: {exc} → UNKNOWN causality")
        return False

    if not isinstance(activities, (list, tuple)):
        logger.warning(f"Malformed activities payload for Jules session {session_id} " f"(expected list, got {type(activities).__name__}) → UNKNOWN causality")
        return False

    # Look for sessionCompleted/sessionFailed event preceded by a
    # user-message or plan-approval event that would confirm current activity.
    # The activity_revision tells us how many admitted activity events the
    # local store has registered; if the activities list shows a completion
    # event that post-dates the most recent user-message/plan event, the
    # terminal state is causally attributable to the current activity.
    completion_events = [a for a in activities if isinstance(a, dict) and _activity_kind(a) in ("session_completed", "session_failed")]
    user_events = [a for a in activities if isinstance(a, dict) and _activity_kind(a) in ("user_message", "plan_approval")]

    if not completion_events:
        return False

    # Simplest valid causality: a completion event exists and no later
    # user/plan events follow it (no newer admitted activity).
    latest_completion = _max_activity_time(completion_events)
    latest_user = _max_activity_time(user_events)

    if latest_completion is None:
        return False

    if latest_user is not None and latest_user > latest_completion:
        # A user/plan event occurred after the completion — indicates newer
        # admitted activity that this terminal observation does not settle.
        logger.debug(f"Jules session {session_id}: user/plan event after completion → UNKNOWN causality")
        return False

    return True


# Real Jules Activity payloads (jules.google/docs/api/reference/activities/)
# represent the event kind as a "oneof"-style field: exactly one of these keys
# holds the event's own (possibly empty) object, rather than a synthetic
# top-level "type" string. Some historical/test fixtures use the latter
# convention directly; both are normalized to the same canonical kind so
# causality checks do not depend on which shape the payload used.
_ACTIVITY_KIND_ONEOF_KEYS: Dict[str, str] = {
    "userMessage": "user_message",
    "user_message": "user_message",
    "planApproval": "plan_approval",
    "plan_approval": "plan_approval",
    "planApprovalActivity": "plan_approval",
    "sessionCompleted": "session_completed",
    "session_completed": "session_completed",
    "sessionCompletedActivity": "session_completed",
    "sessionFailed": "session_failed",
    "session_failed": "session_failed",
    "sessionFailedActivity": "session_failed",
}

_ACTIVITY_KIND_TYPE_ALIASES: Dict[str, str] = {
    "userMessage": "user_message",
    "user_message": "user_message",
    "planApproval": "plan_approval",
    "plan_approval": "plan_approval",
    "sessionCompleted": "session_completed",
    "session_completed": "session_completed",
    "sessionFailed": "session_failed",
    "session_failed": "session_failed",
}


def _activity_kind(activity: Dict[str, Any]) -> Optional[str]:
    """Return the canonical kind of one Jules activity event, or None.

    Canonical kinds: "user_message", "plan_approval", "session_completed",
    "session_failed". Supports both a synthetic ``type`` string field (used
    by some historical fixtures) and the real oneof-style payload shape where
    the event kind is expressed as the presence of one specific field (e.g.
    ``sessionCompleted``) holding the event's own object. Reject ambiguous
    entries (more than one oneof key present) conservatively by returning
    None rather than guessing.
    """
    raw_type = activity.get("type")
    if isinstance(raw_type, str) and raw_type in _ACTIVITY_KIND_TYPE_ALIASES:
        return _ACTIVITY_KIND_TYPE_ALIASES[raw_type]

    found: List[str] = []
    for key, kind in _ACTIVITY_KIND_ONEOF_KEYS.items():
        if key in activity and activity.get(key) is not None:
            if kind not in found:
                found.append(kind)
    if len(found) == 1:
        return found[0]
    return None


def _max_activity_time(activities: List[Dict[str, Any]]) -> Optional[float]:
    """Return the maximum timestamp from a list of activity dicts, or None."""
    times: List[float] = []
    for a in activities:
        ts = a.get("createTime") or a.get("timestamp") or a.get("eventTime")
        if ts is None:
            continue
        if isinstance(ts, (int, float)):
            times.append(float(ts))
        elif isinstance(ts, str):
            try:
                from dateutil import parser as dateutil_parser

                dt = dateutil_parser.parse(ts)
                times.append(dt.timestamp())
            except Exception:
                pass
    return max(times) if times else None


# ---------------------------------------------------------------------------
# Local execution liveness — REQ-001, REQ-010
# ---------------------------------------------------------------------------


def _observe_local_executions(
    slots: ImplementationSlotRepository,
    owner: ImplementationOwner,
) -> List[LocalExecutionObservation]:
    """Observe liveness of all local executions recorded for *owner* (REQ-010).

    Uses process-identity-aware dead-execution classification. A process whose
    identity cannot be conclusively classified as ended is UNKNOWN.
    """
    with slots._state_lock():
        record = slots._read().get(owner.key, {})
    stored_executions = record.get("executions", [])
    if not isinstance(stored_executions, list):
        return []

    observations: List[LocalExecutionObservation] = []
    for exec_entry in stored_executions:
        if not isinstance(exec_entry, dict):
            continue
        exec_id = exec_entry.get("id")
        if not isinstance(exec_id, str) or not exec_id:
            continue

        state = _classify_execution_liveness(exec_entry)
        observations.append(LocalExecutionObservation(execution_id=exec_id, state=state))

    return observations


def _classify_execution_liveness(
    exec_entry: Dict[str, Any],
) -> ExecutionTerminalState:
    """Classify one execution record's liveness (REQ-010).

    Process-identity-aware: uses boot_id + start_ticks stored during admission
    to distinguish a dead PID from a reused PID on the same machine.
    """
    pid = exec_entry.get("pid")
    boot_id = exec_entry.get("boot_id")
    process_start_ticks = exec_entry.get("process_start_ticks")

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        # No PID recorded — cannot determine liveness
        return ExecutionTerminalState.UNKNOWN

    if not _pid_exists(pid):
        return ExecutionTerminalState.ENDED

    # PID exists — check identity to distinguish reuse
    if boot_id is not None or process_start_ticks is not None:
        current_identity = _read_process_identity(pid)
        if current_identity is None:
            # Cannot read current process identity — uncertain
            return ExecutionTerminalState.UNKNOWN
        if boot_id is not None and current_identity.get("boot_id") != boot_id:
            return ExecutionTerminalState.ENDED
        if process_start_ticks is not None and current_identity.get("start_ticks") != process_start_ticks:
            return ExecutionTerminalState.ENDED
        # Identity matches — process is genuinely running
        return ExecutionTerminalState.LIVE

    # PID exists but no stored identity to compare against — uncertain
    return ExecutionTerminalState.UNKNOWN


def _pid_exists(pid: int) -> bool:
    """Return True if *pid* exists in the OS process table."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we cannot signal it
    except Exception:
        return False


def _read_process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Read current process identity fields for *pid*, or None if unreadable."""
    try:
        stat_path = f"/proc/{pid}/stat"
        with open(stat_path, "r", encoding="utf-8") as f:
            content = f.read()
        # starttime is field 22 (0-indexed 21) in /proc/PID/stat
        parts = content.split()
        if len(parts) >= 22:
            start_ticks = int(parts[21])
        else:
            return None
        boot_id = _read_boot_id()
        return {"start_ticks": start_ticks, "boot_id": boot_id}
    except Exception:
        return None


def _read_boot_id() -> Optional[str]:
    """Read the system boot_id from /proc/sys/kernel/random/boot_id."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main entry point — REQ-001
# ---------------------------------------------------------------------------


def collect_retirement_observation(
    owner: ImplementationOwner,
    slots: ImplementationSlotRepository,
    github_client: Any,
    jules_client: Optional[Any] = None,
    cloud_manager: Optional[Any] = None,
    cloud_run_store: Optional[Any] = None,
) -> Optional[ImplementationRetirementObservation]:
    """Derive an authoritative ImplementationRetirementObservation for *owner*.

    Returns None when the owner is not in scope (not active, or not an
    Issue-owned slot eligible for PR-backed retirement).

    The caller must evaluate the returned observation with
    ``evaluate_retirement_predicate`` (or pass it to ``retire_implementation_slot``)
    before acting on it. This function only collects evidence; it does not
    commit any state.

    REQ-001: identifies repository, owner, incarnation, activity_revision,
    complete PR/session membership, local execution evidence, and continuing
    obligations. Missing required evidence produces UNKNOWN, not empty sets.
    """
    if owner.kind != "issue":
        return None

    # Read live slot state
    with slots._state_lock():
        records = slots._read()
        record = records.get(owner.key)
    if record is None:
        return None

    incarnation = record.get("incarnation")
    if not isinstance(incarnation, str) or not incarnation:
        return None
    activity_revision = record.get("activity_revision")
    if activity_revision is None or isinstance(activity_revision, bool) or not isinstance(activity_revision, int):
        return None

    expected_repo = slots.repo_name

    # Check for unsupported provider conditions (REQ-004)
    jules_resolution = _resolve_jules_session_ownership(owner, slots, cloud_manager, cloud_run_store, expected_repo)
    if jules_resolution.blocked:
        # Cannot safely determine provider scope — return UNKNOWN observation
        logger.warning(f"Jules ownership resolution blocked for {owner.key}: " f"{jules_resolution.blocked_reason}")
        # Return a minimal observation that will evaluate as RETAINED_UNKNOWN
        return ImplementationRetirementObservation(
            repository=expected_repo,
            owner=owner,
            reservation_incarnation=incarnation,
            activity_revision=activity_revision,
            implementation_prs=(),
            provider_sessions=(
                ProviderSessionObservation(
                    session_id="__blocked__",
                    provider="unknown",
                    state=SessionTerminalState.UNKNOWN,
                ),
            ),
            local_executions=(),
            continuing_obligations=ContinuingObligations(has_unresolved_submission=True),
        )

    attributed_sessions = jules_resolution.attributed_sessions

    # Fetch raw Jules session data for PR candidate set construction
    jules_session_raws: Dict[str, Dict[str, Any]] = {}
    if jules_client is not None:
        for session_id in attributed_sessions:
            try:
                raw = jules_client.get_session(session_id)
                if isinstance(raw, dict):
                    jules_session_raws[session_id] = raw
            except Exception as exc:
                logger.warning(f"Cannot pre-fetch Jules session {session_id} for " f"PR candidate construction: {exc}")

    # Build PR candidate set (REQ-002)
    candidate_set = _build_pr_candidate_set(owner, slots, github_client, jules_session_raws, expected_repo)

    # Observe each PR individually (REQ-003)
    pr_observations: List[ImplementationPRObservation] = []
    for pr_num in candidate_set.local_prs:
        obs = _observe_pr(github_client, expected_repo, pr_num)
        pr_observations.append(obs)

    # Contradicted PRs → UNKNOWN blocker (REQ-002)
    for pr_num in candidate_set.contradicted_prs:
        pr_observations.append(ImplementationPRObservation(pr_num, PRTerminalState.UNKNOWN))

    # Incomplete discovery → add a synthetic UNKNOWN PR blocker (REQ-002).
    #
    # This must block unconditionally, even when other known/local PR
    # candidates exist and are all terminal: incomplete native-association or
    # open-PR enumeration means an unobserved implementation PR could still
    # exist for this owner, and evidence of *other* terminal PRs does not
    # establish that no such PR exists. Only gating this on "no other known
    # PRs" would let a stale/incomplete crash-recovery discovery authorize
    # release merely because the already-known PR happened to be closed.
    if candidate_set.incomplete_discovery:
        pr_observations.append(ImplementationPRObservation(-1, PRTerminalState.UNKNOWN))

    # Observe each Jules session individually (REQ-005)
    session_observations: List[ProviderSessionObservation] = []
    for session_id in attributed_sessions:
        if jules_client is None:
            session_observations.append(
                ProviderSessionObservation(
                    session_id=session_id,
                    provider="jules",
                    state=SessionTerminalState.UNKNOWN,
                )
            )
            continue
        evidence = _observe_jules_session(session_id, jules_client, expected_repo, activity_revision, owner.key)
        session_observations.append(
            ProviderSessionObservation(
                session_id=session_id,
                provider="jules",
                state=evidence.state,
                latest_activity_ended=evidence.latest_activity_ended,
            )
        )

    # Observe local executions (REQ-010)
    local_exec_observations = _observe_local_executions(slots, owner)

    # Continuing obligations (read from slot store — set by admission writers)
    obligations = _read_continuing_obligations(record)

    return ImplementationRetirementObservation(
        repository=expected_repo,
        owner=owner,
        reservation_incarnation=incarnation,
        activity_revision=activity_revision,
        implementation_prs=tuple(pr_observations),
        provider_sessions=tuple(session_observations),
        local_executions=tuple(local_exec_observations),
        continuing_obligations=obligations,
    )


def _read_continuing_obligations(record: Dict[str, Any]) -> ContinuingObligations:
    """Read continuing-work obligations from a slot owner record."""
    # The slot store's boolean flags set by admission/dispatch writers
    has_unresolved = bool(record.get("submission_outcome_indeterminate", False))
    has_repair = bool(record.get("assigned_repair", False))
    has_replacement = bool(record.get("replacement_publication", False))
    has_retry_handoff = bool(record.get("admitted_retry_handoff", False))
    return ContinuingObligations(
        has_unresolved_submission=has_unresolved,
        has_assigned_repair=has_repair,
        has_replacement_publication=has_replacement,
        has_admitted_retry_handoff=has_retry_handoff,
    )


# ---------------------------------------------------------------------------
# Retirement guard for Jules outbound boundaries — REQ-007, REQ-009
# ---------------------------------------------------------------------------


def guard_retired_session_reuse(
    session_id: str,
    slots: ImplementationSlotRepository,
) -> bool:
    """Return True if *session_id* is retired and must not be reused (REQ-009).

    A retired session must not be automatically resumed, sent repair/publication
    instructions, recreated as a replacement session, or adopted as a new slot
    merely because maintenance or an old provider list reports it again.
    """
    return slots.has_retired_session(session_id)


def guard_retired_pr_reuse(
    pr_number: int,
    slots: ImplementationSlotRepository,
) -> bool:
    """Return True if *pr_number* is from a retired slot and must not be reused (REQ-009)."""
    return slots.has_retired_pr(pr_number)


def register_outbound_jules_activity(
    owner: ImplementationOwner,
    slots: ImplementationSlotRepository,
    session_id: str,
) -> bool:
    """Durably register Jules outbound activity before the actual send (REQ-007).

    Must be called before sending resume, feedback, plan-approval,
    replacement-session, or publication work. Uses
    ``ImplementationSlotRepository.admit_outbound_provider_activity``, which
    unconditionally advances ``activity_revision`` for the owner's current
    incarnation — even when *session_id* is already a known provider session
    (same-session continuation) — unlike ``record_provider_session``, whose
    membership recording is idempotent and therefore does not by itself
    advance the revision for an unchanged session id.

    This ensures any retirement observation collected before this call
    becomes stale: a subsequent ``retire_implementation_slot`` call using
    that older observation will detect the activity_revision mismatch and
    return STALE_OBSERVATION rather than releasing capacity prematurely.

    Returns True if the admission succeeded, False if the owner is no
    longer active (indicating retirement has already occurred) — in which
    case the caller MUST NOT send the outbound mutation (REQ-009).
    """
    return slots.admit_outbound_provider_activity(owner, session_id)


def admit_or_block_outbound_jules_send(
    session_id: str,
    issue_number: Optional[int],
    slots: Optional[ImplementationSlotRepository],
) -> bool:
    """Guard + durably admit one outbound Jules mutation (REQ-007, REQ-009).

    Shared boundary helper for every real outbound Jules production caller
    (periodic maintenance resume/plan-approval/replacement-session, PR
    repair/recovery feedback, explicit --only/--force paths, etc). Must be
    called immediately before sending the outbound provider mutation.

    Returns True when the caller may proceed with the outbound mutation.
    Returns False when the caller MUST NOT send it: either the session
    already belongs to a durably retired slot, or durable admission of the
    new activity failed because the owner's slot retired concurrently, or
    the guard/admission check itself failed (a failure here must block the
    send, never be silently treated as permission to proceed).

    When *slots* is None (no repository/slot-store context available) or
    *issue_number* does not resolve to an ordinary Issue owner, this is a
    no-op returning True: non-Jules-retirement-tracked callers and code
    paths untouched by Issue #2147 behave exactly as before.
    """
    if slots is None:
        return True
    try:
        if guard_retired_session_reuse(session_id, slots):
            logger.info(f"Jules session {session_id} belongs to a durably retired implementation slot; " "refusing outbound mutation (REQ-009)")
            return False

        if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0:
            # No resolvable ordinary Issue owner for this session — nothing to
            # admit against, and the retired-session guard above already ran.
            return True

        owner = ImplementationOwner("issue", issue_number)
        if not register_outbound_jules_activity(owner, slots, session_id):
            logger.info(f"Jules session {session_id} owner #{issue_number} has already retired; " "refusing outbound mutation (REQ-007/REQ-009)")
            return False
        return True
    except Exception as exc:
        logger.error(f"Retirement guard/admission failed for Jules session {session_id}: {exc}; " "blocking outbound mutation")
        return False
