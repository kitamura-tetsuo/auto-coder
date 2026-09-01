"""Shared marker for a cloud implementation agent's "addressed" claim on a PR review thread.

Auto-Coder can delegate an unresolved GitHub PR review thread to a cloud
implementation agent. The agent must fix the reported problem and explain why
it believes the finding has been addressed, but it must never resolve the
GitHub review thread itself: that decision belongs to a later, independent
validation pass. To let that later pass distinguish an explicit
implementation-complete claim from ordinary discussion (e.g. "I think this is
fixed"), the agent's reply must contain this stable, versioned marker.
"""

import re

REVIEW_ADDRESSED_MARKER_VERSION = "v1"

REVIEW_ADDRESSED_MARKER = f"<!-- auto-coder-review-addressed:{REVIEW_ADDRESSED_MARKER_VERSION} -->"

_REVIEW_ADDRESSED_MARKER_RE = re.compile(r"<!--\s*auto-coder-review-addressed:[^>]+-->")


def reply_claims_review_addressed(body: str) -> bool:
    """Return whether a review-thread reply carries the Auto-Coder addressed marker.

    Ordinary natural-language phrases such as "fixed" or "done" never count;
    only the presence of the explicit, versioned marker does.
    """
    if not isinstance(body, str):
        return False
    return bool(_REVIEW_ADDRESSED_MARKER_RE.search(body))
