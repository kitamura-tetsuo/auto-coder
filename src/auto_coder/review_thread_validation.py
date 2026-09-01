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

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Sequence, Set

from .adversarial_validator import ReviewThreadDisposition
from .logger_config import get_logger
from .review_feedback_marker import reply_claims_review_addressed
from .util.gh_cache import ReviewThread

logger = get_logger(__name__)

RESOLVER_EXPLANATION_MARKER = "<!-- auto-coder-review-thread-resolved:v1 -->"

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
                # a log-and-continue outcome (REQ-006, REQ-008) — raise so the
                # caller treats this run as blocked rather than merging with
                # an incorrectly resolved thread.
                raise StaleReviewThreadResolutionError(thread_id, repo_name, pr_number) from last_exc
            return resolved

        resolved.append(thread_id)

    return resolved
