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

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Set

from .adversarial_validator import ReviewThreadDisposition
from .logger_config import get_logger
from .review_feedback_marker import reply_claims_review_addressed
from .util.gh_cache import ReviewThread

logger = get_logger(__name__)

RESOLVER_EXPLANATION_MARKER = "<!-- auto-coder-review-thread-resolved:v1 -->"

# Posted into the affected GitHub thread itself as an independent durability
# layer for a stale-resolution blocker: unlike the local JSON registry, a
# GitHub comment survives a local disk failure or process restart, because
# it lives in a different failure domain (REQ-006, REQ-008).
STALE_BLOCKER_MARKER = "<!-- auto-coder-stale-review-thread-blocker:v1 -->"
STALE_BLOCKER_CLEARED_MARKER = "<!-- auto-coder-stale-review-thread-blocker-cleared:v1 -->"

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
    """A durable record that a thread is resolved against a stale head.

    Persisted so the integrity failure survives across separate
    ``_handle_pr_merge`` invocations (and process restarts): an in-memory
    exception alone only blocks the single run that discovered it, but the
    underlying GitHub thread remains incorrectly resolved until a later run
    successfully rolls it back (REQ-006, REQ-008).
    """

    repository: str = ""
    pr_number: int = 0
    thread_id: str = ""


class StaleReviewThreadRegistry:
    """Persistent store of unresolved stale-resolution rollback failures."""

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

    def clear(self, repository: str, thread_id: str) -> None:
        """Remove a blocker once GitHub confirms the thread is unresolved."""
        with self._lock:
            data = self._load()
            key = self._key(repository, thread_id)
            if key not in data:
                return
            del data[key]
            self._save(data)

    def pending_for_pr(self, repository: str, pr_number: int) -> List[str]:
        """Return every still-pending stale thread ID for this PR.

        Every entry in the file is validated structurally, not just entries
        matching this PR: a syntactically valid JSON object containing a
        malformed entry (missing/mistyped fields, e.g. from a bug or manual
        edit) could otherwise silently hide a real blocker for this or any
        other PR, which is exactly the false-success this registry exists
        to prevent (REQ-006, REQ-008).
        """
        with self._lock:
            data = self._load()
        thread_ids: List[str] = []
        for key, value in data.items():
            if not isinstance(value, dict) or not isinstance(value.get("repository"), str) or not isinstance(value.get("pr_number"), int) or isinstance(value.get("pr_number"), bool) or not isinstance(value.get("thread_id"), str) or not value.get("thread_id"):
                raise StaleReviewThreadRegistryError(f"Stale-review-thread registry at {self.path} contains a malformed entry for key {key!r}")
            if value["repository"] == repository and value["pr_number"] == pr_number:
                thread_ids.append(str(value["thread_id"]))
        return thread_ids


def _find_github_stale_blockers(github_client: Any, repo_name: str, pr_number: int) -> dict[str, Optional[int]]:
    """Scan every review thread for an unmatched ``STALE_BLOCKER_MARKER``.

    Returns the pending thread IDs (mapped to their root comment's database
    ID, for posting the cleared-marker reply later, when available). This is
    an independent durability layer that lives on GitHub rather than local
    disk: a marker reply survives a local registry write failure and a
    process restart because it is in a different failure domain. A thread
    counts as pending only if the marker appears with no later
    ``STALE_BLOCKER_CLEARED_MARKER`` reply in the same thread.

    This rediscovery pass is deliberately best-effort, not itself fail-closed:
    the local registry (see ``StaleReviewThreadRegistry``) is the primary,
    strictly fail-closed source of truth and already raises on its own
    corruption. A failure scanning GitHub here (network error, unexpected
    client) only means this extra rediscovery opportunity is skipped for
    this run, not that merge is blocked — the local registry's own
    guarantees are unaffected.
    """
    try:
        threads = github_client.get_pr_review_threads_strict(repo_name, pr_number)
        pending: dict[str, Optional[int]] = {}
        for thread in threads:
            comments = thread.comments or []
            marker_index = next((index for index, comment in enumerate(comments) if STALE_BLOCKER_MARKER in comment.body), None)
            if marker_index is None:
                continue
            cleared = any(STALE_BLOCKER_CLEARED_MARKER in comment.body for comment in comments[marker_index + 1 :])
            if cleared:
                continue
            root_comment_database_id = comments[0].database_id if comments else None
            pending[thread.id] = root_comment_database_id
        return pending
    except Exception as exc:
        logger.warning(f"Could not scan GitHub review threads for stale-resolution markers on PR #{pr_number}; relying on the local registry only this run: {exc}")
        return {}


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
    github_pending = _find_github_stale_blockers(github_client, repo_name, pr_number)

    all_pending: dict[str, Optional[int]] = dict.fromkeys(local_pending, None)
    all_pending.update(github_pending)

    still_blocked: List[str] = []
    for thread_id, root_comment_database_id in all_pending.items():
        try:
            github_client.unresolve_review_thread(thread_id)
        except Exception as exc:
            logger.error(f"Retry to revert stale resolution of thread {thread_id} on PR #{pr_number} failed: {exc}")
            still_blocked.append(thread_id)
            continue

        try:
            registry.clear(repo_name, thread_id)
        except Exception as exc:
            logger.error(f"Reverted thread {thread_id} on PR #{pr_number} but could not clear its local registry entry: {exc}")

        if root_comment_database_id is not None:
            try:
                github_client.reply_to_review_thread(repo_name, pr_number, root_comment_database_id, STALE_BLOCKER_CLEARED_MARKER)
            except Exception as exc:
                # Best effort: the GitHub-side marker staying unmatched would
                # only cause a harmless extra retry next run, never a missed
                # block (the local registry entry, if it existed, is already
                # cleared; the mutation itself is what matters for safety).
                logger.error(f"Reverted thread {thread_id} on PR #{pr_number} but could not post the cleared marker: {exc}")
    return still_blocked


