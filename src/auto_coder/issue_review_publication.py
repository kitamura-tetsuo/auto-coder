"""Route Issue validation findings comments through the dedicated reviewer App.

``SpecificationValidationLifecycle`` and ``DecompositionValidationLifecycle``
both publish their BLOCKED findings comments through this module rather than
the ordinary GitHub credential (Issue #2026). The dedicated reviewer App,
authenticated via ``github_app_reviewer.publish_issue_review``, is the sole
authoritative author of a confirmed findings comment: confirmation requires
both a marker match and an authenticated App-identity match on the comment
author, never a body substring alone (REQ-005).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Mapping, Optional

from .github_app_reviewer import ReviewerAppIdentity, publish_issue_review
from .util.github_request_outcome import GitHubRequestError


@dataclass(frozen=True)
class PublicationReceipt:
    """Durable evidence that the reviewer App authored one confirmed comment."""

    comment_id: int
    publisher_login: str
    publisher_app_id: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @staticmethod
    def from_dict(value: object) -> Optional["PublicationReceipt"]:
        if not isinstance(value, dict):
            return None
        comment_id = value.get("comment_id")
        publisher_login = value.get("publisher_login")
        publisher_app_id = value.get("publisher_app_id")
        if not isinstance(comment_id, int) or not isinstance(publisher_login, str) or not isinstance(publisher_app_id, int):
            return None
        return PublicationReceipt(comment_id, publisher_login, publisher_app_id)


def _comment_author_matches(comment: Mapping[str, object], identity: ReviewerAppIdentity) -> bool:
    """Verify GitHub-authoritative authorship metadata, not a display name."""
    user = comment.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    if not identity.matches_login(login if isinstance(login, str) else None):
        return False
    via_app = comment.get("performed_via_github_app")
    via_app_id = via_app.get("id") if isinstance(via_app, dict) else None
    # A present-but-mismatched App id is a definite conflict; its absence on an
    # otherwise-matching login is not itself proof (some GitHub responses omit
    # the field), so it is not treated as a mismatch on its own.
    if via_app is not None and via_app_id != identity.app_id:
        return False
    return True


def find_confirmed_publication(
    comments: Iterable[object],
    marker: str,
    expected_body: str,
    identity: ReviewerAppIdentity,
) -> tuple[Optional[PublicationReceipt], bool]:
    """Authoritatively decide whether ``marker`` was already confirmed-published.

    Returns ``(receipt, conflicting)``: ``receipt`` is set only when a comment
    carries exactly the expected outgoing body and is authored by the
    resolved reviewer App identity. ``conflicting`` is set when a comment
    carries the marker but fails that check (a copied body, a different
    actor, or a divergent report for the same decision) so the caller can
    report an explicit unconfirmed conflict instead of silently overwriting
    or reposting it (REQ-005, AS-002).
    """
    conflicting = False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str) or marker not in body:
            continue
        comment_id = comment.get("id")
        if body == expected_body and isinstance(comment_id, int) and _comment_author_matches(comment, identity):
            return PublicationReceipt(comment_id, identity.login, identity.app_id), False
        conflicting = True
    return None, conflicting


def publish_findings_comment(
    repo_name: str,
    issue_number: int,
    body: str,
    authorize_fn: Callable[[], bool],
) -> PublicationReceipt:
    """Publish one findings comment through the dedicated reviewer App.

    Never falls back to the ordinary GitHub credential or another App
    (REQ-002): a missing configuration, denied permission, a refused
    pre-send authorization, or a transport failure all surface as a typed
    ``GitHubRequestError`` carrying the original ``GitHubRequestOutcome``, so
    the caller's existing durable-retry handling applies unchanged
    (REQ-006).
    """
    result = publish_issue_review(repo_name, issue_number, body, authorize_fn)
    if result.confirmed_comment_id is None or result.identity is None:
        raise GitHubRequestError(result.outcome)
    return PublicationReceipt(result.confirmed_comment_id, result.identity.login, result.identity.app_id)
