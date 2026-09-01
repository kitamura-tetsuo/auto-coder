from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from auto_coder.adversarial_validator import ReviewThreadDisposition
from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER
from auto_coder.review_thread_validation import (
    STALE_BLOCKER_MARKER,
    UNRESOLVE_ROLLBACK_MAX_ATTEMPTS,
    ClaimedReviewThread,
    StaleReviewThreadRegistry,
    StaleReviewThreadRegistryError,
    StaleReviewThreadResolutionError,
    classify_review_threads,
    render_claimed_review_threads_section,
    resolve_addressed_review_threads,
    retry_pending_stale_review_thread_rollbacks,
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
    @pytest.fixture(autouse=True)
    def _isolate_default_stale_registry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

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
        client.get_pull_request_head_sha_strict.return_value = "sha1"
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == ["t1"]
        # Resolver explanation, the pre-resolve durability marker, and the
        # post-resolve cleared marker.
        assert client.reply_to_review_thread.call_count == 3
        client.resolve_review_thread.assert_called_once_with("t1")

    def test_still_valid_disposition_is_never_resolved(self):
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "sha1"
        claimed = [self._claimed()]
        dispositions = [self._disposition(status="STILL_VALID")]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_inconclusive_disposition_is_never_resolved(self):
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "sha1"
        claimed = [self._claimed()]
        dispositions = [self._disposition(status="INCONCLUSIVE")]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_head_changed_since_validation_blocks_resolution(self):
        """AC-009: the PR advanced to a new head before resolution runs."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "sha2-newer"
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.reply_to_review_thread.assert_not_called()
        client.resolve_review_thread.assert_not_called()

    def test_cached_h1_cannot_hide_authoritative_h2_before_resolution(self):
        """[P1] REQ-006/AC-009: the ordinary cached PR read may still expose
        H1, but only the uncached authoritative H2 may drive resolution."""
        client = MagicMock()
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        client.get_pull_request_head_sha_strict.return_value = "sha2-newer"
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.get_pull_request.assert_not_called()
        client.get_pull_request_head_sha_strict.assert_called_once_with("owner/repo", 1)
        client.resolve_review_thread.assert_not_called()

    def test_head_lookup_failure_fails_closed(self):
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = Exception("network error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_reply_failure_prevents_resolution(self):
        """AC-010: recording the explanation must succeed before resolving."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "sha1"
        client.reply_to_review_thread.side_effect = Exception("GitHub API error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_resolve_mutation_failure_leaves_thread_unresolved(self):
        """AC-010: the resolve mutation itself failing must not be masked as success."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "sha1"
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
        client.get_pull_request_head_sha_strict.return_value = "sha1"
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
        client.get_pull_request_head_sha_strict.assert_not_called()

    def test_two_independent_addressed_threads_both_resolved(self):
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "sha1"
        claimed = [self._claimed("t1", 1), self._claimed("t2", 2)]
        dispositions = [self._disposition("t1"), self._disposition("t2")]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert set(resolved) == {"t1", "t2"}
        assert client.resolve_review_thread.call_count == 2

    def test_head_advances_between_reply_and_resolve_mutation(self):
        """AC-009: the first head lookup passes (H1), but the PR advances to
        H2 before the resolve mutation — re-checked immediately before that
        mutation, not just once at the start of the loop."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = [
            "sha1",  # initial check before the loop
            "sha2-newer",  # re-check right before resolving
        ]
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.reply_to_review_thread.assert_called_once()
        client.resolve_review_thread.assert_not_called()

    def test_head_advances_between_resolve_mutation_and_post_check_reverts_it(self):
        """AC-009: the head is still current for the pre-mutation check and
        the resolve_review_thread() mutation itself succeeds, but a
        post-mutation re-check finds the head has since moved. The
        resolution must be reverted (unresolve_review_thread) and the thread
        must not be reported as resolved."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = [
            "sha1",  # initial check before the loop
            "sha1",  # pre-mutation re-check
            "sha2-newer",  # post-mutation re-check
        ]
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_called_once_with("t1")
        client.unresolve_review_thread.assert_called_once_with("t1")

    def test_rollback_retries_and_succeeds_on_a_later_attempt(self):
        """The first two unresolve attempts fail, the third succeeds: the
        thread is still not reported as resolved, and no exception escapes."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = [
            "sha1",
            "sha1",
            "sha2-newer",
        ]
        client.unresolve_review_thread.side_effect = [Exception("transient error 1"), Exception("transient error 2"), None]
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        assert client.unresolve_review_thread.call_count == 3

    def test_rollback_exhausted_retries_raises_stale_resolution_error(self):
        """[P1] Every rollback attempt fails: this must not be a log-and-continue
        outcome. The GitHub thread is durably resolved against a stale head,
        so the failure must surface as a blocking exception rather than being
        silently absorbed."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = [
            "sha1",
            "sha1",
            "sha2-newer",
        ]
        client.unresolve_review_thread.side_effect = Exception("persistent error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        with pytest.raises(StaleReviewThreadResolutionError) as exc_info:
            resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert exc_info.value.thread_id == "t1"
        assert client.unresolve_review_thread.call_count == UNRESOLVE_ROLLBACK_MAX_ATTEMPTS

    def test_exhausted_rollback_persists_a_blocker(self):
        """[P1] Exhausting rollback attempts must persist the failure, not
        just raise an in-memory exception, so it survives past this run."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = [
            "sha1",
            "sha1",
            "sha2-newer",
        ]
        client.unresolve_review_thread.side_effect = Exception("persistent error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]
        registry = MagicMock()

        with pytest.raises(StaleReviewThreadResolutionError):
            resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions, stale_registry=registry)

        registry.record.assert_called_once_with("owner/repo", 1, "t1")

    def test_exhausted_rollback_posts_a_github_side_durable_marker(self):
        """[P1] An independent GitHub-side marker must be posted too, so the
        blocker survives even a local registry write failure combined with
        a process restart."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = [
            "sha1",
            "sha1",
            "sha2-newer",
        ]
        client.unresolve_review_thread.side_effect = Exception("persistent error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        with pytest.raises(StaleReviewThreadResolutionError):
            resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        marker_calls = [call for call in client.reply_to_review_thread.call_args_list if "auto-coder-stale-review-thread-blocker:v1" in call.args[3]]
        assert len(marker_calls) == 1
        assert marker_calls[0].args[2] == 42  # root_comment_database_id

    def test_pre_resolve_marker_post_failure_skips_thread_without_resolving(self):
        """[P2] Durability-before-risk: if the pre-resolve intent marker
        itself cannot be posted, the thread must never enter the risky
        "resolved-but-possibly-stale" state in the first place — the resolve
        mutation must not even be attempted, and the thread stays unresolved
        (which the ordinary unresolved-thread gate already handles safely)."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "sha1"
        client.reply_to_review_thread.side_effect = [
            None,  # resolver explanation succeeds
            Exception("could not post durable pre-resolve marker"),
        ]
        claimed = [self._claimed()]
        dispositions = [self._disposition()]

        resolved = resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions)

        assert resolved == []
        client.resolve_review_thread.assert_not_called()

    def test_both_durable_writes_failing_still_survives_via_predresolve_marker(self, tmp_path, monkeypatch):
        """[P2] Regression oracle for the "both durable writes fail" scenario:
        the local registry write AND the post-rollback cleared-marker write
        both fail, but because the pre-resolve intent marker was posted
        successfully *before* the risky resolve mutation, a fresh process
        invocation (empty in-memory state, new registry) can still discover
        the durable GitHub-side record and refuse to treat the thread as
        settled."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = [
            "sha1",
            "sha1",
            "sha2-newer",
        ]
        client.unresolve_review_thread.side_effect = Exception("persistent error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]
        registry = MagicMock()
        registry.record.side_effect = OSError("disk full")

        with pytest.raises(StaleReviewThreadResolutionError):
            resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions, stale_registry=registry)

        # The pre-resolve durable marker was posted before the risky resolve
        # mutation, independent of the local registry write that failed above.
        marker_calls = [call for call in client.reply_to_review_thread.call_args_list if "auto-coder-stale-review-thread-blocker:v1" in call.args[3]]
        assert len(marker_calls) == 1

        # A fresh process invocation (new registry, no in-memory state) must
        # still discover the durable GitHub-side marker independently.
        fresh_registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        fresh_client = MagicMock()
        fresh_client.get_authenticated_user_login.return_value = "agent[bot]"
        fresh_client.get_pr_review_threads_strict.return_value = [
            _thread(
                "t1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=42),
                    _comment("agent[bot]", "<!-- auto-coder-stale-review-thread-blocker:v1 -->"),
                ],
            )
        ]
        fresh_client.unresolve_review_thread.side_effect = Exception("still failing")

        still_blocked = retry_pending_stale_review_thread_rollbacks(fresh_client, "owner/repo", 1, registry=fresh_registry)

        assert still_blocked == ["t1"]

    def test_exhausted_rollback_still_raises_even_if_persisting_the_blocker_fails(self):
        """[P1] A write failure while persisting the blocker (disk full,
        permissions, ...) must not turn into a silently-continuing ordinary
        exception — this run must still fail closed with
        StaleReviewThreadResolutionError."""
        client = MagicMock()
        client.get_pull_request_head_sha_strict.side_effect = [
            "sha1",
            "sha1",
            "sha2-newer",
        ]
        client.unresolve_review_thread.side_effect = Exception("persistent error")
        claimed = [self._claimed()]
        dispositions = [self._disposition()]
        registry = MagicMock()
        registry.record.side_effect = OSError("disk full")

        with pytest.raises(StaleReviewThreadResolutionError) as exc_info:
            resolve_addressed_review_threads(client, "owner/repo", 1, "sha1", claimed, dispositions, stale_registry=registry)

        assert exc_info.value.thread_id == "t1"


class TestStaleReviewThreadRegistry:
    def test_record_then_pending_for_pr(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.record("owner/repo", 42, "thread-1")

        assert registry.pending_for_pr("owner/repo", 42) == ["thread-1"]
        assert registry.pending_for_pr("owner/repo", 99) == []
        assert registry.pending_for_pr("other/repo", 42) == []

    def test_clear_removes_the_blocker(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.record("owner/repo", 42, "thread-1")
        registry.clear("owner/repo", "thread-1")

        assert registry.pending_for_pr("owner/repo", 42) == []

    def test_clear_missing_entry_is_a_no_op(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.clear("owner/repo", "thread-1")  # should not raise
        assert registry.pending_for_pr("owner/repo", 42) == []

    def test_multiple_threads_tracked_independently(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.record("owner/repo", 42, "thread-1")
        registry.record("owner/repo", 42, "thread-2")
        registry.clear("owner/repo", "thread-1")

        assert registry.pending_for_pr("owner/repo", 42) == ["thread-2"]

    def test_marker_cleanup_state_is_distinct_from_rollback_state(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.record_marker_cleanup("owner/repo", 42, "thread-1", 101)

        assert registry.pending_for_pr("owner/repo", 42) == []
        assert registry.marker_cleanup_pending_for_pr("owner/repo", 42) == {"thread-1": 101}

    def test_rollback_transition_is_distinct_and_fail_closed(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.record_rollback_transition("owner/repo", 42, "thread-1", 101)

        assert registry.pending_for_pr("owner/repo", 42) == []
        assert registry.marker_cleanup_pending_for_pr("owner/repo", 42) == {}
        assert registry.rollback_transitions_for_pr("owner/repo", 42) == ["thread-1"]

    def test_malformed_entry_fails_closed_even_for_a_different_pr(self, tmp_path):
        """[P1] A structurally malformed entry anywhere in the file (e.g.
        from a bug or manual edit) must never be silently skipped — it could
        be hiding a real blocker for this or any other PR."""
        path = tmp_path / "stale.json"
        path.write_text('{"k1": {"repository": "owner/repo", "pr_number": "not-an-int", "thread_id": "t1"}}', encoding="utf-8")
        registry = StaleReviewThreadRegistry(path=path)

        with pytest.raises(StaleReviewThreadRegistryError):
            registry.pending_for_pr("owner/repo", 42)

    def test_missing_thread_id_field_fails_closed(self, tmp_path):
        path = tmp_path / "stale.json"
        path.write_text('{"k1": {"repository": "owner/repo", "pr_number": 42}}', encoding="utf-8")
        registry = StaleReviewThreadRegistry(path=path)

        with pytest.raises(StaleReviewThreadRegistryError):
            registry.pending_for_pr("owner/repo", 42)

    def test_survives_across_registry_instances(self, tmp_path):
        """The blocker is durable: a fresh registry instance pointed at the
        same file still sees it (models a later, separate processing run)."""
        path = tmp_path / "stale.json"
        StaleReviewThreadRegistry(path=path).record("owner/repo", 42, "thread-1")

        assert StaleReviewThreadRegistry(path=path).pending_for_pr("owner/repo", 42) == ["thread-1"]

    def test_corrupt_json_fails_closed_instead_of_looking_empty(self, tmp_path):
        """[P1] An existing-but-unparseable registry file must never be
        silently treated as "no blockers" — it may be hiding a real one."""
        path = tmp_path / "stale.json"
        path.write_text("{not valid json", encoding="utf-8")
        registry = StaleReviewThreadRegistry(path=path)

        with pytest.raises(StaleReviewThreadRegistryError):
            registry.pending_for_pr("owner/repo", 42)

    def test_non_object_json_fails_closed(self, tmp_path):
        path = tmp_path / "stale.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        registry = StaleReviewThreadRegistry(path=path)

        with pytest.raises(StaleReviewThreadRegistryError):
            registry.pending_for_pr("owner/repo", 42)

    def test_unreadable_file_fails_closed(self, tmp_path):
        path = tmp_path / "stale.json"
        path.write_text('{"k": {"repository": "owner/repo", "pr_number": 42, "thread_id": "t1"}}', encoding="utf-8")
        registry = StaleReviewThreadRegistry(path=path)

        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            with pytest.raises(StaleReviewThreadRegistryError):
                registry.pending_for_pr("owner/repo", 42)

    def test_missing_file_is_legitimately_empty(self, tmp_path):
        """Only a genuinely absent file (never yet written) may return empty
        without raising — this is the sole exception to fail-closed."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "does-not-exist.json")
        assert registry.pending_for_pr("owner/repo", 42) == []


