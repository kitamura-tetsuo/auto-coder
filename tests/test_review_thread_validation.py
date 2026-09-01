from unittest.mock import MagicMock

from auto_coder.adversarial_validator import ReviewThreadDisposition
from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER
from auto_coder.review_thread_validation import (
    ClaimedReviewThread,
    classify_review_threads,
    render_claimed_review_threads_section,
    resolve_addressed_review_threads,
)
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment

CODEX_LOGIN = "chatgpt-codex-connector[bot]"
REVIEWER_APP_LOGIN = "auto-coder-reviewer[bot]"
ELIGIBLE_LOGINS = {CODEX_LOGIN, REVIEWER_APP_LOGIN}


def _thread(thread_id, is_resolved, comments, comments_truncated=False):
    return ReviewThread(id=thread_id, is_resolved=is_resolved, comments=comments, comments_truncated=comments_truncated)


def _comment(author, body, database_id=1):
    return ReviewThreadComment(database_id=database_id, body=body, author_login=author)


class TestClassifyReviewThreads:
    def test_resolved_thread_is_ignored(self):
        threads = [_thread("t1", True, [_comment(CODEX_LOGIN, "finding")])]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert result.claimed == ()
        assert result.blocking_unresolved_count == 0

    def test_unresolved_eligible_thread_with_marker_is_claimed(self):
        threads = [
            _thread(
                "t1",
                False,
                [
                    _comment(CODEX_LOGIN, "The counter never resets on retry", database_id=42),
                    _comment("agent[bot]", f"Fixed by resetting the counter.\n{REVIEW_ADDRESSED_MARKER}"),
                ],
            )
        ]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert result.blocking_unresolved_count == 0
        assert len(result.claimed) == 1
        claimed = result.claimed[0]
        assert claimed.thread_id == "t1"
        assert claimed.root_comment_database_id == 42
        assert claimed.root_author_login == CODEX_LOGIN
        assert "counter never resets" in claimed.original_finding

    def test_unresolved_eligible_thread_without_marker_is_blocking(self):
        threads = [
            _thread(
                "t1",
                False,
                [
                    _comment(CODEX_LOGIN, "finding"),
                    _comment("agent[bot]", "I think this is fixed"),
                ],
            )
        ]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert result.claimed == ()
        assert result.blocking_unresolved_count == 1

    def test_unresolved_ineligible_human_thread_with_marker_is_blocking(self):
        """A human-authored thread cannot be auto-adjudicated even if it contains
        a copied marker string (REQ-011, AC-012)."""
        threads = [
            _thread(
                "t1",
                False,
                [
                    _comment("a-human-reviewer", "finding"),
                    _comment("agent[bot]", f"Fixed.\n{REVIEW_ADDRESSED_MARKER}"),
                ],
            )
        ]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert result.claimed == ()
        assert result.blocking_unresolved_count == 1

    def test_thread_with_no_comments_is_blocking(self):
        threads = [_thread("t1", False, [])]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert result.claimed == ()
        assert result.blocking_unresolved_count == 1

    def test_marker_in_root_finding_alone_does_not_claim_the_thread(self):
        """AC-011: the root comment is the original review finding, not an
        implementation-agent claim. An eligible reviewer that happens to
        quote/emit the marker in its own finding (with no addressed reply)
        must not make its own thread look claimed."""
        threads = [
            _thread(
                "t1",
                False,
                [
                    _comment(CODEX_LOGIN, f"The counter never resets on retry.\n{REVIEW_ADDRESSED_MARKER}"),
                ],
            )
        ]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert result.claimed == ()
        assert result.blocking_unresolved_count == 1

    def test_marker_in_root_with_no_reply_stays_blocking_even_with_replies(self):
        """Same as above but with subsequent non-claiming discussion: still
        no implementation-agent claim exists anywhere but the root."""
        threads = [
            _thread(
                "t1",
                False,
                [
                    _comment(CODEX_LOGIN, f"finding.\n{REVIEW_ADDRESSED_MARKER}"),
                    _comment("a-human", "looking into this"),
                ],
            )
        ]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert result.claimed == ()
        assert result.blocking_unresolved_count == 1

    def test_truncated_comment_list_fails_closed_to_blocking(self):
        """REQ-002/REQ-008: a thread whose full discussion could not be
        retrieved (>50 comments) must never be treated as claimed, even if
        the visible page contains an eligible root and an addressed marker."""
        threads = [
            _thread(
                "t1",
                False,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", f"Fixed.\n{REVIEW_ADDRESSED_MARKER}"),
                ],
                comments_truncated=True,
            )
        ]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert result.claimed == ()
        assert result.blocking_unresolved_count == 1

    def test_mixed_threads(self):
        threads = [
            _thread("resolved", True, [_comment(CODEX_LOGIN, "finding")]),
            _thread(
                "claimed",
                False,
                [_comment(CODEX_LOGIN, "finding", database_id=1), _comment("agent[bot]", f"Fixed.\n{REVIEW_ADDRESSED_MARKER}")],
            ),
            _thread("blocking", False, [_comment("human", "please fix this")]),
        ]
        result = classify_review_threads(threads, ELIGIBLE_LOGINS)
        assert [c.thread_id for c in result.claimed] == ["claimed"]
        assert result.blocking_unresolved_count == 1


