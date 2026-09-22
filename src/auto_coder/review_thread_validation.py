"""Independent validation of implementation-agent "addressed" claims on review threads.

Auto-Coder can delegate an unresolved PR review thread to a cloud implementation
agent (see :mod:`review_feedback_marker`). The agent may fix the reported defect
and reply with a machine-readable "addressed" claim, but it must never resolve
the GitHub thread itself. This module implements the independent side of that
protocol: selecting the eligible, explicitly claimed threads that a fresh
``backend_adversarial_validation`` run must also adjudicate, and resolving only
the ones the validator establishes are genuinely fixed on the still-current PR
head.

Fail-closed by construction:
- A thread whose root comment was not authored by a supported automated
  reviewer is never eligible, no matter what text or marker its replies
  contain (a human-review thread cannot be auto-adjudicated).
- A thread is only "claimed" when an eligible root AND an explicit versioned
  marker are both present; ordinary discussion never counts.
- Resolution requires a fresh, valid ``ADDRESSED`` disposition for the exact
  thread, the PR head unchanged since validation, a successfully recorded
  resolver explanation, and a confirmed GitHub resolve mutation. Any failure
  at any step leaves the thread unresolved.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Set

from .adversarial_validator import CHANGE_PROVENANCE_CLARIFICATION_MARKER, ReviewThreadDisposition
from .logger_config import get_logger
from .review_feedback_marker import reply_claims_review_addressed
from .util.gh_cache import ReviewThread
from .utils import is_same_github_login

logger = get_logger(__name__)

RESOLVER_EXPLANATION_MARKER = "<!-- auto-coder-review-thread-resolved:v1 -->"

# Posted into the affected GitHub thread itself as an independent durability
# layer for a stale-resolution blocker: unlike the local JSON registry, a
# GitHub comment survives a local disk failure or process restart, because
# it lives in a different failure domain (REQ-006, REQ-008).
STALE_BLOCKER_MARKER = "<!-- auto-coder-stale-review-thread-blocker:v1 -->"
STALE_BLOCKER_CLEARED_MARKER = "<!-- auto-coder-stale-review-thread-blocker-cleared:v1 -->"

# Second line appended to a freshly-posted STALE_BLOCKER_MARKER, recording the
# PR head the blocker was posted against. A later scan compares this to the
# PR's *current* head: if nothing has changed, the marker cannot represent an
# actual stale resolution (staleness only exists relative to a head that has
# since moved), so a failed close-out CLEARED write can never make a
# still-valid, current-head resolution look stale on a later run. A bare
# STALE_BLOCKER_MARKER with no head line (the legacy/unknown-provenance case)
# has no such information to compare against and is always treated as
# pending, matching the previous, more conservative behavior.
_STALE_BLOCKER_HEAD_LINE_PREFIX = "<!-- resolved-against-head: "
_STALE_BLOCKER_HEAD_RE = re.compile(re.escape(_STALE_BLOCKER_HEAD_LINE_PREFIX) + r"(?P<sha>\S+) -->$")


def _format_stale_blocker_marker(head_sha: str) -> str:
    return f"{STALE_BLOCKER_MARKER}\n{_STALE_BLOCKER_HEAD_LINE_PREFIX}{head_sha} -->"


# Bounded attempts to revert a stale resolution (REQ-006/REQ-008): a single
# failed rollback attempt must not permanently leave a thread resolved
# against a disposition for a head that is no longer current.
UNRESOLVE_ROLLBACK_MAX_ATTEMPTS = 3


class StaleReviewThreadResolutionError(RuntimeError):
    """A thread was resolved against a stale head and could not be rolled back.

    The GitHub thread has already been marked resolved by a disposition that
    no longer corresponds to the PR's current head, and every rollback
    attempt failed or returned an unconfirmed response. This is a durable
    integrity problem, not a transient one to log and move past: the caller
    must treat it as a merge-blocking failure rather than silently
    continuing (REQ-006, REQ-008).
    """

    def __init__(self, thread_id: str, repo_name: str, pr_number: int) -> None:
        self.thread_id = thread_id
        self.repo_name = repo_name
        self.pr_number = pr_number
        super().__init__(f"Thread {thread_id} on {repo_name}#{pr_number} was resolved against a stale PR head and could not be rolled back to unresolved")


class StaleReviewThreadRegistryError(RuntimeError):
    """The stale-resolution registry's storage could not be trusted.

    Raised when the registry file exists but cannot be read or parsed
    (corruption, permissions, an I/O error, or unexpected content). A missing
    file legitimately means "no blockers recorded yet"; an existing-but-bad
    file must never be treated the same way, since it may be hiding a real
    blocker (REQ-006, REQ-008) — silently degrading to an empty registry
    would recreate the exact false-success this registry exists to prevent.
    """


@dataclass
class StaleReviewThreadBlocker:
    """Durable state for rollback, its transition, or marker cleanup work.

    Persisted so rollback failures and post-rollback marker cleanup survive
    separate ``_handle_pr_merge`` invocations and process restarts
    (REQ-006, REQ-008).
    """

    repository: str = ""
    pr_number: int = 0
    thread_id: str = ""
    state: str = "rollback_required"
    root_comment_database_id: Optional[int] = None


class StaleReviewThreadRegistry:
    """Persistent store of stale rollbacks and their marker cleanup state."""

    _lock = threading.RLock()

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path.home() / ".auto-coder" / "stale_review_threads.json"

    @staticmethod
    def _key(repository: str, thread_id: str) -> str:
        return json.dumps([repository, thread_id], separators=(",", ":"))

    def _load(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            # No file yet legitimately means "no blockers recorded" — this is
            # the only case allowed to return empty without raising.
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StaleReviewThreadRegistryError(f"Could not read stale-review-thread registry at {self.path}: {exc}") from exc
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StaleReviewThreadRegistryError(f"Stale-review-thread registry at {self.path} contains invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise StaleReviewThreadRegistryError(f"Stale-review-thread registry at {self.path} does not contain a JSON object")
        return value

    def _save(self, data: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def record(self, repository: str, pr_number: int, thread_id: str) -> None:
        """Persist that ``thread_id`` is resolved against a stale head."""
        with self._lock:
            data = self._load()
            data[self._key(repository, thread_id)] = asdict(StaleReviewThreadBlocker(repository=repository, pr_number=pr_number, thread_id=thread_id))
            self._save(data)

    def record_marker_cleanup(self, repository: str, pr_number: int, thread_id: str, root_comment_database_id: int) -> None:
        """Persist that rollback succeeded and only marker cleanup remains."""
        with self._lock:
            data = self._load()
            data[self._key(repository, thread_id)] = asdict(
                StaleReviewThreadBlocker(
                    repository=repository,
                    pr_number=pr_number,
                    thread_id=thread_id,
                    state="marker_cleanup_required",
                    root_comment_database_id=root_comment_database_id,
                )
            )
            self._save(data)

    def record_rollback_transition(self, repository: str, pr_number: int, thread_id: str, root_comment_database_id: Optional[int]) -> None:
        """Persist an ambiguous rollback transition before mutating GitHub.

        If the process cannot durably record the mutation's outcome afterward,
        this state prevents a future run from applying the old blocker to a
        later legitimate resolution.
        """
        with self._lock:
            data = self._load()
            data[self._key(repository, thread_id)] = asdict(
                StaleReviewThreadBlocker(
                    repository=repository,
                    pr_number=pr_number,
                    thread_id=thread_id,
                    state="rollback_transition",
                    root_comment_database_id=root_comment_database_id,
                )
            )
            self._save(data)

    def clear(self, repository: str, thread_id: str) -> None:
        """Remove a blocker once GitHub confirms the thread is unresolved."""
        with self._lock:
            data = self._load()
            key = self._key(repository, thread_id)
            if key not in data:
                return
            del data[key]
            self._save(data)

    def _records_for_pr(self, repository: str, pr_number: int) -> List[StaleReviewThreadBlocker]:
        """Load and validate every registry record, returning this PR's rows.

        Every entry in the file is validated structurally, not just entries
        matching this PR: a syntactically valid JSON object containing a
        malformed entry (missing/mistyped fields, e.g. from a bug or manual
        edit) could otherwise silently hide a real blocker for this or any
        other PR, which is exactly the false-success this registry exists
        to prevent (REQ-006, REQ-008).
        """
        with self._lock:
            data = self._load()
        records: List[StaleReviewThreadBlocker] = []
        for key, value in data.items():
            if not isinstance(value, dict) or not isinstance(value.get("repository"), str) or not isinstance(value.get("pr_number"), int) or isinstance(value.get("pr_number"), bool) or not isinstance(value.get("thread_id"), str) or not value.get("thread_id"):
                raise StaleReviewThreadRegistryError(f"Stale-review-thread registry at {self.path} contains a malformed entry for key {key!r}")
            state = value.get("state", "rollback_required")
            root_comment_database_id = value.get("root_comment_database_id")
            if not isinstance(state, str) or state not in {"rollback_required", "rollback_transition", "marker_cleanup_required"}:
                raise StaleReviewThreadRegistryError(f"Stale-review-thread registry at {self.path} contains an invalid state for key {key!r}")
            if root_comment_database_id is not None and (not isinstance(root_comment_database_id, int) or isinstance(root_comment_database_id, bool)):
                raise StaleReviewThreadRegistryError(f"Stale-review-thread registry at {self.path} contains invalid root-comment data for key {key!r}")
            if state == "marker_cleanup_required" and root_comment_database_id is None:
                raise StaleReviewThreadRegistryError(f"Stale-review-thread registry at {self.path} contains invalid marker-cleanup data for key {key!r}")
            if value["repository"] == repository and value["pr_number"] == pr_number:
                records.append(
                    StaleReviewThreadBlocker(
                        repository=repository,
                        pr_number=pr_number,
                        thread_id=str(value["thread_id"]),
                        state=str(state),
                        root_comment_database_id=root_comment_database_id if isinstance(root_comment_database_id, int) else None,
                    )
                )
        return records

    def pending_for_pr(self, repository: str, pr_number: int) -> List[str]:
        """Return stale thread IDs whose rollback is still required."""
        return [record.thread_id for record in self._records_for_pr(repository, pr_number) if record.state == "rollback_required"]

    def marker_cleanup_pending_for_pr(self, repository: str, pr_number: int) -> dict[str, int]:
        """Return rolled-back threads that only need a CLEARED marker reply."""
        return {record.thread_id: record.root_comment_database_id for record in self._records_for_pr(repository, pr_number) if record.state == "marker_cleanup_required" and record.root_comment_database_id is not None}

    def rollback_transitions_for_pr(self, repository: str, pr_number: int) -> dict[str, Optional[int]]:
        """Return ambiguous transitions for authoritative reconciliation."""
        return {record.thread_id: record.root_comment_database_id for record in self._records_for_pr(repository, pr_number) if record.state == "rollback_transition"}


def _find_github_stale_blockers(github_client: Any, repo_name: str, pr_number: int) -> tuple[dict[str, Optional[int]], List[str], List[ReviewThread]]:
    """Scan every resolved review thread for an active ``STALE_BLOCKER_MARKER``.

    Returns ``(pending, incomplete_thread_ids, threads)``. ``pending`` maps a
    confirmed-blocked thread ID to its root comment's database ID (for
    posting the cleared-marker reply later, when available). This is an
    independent durability layer that lives on GitHub rather than local
    disk: a marker reply survives a local registry write failure and a
    process restart because it is in a different failure domain, and may be
    the only surviving evidence of a stale resolution (REQ-006, REQ-008).

    A thread's state is derived chronologically from trusted events: the
    *latest* marker (BLOCKER or CLEARED) authored by the identity proven by
    this GitHub client's credential wins. Exact marker text from any other
    author is ignored. Only resolved threads are considered, since an
    unresolved thread is already caught by the ordinary unresolved-thread
    gate regardless of any marker.

    Marker recognition requires an *exact* match (a comment body, stripped
    of surrounding whitespace, equal to the marker constant) rather than a
    substring search. Ordinary review discussion — including this very
    feature's own review thread — routinely quotes marker text in prose
    (e.g. "the CLEARED marker has not been posted yet"), and substring
    matching would let such prose silently create or clear blocker state.

    A thread whose comment list is truncated (``comments_truncated``) has an
    unknown latest state even when a BLOCKER is visible: an unfetched later
    page may contain its matching CLEARED event. Such threads are returned
    only in ``incomplete_thread_ids`` so the caller fails closed for merge
    processing (REQ-008) without mutating them from partial evidence.

    A freshly-posted ``STALE_BLOCKER_MARKER`` records the PR head it was
    posted against (see ``_format_stale_blocker_marker``). If that recorded
    head still equals the PR's *current* head, nothing has changed since the
    marker was posted, so it cannot represent an actual stale resolution
    (staleness is only meaningful relative to a head that has since moved):
    such a thread is not reported as pending. This is what keeps a failed
    close-out ``STALE_BLOCKER_CLEARED_MARKER`` write from making a
    still-valid, current-head resolution look stale on a later run. A bare,
    headless ``STALE_BLOCKER_MARKER`` (legacy/unknown provenance) or a
    failure to determine the current head fails the scan closed. When a
    head-bound marker is compared with the current head, a second
    authoritative lookup after every thread has been scanned must return the
    same head. This brackets the marker decision with a stable head snapshot
    so a push during the scan cannot make a just-dismissed marker stale.
    """
    threads = github_client.get_pr_review_threads_strict(repo_name, pr_number)
    pending: dict[str, Optional[int]] = {}
    incomplete_thread_ids: List[str] = []
    current_head_sha: Optional[str] = None
    current_head_fetched = False
    trusted_marker_author: Optional[str] = None
    trusted_marker_author_fetched = False

    def _current_head() -> Optional[str]:
        nonlocal current_head_sha, current_head_fetched
        if not current_head_fetched:
            current_head_fetched = True
            current_head_sha = github_client.get_pull_request_head_sha_strict(repo_name, pr_number)
        return current_head_sha

    def _trusted_marker_author() -> str:
        nonlocal trusted_marker_author, trusted_marker_author_fetched
        if not trusted_marker_author_fetched:
            trusted_marker_author_fetched = True
            candidate = github_client.get_authenticated_user_login()
            if not isinstance(candidate, str) or not candidate:
                raise RuntimeError("Could not determine Auto-Coder's authenticated GitHub marker identity")
            trusted_marker_author = candidate
        if trusted_marker_author is None:
            raise RuntimeError("Could not determine Auto-Coder's authenticated GitHub marker identity")
        return trusted_marker_author

    for thread in threads:
        if not thread.is_resolved:
            continue
        if thread.comments_truncated:
            incomplete_thread_ids.append(thread.id)
            continue
        comments = thread.comments or []
        root_comment_database_id = comments[0].database_id if comments else None
        # None = no active blocker marker seen yet; "" = a legacy/headless
        # blocker marker (always pending); otherwise the head it was posted
        # against.
        latest_blocker_head: Optional[str] = None
        for comment in comments:
            body = (comment.body or "").strip()
            if not (body == STALE_BLOCKER_CLEARED_MARKER or body == STALE_BLOCKER_MARKER or body.startswith(f"{STALE_BLOCKER_MARKER}\n")):
                continue
            if not is_same_github_login(comment.author_login, _trusted_marker_author()):
                continue
            if body == STALE_BLOCKER_CLEARED_MARKER:
                latest_blocker_head = None
            elif body == STALE_BLOCKER_MARKER:
                latest_blocker_head = ""
            elif body.startswith(STALE_BLOCKER_MARKER):
                match = _STALE_BLOCKER_HEAD_RE.search(body)
                latest_blocker_head = match.group("sha") if match else ""
        if latest_blocker_head is None:
            continue
        if latest_blocker_head and latest_blocker_head == _current_head():
            # Nothing has changed since this marker was posted; not stale.
            continue
        pending[thread.id] = root_comment_database_id

    if current_head_fetched:
        final_head_sha = github_client.get_pull_request_head_sha_strict(repo_name, pr_number)
        if final_head_sha != current_head_sha:
            raise RuntimeError(f"PR #{pr_number} head changed while scanning stale-resolution markers " f"({current_head_sha} -> {final_head_sha})")
    return pending, incomplete_thread_ids, threads


def retry_pending_stale_review_thread_rollbacks(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    registry: Optional[StaleReviewThreadRegistry] = None,
) -> List[str]:
    """Retry every persisted stale-resolution rollback for this PR.

    Called on every processing run so a rollback failure from an earlier run
    is retried rather than forgotten. Blockers are gathered from both the
    local registry and an independent GitHub-side marker scan, so a rollback
    failure survives even a local registry write failure combined with a
    process restart (REQ-006, REQ-008). A thread's blocker is cleared only
    once GitHub explicitly confirms it is unresolved; every failure or
    unconfirmed response leaves it recorded. Returns the thread IDs that are
    still blocked after this attempt — callers must refuse to merge while
    this is non-empty.
    """
    registry = registry or StaleReviewThreadRegistry()
    local_pending = registry.pending_for_pr(repo_name, pr_number)
    marker_cleanup_pending = registry.marker_cleanup_pending_for_pr(repo_name, pr_number)
    rollback_transitions = registry.rollback_transitions_for_pr(repo_name, pr_number)
    try:
        github_pending, incomplete_thread_ids, github_threads = _find_github_stale_blockers(github_client, repo_name, pr_number)
    except StaleReviewThreadRegistryError:
        raise
    except Exception as exc:
        # The GitHub-side marker may be the *only* surviving evidence of a
        # stale resolution (the local registry write can itself have
        # failed), so a lookup failure here must fail closed exactly like a
        # corrupt local registry, not be treated as "no blockers" (REQ-006,
        # REQ-008).
        raise StaleReviewThreadRegistryError(f"Could not scan GitHub review threads for stale-resolution markers on PR #{pr_number}: {exc}") from exc

    if incomplete_thread_ids:
        # These threads have truncated comment lists, so their latest marker
        # state is unknown regardless of which events are visible. Fail
        # closed (REQ-008) without mutating GitHub from partial evidence.
        raise StaleReviewThreadRegistryError(f"Could not fully scan review thread(s) {', '.join(incomplete_thread_ids)} for stale-resolution markers on PR #{pr_number}: comment list truncated")

    all_pending: dict[str, Optional[int]] = dict.fromkeys(local_pending, None)
    all_pending.update(github_pending)
    for thread_id in marker_cleanup_pending:
        # A confirmed rollback owns this old marker. It must never attach to
        # a later legitimate resolution and authorize another unresolve.
        all_pending.pop(thread_id, None)
    for thread_id in rollback_transitions:
        all_pending.pop(thread_id, None)

    still_blocked: List[str] = []
    for thread_id, cleanup_root_comment_database_id in marker_cleanup_pending.items():
        try:
            github_client.reply_to_review_thread(repo_name, pr_number, cleanup_root_comment_database_id, STALE_BLOCKER_CLEARED_MARKER)
            registry.clear(repo_name, thread_id)
        except Exception as exc:
            logger.error(f"Could not finish stale-marker cleanup for rolled-back thread {thread_id} on PR #{pr_number}: {exc}")
            still_blocked.append(thread_id)

    for thread_id, transition_root_comment_database_id in rollback_transitions.items():
        exact_matches = [thread for thread in github_threads if thread.id == thread_id]
        if len(exact_matches) != 1:
            logger.error(f"Could not reconcile rollback transition for thread {thread_id} on PR #{pr_number}: " f"expected exactly one authoritative thread, found {len(exact_matches)}")
            still_blocked.append(thread_id)
            continue

        if exact_matches[0].is_resolved:
            # The guarded rollback did not run or did not take effect. Keep
            # the existing transition durable while retrying it below.
            all_pending[thread_id] = transition_root_comment_database_id
            continue

        # GitHub now explicitly confirms the exact thread is unresolved, so
        # the ambiguous mutation outcome has converged. Retire its marker
        # without ever issuing another unresolve against this thread.
        if transition_root_comment_database_id is not None:
            try:
                github_client.reply_to_review_thread(
                    repo_name,
                    pr_number,
                    transition_root_comment_database_id,
                    STALE_BLOCKER_CLEARED_MARKER,
                )
            except Exception as exc:
                logger.error(f"Confirmed thread {thread_id} unresolved on PR #{pr_number} but could not post the cleared marker: {exc}")
                try:
                    registry.record_marker_cleanup(
                        repo_name,
                        pr_number,
                        thread_id,
                        transition_root_comment_database_id,
                    )
                except Exception as persist_exc:
                    logger.error(f"Could not record marker cleanup for reconciled thread {thread_id} on PR #{pr_number}; " f"the fail-closed rollback transition remains: {persist_exc}")
                still_blocked.append(thread_id)
                continue
        try:
            registry.clear(repo_name, thread_id)
        except Exception as exc:
            logger.error(f"Could not retire reconciled rollback transition for thread {thread_id} on PR #{pr_number}: {exc}")
            still_blocked.append(thread_id)

    for thread_id, root_comment_database_id in all_pending.items():
        try:
            registry.record_rollback_transition(repo_name, pr_number, thread_id, root_comment_database_id)
        except Exception as exc:
            raise StaleReviewThreadRegistryError(f"Could not durably prepare stale rollback of thread {thread_id} on PR #{pr_number}: {exc}") from exc

        try:
            github_client.unresolve_review_thread(thread_id)
        except Exception as exc:
            logger.error(f"Retry to revert stale resolution of thread {thread_id} on PR #{pr_number} failed: {exc}")
            try:
                registry.record(repo_name, pr_number, thread_id)
            except Exception as persist_exc:
                logger.error(f"Could not restore rollback-required state for thread {thread_id} on PR #{pr_number}; the fail-closed rollback transition remains: {persist_exc}")
            still_blocked.append(thread_id)
            continue

        if root_comment_database_id is not None:
            try:
                github_client.reply_to_review_thread(repo_name, pr_number, root_comment_database_id, STALE_BLOCKER_CLEARED_MARKER)
            except Exception as exc:
                logger.error(f"Reverted thread {thread_id} on PR #{pr_number} but could not post the cleared marker: {exc}")
                try:
                    registry.record_marker_cleanup(repo_name, pr_number, thread_id, root_comment_database_id)
                except Exception as persist_exc:
                    logger.error(f"Could not record marker cleanup for thread {thread_id} on PR #{pr_number}; the fail-closed rollback transition remains: {persist_exc}")
                still_blocked.append(thread_id)
                continue
        try:
            registry.clear(repo_name, thread_id)
        except Exception as exc:
            logger.error(f"Reverted thread {thread_id} on PR #{pr_number} but could not clear its local registry entry: {exc}")
            still_blocked.append(thread_id)
    return still_blocked


_BLOCKER_ID_RE = re.compile(r"(?:Blocker identity:\s*`?([^`\s\n]+)`?|blocker_id[=:]\s*`?([^`\s\n]+)`?)", re.IGNORECASE)
_GAP_ID_RE = re.compile(r"(?:Gap identity:\s*`?([^`\s\n]+)`?|gap_id[=:]\s*`?([^`\s\n]+)`?|TEST_ORACLE_GAP\s+([a-zA-Z0-9_-]+))", re.IGNORECASE)


@dataclass(frozen=True)
class ClaimedReviewThread:
    """One unresolved review thread eligible for independent adjudication."""

    thread_id: str = ""
    root_comment_database_id: Optional[int] = None
    root_author_login: str = ""
    original_finding: str = ""
    discussion: str = ""
    is_change_provenance: bool = False
    claim_evidence: str = ""
    revalidation_after_head_change: bool = False
    revalidation_forced: bool = False
    blocker_ids: tuple[str, ...] = ()
    concern_ids: tuple[str, ...] = ()
    authoritative_boundary: str = ""
    category: str = ""


@dataclass(frozen=True)
class ReviewThreadClassification:
    """Result of splitting unresolved review threads into claimed vs. blocking."""

    claimed: Sequence[ClaimedReviewThread] = field(default_factory=tuple)
    blocking_unresolved_count: int = 0


def classify_review_threads(
    threads: Iterable[ReviewThread],
    eligible_author_ids: Set[int],
    *,
    snapshot: Optional[Any] = None,
) -> ReviewThreadClassification:
    """Split unresolved review threads into claimed-addressed and ordinary blockers.

    A thread is "claimed" only when its root (first) comment was authored by a
    stable GitHub identity ID in ``eligible_author_ids`` AND at least one *reply after the
    root* carries the explicit Auto-Coder addressed marker (REQ-001). The root
    comment itself is excluded from marker detection: it is the original
    review finding, not an implementation-agent claim, so a reviewer that
    happens to quote or emit the marker text in its finding must never make
    its own thread look claimed (AC-011). Every other unresolved thread —
    including one where an ineligible/human thread merely contains a copied
    marker string, or where the full discussion could not be retrieved
    (``comments_truncated``) — counts as an ordinary blocker (REQ-008 fails
    closed on incomplete evidence).
    """
    claimed: List[ClaimedReviewThread] = []
    blocking_unresolved_count = 0

    for thread in threads:
        if thread.is_resolved:
            continue

        comments = thread.comments or []
        if not comments or thread.comments_truncated:
            blocking_unresolved_count += 1
            continue

        root = comments[0]
        is_eligible = root.author_id is not None and root.author_id in eligible_author_ids
        claim_comments = [comment for comment in comments[1:] if reply_claims_review_addressed(comment.body)]
        has_claim = bool(claim_comments)

        if is_eligible and has_claim:
            discussion = "\n\n".join(f"{comment.author_login or '(unknown author)'}: {comment.body}" for comment in comments)
            blocker_ids: list[str] = []
            concern_ids: list[str] = []
            authoritative_boundary = ""
            category = ""

            if snapshot is not None:
                root_id_str = str(root.database_id) if root.database_id is not None else None
                matching = []
                if root_id_str:
                    matching.extend(snapshot.get_blockers_for_alias("github_root_comment", root_id_str))
                    matching.extend(snapshot.get_blockers_for_alias("root_comment_id", root_id_str))
                matching.extend(snapshot.get_blockers_for_alias("github_thread", thread.id))
                for b in matching:
                    if b.blocker_id not in blocker_ids:
                        blocker_ids.append(b.blocker_id)
                    if b.authoritative_boundary and not authoritative_boundary:
                        authoritative_boundary = b.authoritative_boundary
                    if b.category and not category:
                        category = b.category
                    for c in b.concern_ids or b.accepted_scope.concern_ids:
                        if c not in concern_ids:
                            concern_ids.append(c)

            if not blocker_ids:
                match = _BLOCKER_ID_RE.search(root.body)
                if match:
                    bid = match.group(1) or match.group(2)
                    if bid and bid not in blocker_ids:
                        blocker_ids.append(bid)
                gap_match = _GAP_ID_RE.search(root.body)
                if gap_match:
                    category = "TEST_ORACLE"

            claimed.append(
                ClaimedReviewThread(
                    thread_id=thread.id,
                    root_comment_database_id=root.database_id,
                    root_author_login=root.author_login,
                    original_finding=root.body,
                    discussion=discussion,
                    is_change_provenance=CHANGE_PROVENANCE_CLARIFICATION_MARKER in root.body,
                    claim_evidence="\n\n".join(f"{comment.author_login or '(unknown author)'}: {comment.body}" for comment in claim_comments),
                    blocker_ids=tuple(blocker_ids),
                    concern_ids=tuple(concern_ids),
                    authoritative_boundary=authoritative_boundary,
                    category=category,
                )
            )
        else:
            blocking_unresolved_count += 1

    return ReviewThreadClassification(claimed=tuple(claimed), blocking_unresolved_count=blocking_unresolved_count)


def is_change_provenance_thread(thread: ReviewThread) -> bool:
    """Return whether an unresolved thread is an implementer provenance question."""
    return bool(thread.comments and CHANGE_PROVENANCE_CLARIFICATION_MARKER in thread.comments[0].body)


def change_provenance_reply_fingerprint(claimed: Sequence[ClaimedReviewThread]) -> str:
    """Return a durable marker for the exact provenance replies reviewed this run."""
    discussions = [f"{thread.thread_id}\n{thread.claim_evidence}" for thread in claimed if thread.is_change_provenance]
    if not discussions:
        return ""
    digest = hashlib.sha256("\n\n".join(sorted(discussions)).encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"<!-- auto-coder-change-provenance-evidence:v1:{digest} -->"


def render_claimed_review_threads_section(claimed: Sequence[ClaimedReviewThread]) -> str:
    """Render the evidence block the adversarial-validation prompt injects.

    Only threads listed here may receive a ``thread_dispositions`` entry; the
    prompt instructs the validator not to invent dispositions for any other
    thread ID (REQ-001, REQ-002).
    """
    if not claimed:
        return "(No claimed-addressed review threads for this run.)"

    blocks = []
    for thread in claimed:
        if thread.is_change_provenance:
            heading = "Change-provenance clarification"
            discussion_label = "Full thread discussion (chronological, includes the implementation-agent addressed claim and rationale):"
        elif thread.revalidation_after_head_change:
            heading = "Older-head adversarial finding requiring revalidation"
            discussion_label = "Full thread discussion (chronological; no implementation-agent reply is required for this revalidation):"
        elif thread.revalidation_forced:
            heading = "Forced adversarial-validation revalidation (explicit --force)"
            discussion_label = "Full thread discussion (chronological; this is an explicit forced same-head " "revalidation, not evidence that the head changed or that an implementer replied; " "no implementation-agent reply is required):"
        else:
            heading = "Claimed-addressed review thread"
            discussion_label = "Full thread discussion (chronological, includes the implementation-agent addressed claim and rationale):"

        lines = [f"### {heading}: {thread.thread_id}"]
        if thread.blocker_ids:
            lines.append(f"Canonical blocker identity: {', '.join(thread.blocker_ids)}")
        if thread.category:
            lines.append(f"Finding category: {thread.category}")
        if thread.authoritative_boundary:
            lines.append(f"Authoritative production boundary: {thread.authoritative_boundary}")
        if thread.concern_ids:
            lines.append(f"Owned concrete concern IDs: {', '.join(thread.concern_ids)}")
        lines.extend(
            [
                f"Original review finding (thread root, author: {thread.root_author_login or 'unknown'}):",
                thread.original_finding or "(empty)",
                "",
                discussion_label,
                thread.discussion or "(empty)",
            ]
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _find_claimed_thread(claimed: Sequence[ClaimedReviewThread], thread_id: str) -> Optional[ClaimedReviewThread]:
    for thread in claimed:
        if thread.thread_id == thread_id:
            return thread
    return None


def _format_resolver_explanation(disposition: ReviewThreadDisposition) -> str:
    return "\n".join(
        [
            RESOLVER_EXPLANATION_MARKER,
            "### Auto-Coder independent validator verification",
            "",
            "This finding was independently verified as addressed against the current PR head.",
            "",
            "**Rationale**",
            "",
            disposition.rationale or "(no rationale provided)",
            "",
            "**Evidence**",
            "",
            disposition.evidence or "(no evidence provided)",
        ]
    )


def resolve_addressed_review_threads(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    validated_head_sha: str,
    claimed: Sequence[ClaimedReviewThread],
    dispositions: Sequence[ReviewThreadDisposition],
    stale_registry: Optional[StaleReviewThreadRegistry] = None,
    *,
    ledger: Optional[Any] = None,
    api_origin: str = "https://api.github.com",
    base_sha: str = "",
    requirement_manifest_revision: str = "",
    review_attempt_id: str = "",
    expected_ledger_revision: Optional[int] = None,
    known_absent_apis: tuple[str, ...] = (),
) -> Any:
    """Resolve every thread the validator confirmed ADDRESSED, fail-closed.

    Implements REQ-001 through REQ-010: resolution requires an exact,
    well-formed ADDRESSED disposition for a thread that was actually claimed
    for this run, evaluated against its authenticated canonical blocker scope
    and boundary, durably persisted in the canonical blocker ledger before
    executing GitHub mutations, with compound root gating and CAS/staleness
    fencing.

    Returns the complete per-thread closure report.  The report remains iterable
    over confirmed thread IDs for compatibility with rollback/publication code.
    """
    from .pr_blocker_closure import (
        ClosureExecutionResult,
        ThreadClosureOutcome,
        adjudicate_claimed_thread_closures,
        execute_durable_thread_closures,
        extract_closure_candidates,
    )
    from .util.github_request_outcome import normalize_api_origin

    snapshot = None
    if ledger is not None:
        try:
            snapshot = ledger.get_snapshot(normalize_api_origin(api_origin), repo_name, pr_number, require_retained_state=True)
        except Exception as exc:
            logger.error(f"Could not load canonical blocker ledger snapshot: {exc}")

    candidates = extract_closure_candidates(
        claimed,
        snapshot=snapshot,
        api_origin=api_origin,
        repository=repo_name,
        pr_number=pr_number,
        reviewed_head_sha=validated_head_sha,
        reviewed_base_sha=base_sha,
        requirement_manifest_revision=requirement_manifest_revision,
        review_attempt_id=review_attempt_id,
        known_absent_apis=known_absent_apis,
    )

    evaluations = adjudicate_claimed_thread_closures(candidates, dispositions, snapshot=snapshot)

    addressed_thread_ids = {disposition.thread_id for disposition in dispositions if disposition.status == "ADDRESSED"}
    if addressed_thread_ids:
        exec_result = execute_durable_thread_closures(
            github_client=github_client,
            repo_name=repo_name,
            pr_number=pr_number,
            validated_head_sha=validated_head_sha,
            evaluations=evaluations,
            ledger=ledger,
            api_origin=api_origin,
            stale_registry=stale_registry,
            expected_ledger_revision=expected_ledger_revision,
            claimed_threads=claimed,
        )
    else:
        exec_result = ClosureExecutionResult(
            unresolved_thread_ids=tuple(dict.fromkeys(thread.thread_id for thread in claimed)),
        )

    outcomes: list[ThreadClosureOutcome] = []
    returned_disposition_ids = {disposition.thread_id for disposition in dispositions}
    for thread in claimed:
        owned = tuple(e for e in evaluations if e.thread_id == thread.thread_id)
        decisions = tuple(dict.fromkeys(e.effective_status for e in owned))
        decision = "MISSING" if thread.thread_id not in returned_disposition_ids else decisions[0] if len(decisions) == 1 else "INCONCLUSIVE"
        accepted = bool(owned) and all(e.is_accepted for e in owned)
        completed = thread.thread_id in exec_result.resolved_thread_ids
        thread_errors = [error for error in exec_result.errors if error.startswith(f"{thread.thread_id}|")]
        phase = "complete" if completed else "disposition"
        reason = "Thread resolution confirmed"
        cleanup_warning = None
        if thread_errors:
            _, phase, reason = thread_errors[-1].split("|", 2)
            cleanup = next((error.split("|", 2)[2] for error in thread_errors if "|marker-cleanup|" in error), None)
            cleanup_warning = cleanup
        elif not owned:
            reason = "No valid disposition was returned for the selected thread"
        elif not accepted:
            rejected = next((e for e in owned if not e.is_accepted), owned[0])
            phase = "independent-decision"
            reason = rejected.rejection_reason or rejected.rationale or f"Independent disposition is {rejected.effective_status}"
        elif not completed:
            phase = "effect-confirmation"
            reason = exec_result.errors[-1] if exec_result.errors else "GitHub resolution was not confirmed"

        outcomes.append(
            ThreadClosureOutcome(
                repository=repo_name,
                pr_number=pr_number,
                thread_id=thread.thread_id,
                evaluated_head_sha=validated_head_sha,
                review_attempt_id=owned[0].candidate.review_attempt_id if owned else review_attempt_id,
                root_comment_database_id=thread.root_comment_database_id,
                blocker_ids=tuple(dict.fromkeys(e.evaluated_blocker_id for e in owned if e.evaluated_blocker_id)),
                decision=decision,
                acceptance_state="CONFIRMED" if accepted else "NOT_ACCEPTED",
                effect_state="CONFIRMED" if completed else "NOT_ATTEMPTED" if not accepted else "UNCONFIRMED",
                phase=phase,
                reason=reason,
                cleanup_warning=cleanup_warning,
            )
        )
    return ClosureExecutionResult(
        accepted_closures=exec_result.accepted_closures,
        resolved_thread_ids=exec_result.resolved_thread_ids,
        unresolved_thread_ids=tuple(outcome.thread_id for outcome in outcomes if not outcome.completed),
        persisted_blocker_transitions=exec_result.persisted_blocker_transitions,
        errors=exec_result.errors,
        thread_outcomes=tuple(outcomes),
    )


def reopen_review_threads_after_publication_failure(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    claimed: Sequence[ClaimedReviewThread],
    resolved_thread_ids: Sequence[str],
    registry: Optional[StaleReviewThreadRegistry] = None,
) -> List[str]:
    """Reopen threads resolved by a verdict that failed durable publication.

    A durable rollback transition is recorded before each mutation. If the
    immediate rollback cannot be confirmed, the normal startup reconciliation
    path will retry it on later processing cycles.
    """
    rollback_registry = registry or StaleReviewThreadRegistry()
    reopened: List[str] = []
    for thread_id in resolved_thread_ids:
        claimed_thread = _find_claimed_thread(claimed, thread_id)
        root_comment_database_id = claimed_thread.root_comment_database_id if claimed_thread is not None else None
        try:
            rollback_registry.record_rollback_transition(
                repo_name,
                pr_number,
                thread_id,
                root_comment_database_id,
            )
        except Exception as exc:
            logger.error(f"Could not durably prepare publication-failure rollback for thread {thread_id} on PR #{pr_number}: {exc}")

        last_exc: Optional[Exception] = None
        for attempt in range(1, UNRESOLVE_ROLLBACK_MAX_ATTEMPTS + 1):
            try:
                github_client.unresolve_review_thread(thread_id)
                last_exc = None
                reopened.append(thread_id)
                break
            except Exception as exc:
                last_exc = exc
                logger.error(f"Attempt {attempt}/{UNRESOLVE_ROLLBACK_MAX_ATTEMPTS} to reopen thread {thread_id} after review publication failure on PR #{pr_number} failed: {exc}")

        if last_exc is not None:
            try:
                rollback_registry.record(repo_name, pr_number, thread_id)
            except Exception as persist_exc:
                logger.error(f"Could not persist publication-failure rollback for thread {thread_id} on PR #{pr_number}: {persist_exc}")
            continue

        try:
            rollback_registry.clear(repo_name, thread_id)
        except Exception as exc:
            # GitHub already confirms the thread is unresolved. Keeping the
            # transition merely causes fail-closed reconciliation next cycle.
            logger.error(f"Reopened thread {thread_id} on PR #{pr_number} but could not retire its rollback transition: {exc}")
    return reopened