class TestRetryPendingStaleReviewThreadRollbacks:
    def test_dual_cleanup_failure_leaves_transition_that_never_unresolves_later_resolution(self, tmp_path):
        """[P2] CLEAR and cleanup persistence may both fail after rollback.
        The pre-mutation transition survives and blocks a fresh run without
        mutating a later legitimate resolution."""
        path = tmp_path / "stale.json"
        registry = StaleReviewThreadRegistry(path=path)
        first_client = MagicMock()
        first_client.get_pull_request_head_sha_strict.side_effect = ["sha1", "sha1", "sha2"]
        first_client.reply_to_review_thread.side_effect = [None, None, Exception("CLEAR failed")]
        claimed = [ClaimedReviewThread(thread_id="thread-1", root_comment_database_id=101)]
        dispositions = [ReviewThreadDisposition(thread_id="thread-1", status="ADDRESSED", rationale="fixed", evidence="verified")]

        with patch.object(registry, "record_marker_cleanup", side_effect=OSError("disk full")):
            resolved = resolve_addressed_review_threads(
                first_client,
                "owner/repo",
                42,
                "sha1",
                claimed,
                dispositions,
                stale_registry=registry,
            )

        assert resolved == []
        first_client.unresolve_review_thread.assert_called_once_with("thread-1")
        assert StaleReviewThreadRegistry(path=path).rollback_transitions_for_pr("owner/repo", 42) == ["thread-1"]

        fresh_client = MagicMock()
        fresh_client.get_authenticated_user_login.return_value = "auto-coder[bot]"
        fresh_client.get_pull_request_head_sha_strict.return_value = "sha3"
        fresh_client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=101),
                    _comment("auto-coder[bot]", f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: sha1 -->"),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(
            fresh_client,
            "owner/repo",
            42,
            registry=StaleReviewThreadRegistry(path=path),
        )

        assert still_blocked == ["thread-1"]
        fresh_client.unresolve_review_thread.assert_not_called()
        fresh_client.reply_to_review_thread.assert_not_called()

    def test_retry_path_dual_cleanup_failure_keeps_fail_closed_transition(self, tmp_path):
        """The GitHub-only retry path establishes the same transition before
        unresolving, so dual cleanup failure cannot erase all durable state."""
        path = tmp_path / "stale.json"
        registry = StaleReviewThreadRegistry(path=path)
        first_client = MagicMock()
        first_client.get_authenticated_user_login.return_value = "auto-coder[bot]"
        first_client.get_pull_request_head_sha_strict.return_value = "sha2"
        first_client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=101),
                    _comment("auto-coder[bot]", f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: sha1 -->"),
                ],
            )
        ]
        first_client.reply_to_review_thread.side_effect = Exception("CLEAR failed")

        with patch.object(registry, "record_marker_cleanup", side_effect=OSError("disk full")):
            still_blocked = retry_pending_stale_review_thread_rollbacks(first_client, "owner/repo", 42, registry=registry)

        assert still_blocked == ["thread-1"]
        first_client.unresolve_review_thread.assert_called_once_with("thread-1")
        assert StaleReviewThreadRegistry(path=path).rollback_transitions_for_pr("owner/repo", 42) == ["thread-1"]

    def test_failed_clear_after_rollback_cannot_unresolve_later_legitimate_resolution(self, tmp_path):
        """[P2] H1 resolve -> H2 -> successful rollback -> failed CLEAR.
        A fresh run seeing a later legitimate resolution must retry only the
        marker cleanup, never attach the old H1 intent to that resolution."""
        path = tmp_path / "stale.json"
        registry = StaleReviewThreadRegistry(path=path)
        first_client = MagicMock()
        first_client.get_pull_request_head_sha_strict.side_effect = ["sha1", "sha1", "sha2"]
        first_client.reply_to_review_thread.side_effect = [
            None,  # resolver explanation
            None,  # H1 blocker intent
            Exception("CLEAR write failed after confirmed rollback"),
        ]
        claimed = [ClaimedReviewThread(thread_id="thread-1", root_comment_database_id=101)]
        dispositions = [ReviewThreadDisposition(thread_id="thread-1", status="ADDRESSED", rationale="fixed", evidence="verified")]

        resolved = resolve_addressed_review_threads(
            first_client,
            "owner/repo",
            42,
            "sha1",
            claimed,
            dispositions,
            stale_registry=registry,
        )

        assert resolved == []
        first_client.unresolve_review_thread.assert_called_once_with("thread-1")
        assert registry.marker_cleanup_pending_for_pr("owner/repo", 42) == {"thread-1": 101}

        fresh_client = MagicMock()
        fresh_client.get_authenticated_user_login.return_value = "auto-coder[bot]"
        fresh_client.get_pull_request_head_sha_strict.return_value = "sha3"
        fresh_client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=101),
                    _comment("auto-coder[bot]", f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: sha1 -->"),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(
            fresh_client,
            "owner/repo",
            42,
            registry=StaleReviewThreadRegistry(path=path),
        )

        assert still_blocked == []
        fresh_client.unresolve_review_thread.assert_not_called()
        fresh_client.reply_to_review_thread.assert_called_once_with(
            "owner/repo",
            42,
            101,
            "<!-- auto-coder-stale-review-thread-blocker-cleared:v1 -->",
        )
        assert StaleReviewThreadRegistry(path=path).marker_cleanup_pending_for_pr("owner/repo", 42) == {}

    def test_marker_matching_current_head_is_not_stale_despite_missing_cleared_reply(self, tmp_path):
        """[P2] Regression oracle: a resolve succeeded on the still-current
        head, but the best-effort STALE_BLOCKER_CLEARED_MARKER reply that
        closes out the pre-resolve intent marker failed to post (e.g.
        transient API error) and merge was deferred for an unrelated reason.
        A later, fresh processing run must not call unresolve_review_thread()
        on this thread merely because the old pre-resolve marker remains --
        the marker records the head it was posted against, and since that
        head still equals the PR's current head, nothing has changed and the
        resolution is not actually stale."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pull_request_head_sha_strict.return_value = "sha1"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: sha1 -->"),
                    # The CLEARED reply never made it -- this is the only
                    # marker present.
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        assert client.get_pull_request_head_sha_strict.call_count == 2
        client.unresolve_review_thread.assert_not_called()

    def test_head_change_during_marker_scan_fails_closed_without_mutation(self, tmp_path):
        """[P1] H1 may match the marker at comparison time while H2 lands
        before the scan returns. The post-scan authoritative read must catch
        that race and prevent the H1 marker from being dismissed on H2."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pull_request_head_sha_strict.side_effect = ["sha1", "sha2"]
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: sha1 -->"),
                ],
            )
        ]

        with pytest.raises(StaleReviewThreadRegistryError, match="Could not scan GitHub review threads"):
            retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert client.get_pull_request_head_sha_strict.call_args_list == [
            call("owner/repo", 42),
            call("owner/repo", 42),
        ]
        client.unresolve_review_thread.assert_not_called()
        client.reply_to_review_thread.assert_not_called()

    def test_marker_with_stale_head_is_still_pending(self, tmp_path):
        """The head-aware comparison only suppresses staleness when nothing
        has changed. If a new commit landed after the marker was posted (the
        marker's recorded head no longer matches the PR's current head), the
        thread must still be treated as a genuine, confirmed blocker."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pull_request_head_sha_strict.return_value = "sha2-newer"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: sha1 -->"),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.unresolve_review_thread.assert_called_once_with("thread-1")

    def test_cached_h1_cannot_dismiss_h1_marker_when_authoritative_head_is_h2(self, tmp_path):
        """[P1] The stale-marker oracle must bypass a cached H1 PR response;
        live H2 makes the trusted H1 marker pending and triggers rollback."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pull_request.return_value = {"head": {"sha": "sha1"}}
        client.get_pull_request_head_sha_strict.return_value = "sha2-newer"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: sha1 -->"),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.get_pull_request.assert_not_called()
        assert client.get_pull_request_head_sha_strict.call_args_list == [
            call("owner/repo", 42),
            call("owner/repo", 42),
        ]
        client.unresolve_review_thread.assert_called_once_with("thread-1")

    def test_head_aware_marker_lookup_failure_fails_closed(self, tmp_path):
        """If the current-head lookup itself fails, the marker's staleness
        cannot be ruled out, so processing must stop without mutating the
        thread rather than silently skipping or guessing its state."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pull_request_head_sha_strict.side_effect = Exception("network error")
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", f"{STALE_BLOCKER_MARKER}\n<!-- resolved-against-head: sha1 -->"),
                ],
            )
        ]

        with pytest.raises(StaleReviewThreadRegistryError, match="Could not scan GitHub review threads"):
            retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        client.unresolve_review_thread.assert_not_called()

    def test_no_pending_blockers_is_a_no_op(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.unresolve_review_thread.assert_not_called()

    def test_successful_retry_clears_the_blocker(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.record("owner/repo", 42, "thread-1")
        client = MagicMock()  # unresolve_review_thread succeeds (no exception)
        client.get_pr_review_threads_strict.return_value = []

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        assert registry.pending_for_pr("owner/repo", 42) == []

    def test_failed_retry_keeps_the_blocker_pending(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.record("owner/repo", 42, "thread-1")
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        client.unresolve_review_thread.side_effect = Exception("still failing")

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == ["thread-1"]
        assert registry.pending_for_pr("owner/repo", 42) == ["thread-1"]

    def test_second_run_retries_and_succeeds_where_first_failed(self, tmp_path):
        """Models two separate processing runs: the first run's rollback
        failure is persisted; a later run's successful retry clears it."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        registry.record("owner/repo", 42, "thread-1")

        failing_client = MagicMock()
        failing_client.get_pr_review_threads_strict.return_value = []
        failing_client.unresolve_review_thread.side_effect = Exception("still failing")
        first_run_result = retry_pending_stale_review_thread_rollbacks(failing_client, "owner/repo", 42, registry=registry)
        assert first_run_result == ["thread-1"]

        succeeding_client = MagicMock()
        succeeding_client.get_pr_review_threads_strict.return_value = []
        second_run_result = retry_pending_stale_review_thread_rollbacks(succeeding_client, "owner/repo", 42, registry=registry)
        assert second_run_result == []
        assert registry.pending_for_pr("owner/repo", 42) == []

    def test_github_side_marker_alone_is_discovered_and_blocks(self, tmp_path):
        """[P1] A blocker that exists only as a GitHub marker reply (e.g. the
        local registry write failed) must still be discovered and retried,
        with no local registry state at all."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,  # the thread is (incorrectly) resolved on GitHub
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", "<!-- auto-coder-stale-review-thread-blocker:v1 -->"),
                ],
            )
        ]
        client.unresolve_review_thread.side_effect = Exception("still failing")

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == ["thread-1"]

    def test_github_side_marker_cleared_after_successful_retry(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", "<!-- auto-coder-stale-review-thread-blocker:v1 -->", database_id=2),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.reply_to_review_thread.assert_called_once_with("owner/repo", 42, 1, "<!-- auto-coder-stale-review-thread-blocker-cleared:v1 -->")

    def test_truncated_comments_with_no_visible_marker_fails_closed_without_mutating(self, tmp_path):
        """[P1] A truncated review-thread discussion can hide the marker on a
        page that was never fetched. The visible first page has no confirmed
        marker at all, so its actual state is unknown, not confirmed-blocked:
        merge processing must fail closed (via a raised
        StaleReviewThreadRegistryError), but the scan must never mutate this
        unrelated thread by calling unresolve_review_thread() on it merely
        because it was truncated."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [_comment(CODEX_LOGIN, "finding", database_id=1)],
                comments_truncated=True,
            )
        ]

        with pytest.raises(StaleReviewThreadRegistryError):
            retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        client.unresolve_review_thread.assert_not_called()

    def test_truncated_thread_with_visible_blocker_fails_closed_without_mutating(self, tmp_path):
        """[P2] A later, unfetched page may contain the matching CLEARED
        event, so a visible BLOCKER does not make truncated evidence complete
        and must not authorize an automatic unresolve."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", STALE_BLOCKER_MARKER),
                ],
                comments_truncated=True,
            )
        ]

        with pytest.raises(StaleReviewThreadRegistryError):
            retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        client.unresolve_review_thread.assert_not_called()

    def test_github_marker_scan_failure_fails_closed(self, tmp_path):
        """[P1] The GitHub-side marker scan may be the only surviving evidence
        of a stale resolution when the local registry write itself failed.
        A failure while performing that scan must propagate rather than be
        silently treated as "no marker-only blockers"."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_pr_review_threads_strict.side_effect = Exception("transient GitHub API error")

        with pytest.raises(StaleReviewThreadRegistryError):
            retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        client.unresolve_review_thread.assert_not_called()

    def test_blocker_cleared_blocker_sequence_remains_pending(self, tmp_path):
        """[P2] State must be derived chronologically: the latest marker wins.
        A thread blocked, then cleared, then blocked again by an unrelated
        later incident must still be reported pending, not cleared just
        because a CLEARED marker appears earlier in the discussion."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", "<!-- auto-coder-stale-review-thread-blocker:v1 -->"),
                    _comment("agent[bot]", "<!-- auto-coder-stale-review-thread-blocker-cleared:v1 -->"),
                    _comment("agent[bot]", "<!-- auto-coder-stale-review-thread-blocker:v1 -->"),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.unresolve_review_thread.assert_called_once_with("thread-1")

    def test_prose_quoting_cleared_marker_does_not_clear_a_real_blocker(self, tmp_path):
        """[P1] A real BLOCKER marker followed by ordinary prose that merely
        quotes the CLEARED marker text (e.g. review discussion explaining
        that "the CLEARED marker has not been posted yet") must not be
        mistaken for an actual machine-posted CLEARED marker. Only an exact,
        canonical marker comment may change blocker state."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", STALE_BLOCKER_MARKER),
                    _comment(
                        "kitamura-tetsuo",
                        "Note: the <!-- auto-coder-stale-review-thread-blocker-cleared:v1 --> marker has not been posted yet.",
                    ),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.unresolve_review_thread.assert_called_once_with("thread-1")

    def test_prose_quoting_blocker_marker_without_a_real_marker_does_not_unresolve(self, tmp_path):
        """[P1] Prose that merely quotes the BLOCKER marker text, with no
        actual machine-posted marker comment anywhere in the thread, must
        not trigger an automatic unresolve."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment(
                        "kitamura-tetsuo",
                        "Discussion mentioning <!-- auto-coder-stale-review-thread-blocker:v1 --> in prose, not as an actual marker comment.",
                    ),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.unresolve_review_thread.assert_not_called()

    def test_untrusted_exact_cleared_marker_does_not_clear_trusted_blocker(self, tmp_path):
        """[P1] Only the authenticated Auto-Coder identity may advance the
        durable marker state, even when another author copies the exact body."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "auto-coder[bot]"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("auto-coder[bot]", STALE_BLOCKER_MARKER),
                    _comment("implementation-agent[bot]", "<!-- auto-coder-stale-review-thread-blocker-cleared:v1 -->"),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.unresolve_review_thread.assert_called_once_with("thread-1")

    def test_untrusted_exact_blocker_marker_does_not_unresolve(self, tmp_path):
        """[P1] An unrelated author cannot create authoritative blocker
        state by copying the exact BLOCKER body."""
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "auto-coder[bot]"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("unrelated-human", STALE_BLOCKER_MARKER),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.unresolve_review_thread.assert_not_called()

    def test_github_marker_with_later_cleared_reply_is_not_pending(self, tmp_path):
        registry = StaleReviewThreadRegistry(path=tmp_path / "stale.json")
        client = MagicMock()
        client.get_authenticated_user_login.return_value = "agent[bot]"
        client.get_pr_review_threads_strict.return_value = [
            _thread(
                "thread-1",
                True,
                [
                    _comment(CODEX_LOGIN, "finding", database_id=1),
                    _comment("agent[bot]", "<!-- auto-coder-stale-review-thread-blocker:v1 -->"),
                    _comment("agent[bot]", "<!-- auto-coder-stale-review-thread-blocker-cleared:v1 -->"),
                ],
            )
        ]

        still_blocked = retry_pending_stale_review_thread_rollbacks(client, "owner/repo", 42, registry=registry)

        assert still_blocked == []
        client.unresolve_review_thread.assert_not_called()