class TestRenderClaimedReviewThreadsSection:
    def test_empty_returns_placeholder(self):
        assert render_claimed_review_threads_section([]) == "(No claimed-addressed review threads for this run.)"

    def test_renders_thread_id_and_content(self):
        thread = ClaimedReviewThread(
            thread_id="thread-abc",
            root_comment_database_id=1,
            root_author_login=CODEX_LOGIN,
            original_finding="The counter never resets",
            discussion="chatgpt-codex-connector[bot]: The counter never resets",
        )
        rendered = render_claimed_review_threads_section([thread])
        assert "thread-abc" in rendered
        assert "The counter never resets" in rendered


class TestResolveAddressedReviewThreads:
    def _claimed(self, thread_id="t1", root_comment_database_id=42):
        return ClaimedReviewThread(
            thread_id=thread_id,
            root_comment_database_id=root_comment_database_id,
            root_author_login=CODEX_LOGIN,
            original_finding="finding",
            discussion="discussion",
        )

    def _disposition(self, thread_id="t1", status="ADDRESSED"):
        return ReviewThreadDisposition(thread_id=thread_id, status=status, rationale="Verified the fix directly", evidence="Reproduced the original failing path; now passes")

    def test_addressed_thread_is_resolved(self):
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == ["t1"]
        client.reply_to_review_thread.assert_called_once()
        client.resolve_review_thread.assert_called_once_with("t1")

    def test_still_valid_disposition_is_never_resolved(self):
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        claimed = [self._claimed()]
        dispositions = [self._disposition(status="STILL_VALID")]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_inconclusive_disposition_is_never_resolved(self):
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        claimed = [self._claimed()]
        dispositions = [self._disposition(status="INCONCLUSIVE")]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_head_changed_since_validation_blocks_resolution(self):
        """AC-009: the PR advanced to a new head before resolution runs."""
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha2-newer"}}
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.reply_to_review_thread.assert_not_called()
        client.resolve_review_thread.assert_not_called()

    def test_head_lookup_failure_fails_closed(self):
        client = MagicMock()
        client.get_pull_request.side_effect = Exception("network error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_reply_failure_prevents_resolution(self):
        """AC-010: recording the explanation must succeed before resolving."""
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        client.reply_to_review_thread.side_effect = Exception("GitHub API error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_resolve_mutation_failure_leaves_thread_unresolved(self):
        """AC-010: the resolve mutation itself failing must not be masked as success."""
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        client.resolve_review_thread.side_effect = Exception("mutation rejected")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []

    def test_addressed_disposition_for_unclaimed_thread_is_ignored(self):
        """A disposition for a thread ID never listed as claimed must be ignored,
        never resolved (only threads the run actually offered for adjudication
        may be auto-resolved)."""
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        claimed: list[ClaimedReviewThread] = []
        dispositions = [self._disposition(thread_id="unknown-thread")]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_no_addressed_dispositions_short_circuits_without_head_lookup(self):
        client = MagicMock()
        claimed = [self._claimed()]
        dispositions = [self._disposition(status="STILL_VALID")]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.get_pull_request.assert_not_called()

    def test_two_independent_addressed_threads_both_resolved(self):
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        claimed = [self._claimed("t1", 1), self._claimed("t2", 2)]
        dispositions = [self._disposition("t1"), self._disposition("t2")]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert set(resolved) == {"t1", "t2"}
        assert client.resolve_review_thread.call_count == 2