@dataclass(frozen=True)
class ClaimedReviewThread:
    """One unresolved review thread with an explicit implementation-agent claim."""

    thread_id: str = ""
    root_comment_database_id: Optional[int] = None
    root_author_login: str = ""
    original_finding: str = ""
    discussion: str = ""


@dataclass(frozen=True)
class ReviewThreadClassification:
    """Result of splitting unresolved review threads into claimed vs. blocking."""

    claimed: Sequence[ClaimedReviewThread] = field(default_factory=tuple)
    blocking_unresolved_count: int = 0


def classify_review_threads(threads: Iterable[ReviewThread], eligible_logins: Set[str]) -> ReviewThreadClassification:
    """Split unresolved review threads into claimed-addressed and ordinary blockers.

    A thread is "claimed" only when its root (first) comment was authored by a
    login in ``eligible_logins`` (REQ-011) AND at least one *reply after the
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
        is_eligible = bool(root.author_login) and root.author_login in eligible_logins
        has_claim = any(reply_claims_review_addressed(comment.body) for comment in comments[1:])

        if is_eligible and has_claim:
            discussion = "\n\n".join(f"{comment.author_login or '(unknown author)'}: {comment.body}" for comment in comments)
            claimed.append(
                ClaimedReviewThread(
                    thread_id=thread.id,
                    root_comment_database_id=root.database_id,
                    root_author_login=root.author_login,
                    original_finding=root.body,
                    discussion=discussion,
                )
            )
        else:
            blocking_unresolved_count += 1

    return ReviewThreadClassification(claimed=tuple(claimed), blocking_unresolved_count=blocking_unresolved_count)


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
        blocks.append(
            "\n".join(
                [
                    f"### Claimed-addressed review thread: {thread.thread_id}",
                    f"Original review finding (thread root, author: {thread.root_author_login or 'unknown'}):",
                    thread.original_finding or "(empty)",
                    "",
                    "Full thread discussion (chronological, includes the implementation-agent addressed claim and rationale):",
                    thread.discussion or "(empty)",
                ]
            )
        )
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
            'The implementation-agent "addressed" claim on this thread was independently verified against the current PR head.',
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
) -> List[str]:
    """Resolve every thread the validator confirmed ADDRESSED, fail-closed.

    Implements REQ-006 through REQ-010: resolution requires an exact,
    well-formed ADDRESSED disposition for a thread that was actually claimed
    for this run, the PR head must still equal ``validated_head_sha`` at the
    moment of resolution, and both the explanation reply and the resolve
    mutation must independently succeed. Any failure leaves that single
    thread unresolved without affecting any other thread.

    Returns the thread IDs that were actually resolved.
    """
    addressed_thread_ids = {disposition.thread_id for disposition in dispositions if disposition.status == "ADDRESSED"}
    if not addressed_thread_ids:
        return []

    def _head_is_still_current() -> bool:
        """Re-read the PR's live head and confirm it still matches the
        validated head. Called both before starting and again immediately
        before each resolve mutation, since a new commit can land at any
        point during this loop (REQ-006, AC-009)."""
        try:
            current_pr = github_client.get_pull_request(repo_name, pr_number)
            current_head_sha = current_pr.get("head", {}).get("sha") if isinstance(current_pr, dict) else getattr(getattr(current_pr, "head", None), "sha", None)
        except Exception as exc:
            logger.error(f"Could not verify current PR head before resolving review threads on PR #{pr_number}: {exc}")
            return False
        if not validated_head_sha or not current_head_sha or current_head_sha != validated_head_sha:
            logger.warning(f"PR #{pr_number} head changed since adversarial validation (validated {validated_head_sha}, current {current_head_sha}); " "not resolving claimed review thread(s)")
            return False
        return True

    if not _head_is_still_current():
        return []

    resolved: List[str] = []
    for thread_id in addressed_thread_ids:
        claimed_thread = _find_claimed_thread(claimed, thread_id)
        if claimed_thread is None:
            logger.warning(f"Validator returned ADDRESSED for thread {thread_id} on PR #{pr_number}, which was not among the claimed threads for this run; ignoring")
            continue
        if claimed_thread.root_comment_database_id is None:
            logger.error(f"Cannot record resolver explanation for thread {thread_id} on PR #{pr_number}: no root comment ID available")
            continue

        disposition = next(d for d in dispositions if d.thread_id == thread_id and d.status == "ADDRESSED")
        try:
            github_client.reply_to_review_thread(repo_name, pr_number, claimed_thread.root_comment_database_id, _format_resolver_explanation(disposition))
        except Exception as exc:
            logger.error(f"Failed to record independent validator explanation for thread {thread_id} on PR #{pr_number}: {exc}")
            continue

        # Re-check immediately before the resolve mutation: a new commit can
        # land between the initial check (or the previous iteration's reply)
        # and this point.
        if not _head_is_still_current():
            return resolved

        try:
            github_client.resolve_review_thread(thread_id)
        except Exception as exc:
            logger.error(f"Failed to resolve review thread {thread_id} on PR #{pr_number} after recording its explanation: {exc}")
            continue

        # The head can still advance between the pre-mutation check above and
        # the mutation actually completing. Re-verify once more and, if the
        # head moved, revert the resolution rather than leave a thread
        # resolved against a disposition for a head that is no longer current
        # (REQ-006, AC-009).
        if not _head_is_still_current():
            last_exc: Optional[Exception] = None
            for attempt in range(1, UNRESOLVE_ROLLBACK_MAX_ATTEMPTS + 1):
                try:
                    github_client.unresolve_review_thread(thread_id)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.error(f"Attempt {attempt}/{UNRESOLVE_ROLLBACK_MAX_ATTEMPTS} to revert stale resolution of thread {thread_id} on PR #{pr_number} failed: {exc}")
            if last_exc is not None:
                # Every rollback attempt failed or was unconfirmed: the thread
                # is durably resolved against a stale head. This must not be
                # a log-and-continue outcome (REQ-006, REQ-008). Persist the
                # failure so it survives past this single run — an in-memory
                # exception alone would be forgotten on the very next
                # processing pass while the GitHub thread stays incorrectly
                # resolved — and raise so this run also blocks immediately.
                try:
                    (stale_registry or StaleReviewThreadRegistry()).record(repo_name, pr_number, thread_id)
                except Exception as persist_exc:
                    # A failure to persist the blocker must not turn into a
                    # silently-continuing ordinary exception: this run still
                    # has to block immediately below regardless of whether
                    # the record survives for a later run to see.
                    logger.error(f"Failed to persist stale-resolution blocker for thread {thread_id} on PR #{pr_number}; this run still blocks, but a later run may not remember this failure: {persist_exc}")
                # Independent durability layer: also mark the thread itself on
                # GitHub. Unlike the local registry, this survives a local
                # disk failure combined with a process restart, since it
                # lives in a different failure domain (REQ-006, REQ-008).
                if claimed_thread.root_comment_database_id is not None:
                    try:
                        github_client.reply_to_review_thread(repo_name, pr_number, claimed_thread.root_comment_database_id, STALE_BLOCKER_MARKER)
                    except Exception as marker_exc:
                        logger.error(f"Failed to post the GitHub-side stale-resolution marker for thread {thread_id} on PR #{pr_number}: {marker_exc}")
                raise StaleReviewThreadResolutionError(thread_id, repo_name, pr_number) from last_exc
            return resolved

        resolved.append(thread_id)

    return resolved
