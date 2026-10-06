"""External review intake retains identities without inventing Issue contracts."""

from dataclasses import replace

import pytest

from auto_coder.canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
from auto_coder.external_review_feedback import retain_external_review_feedback
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment


def test_external_comment_identity_survives_edit_head_and_restart(tmp_path):
    path = tmp_path / "blockers.sqlite3"
    comment = ReviewThreadComment(database_id=4191399314, body="Revert nullable boolean defaults", author_login="human")
    thread = ReviewThread(id="external-thread", comments=[comment])
    ids = retain_external_review_feedback(CanonicalPRBlockerLedger(path), "owner/repo", 5467, thread, comment, "head-1")
    assert len(ids) == 1
    assert ids[0].startswith("blk_")
    edited = replace(comment, body="Please revert the unrelated default change")
    replay = retain_external_review_feedback(CanonicalPRBlockerLedger(path), "owner/repo", 5467, replace(thread, comments=[edited]), edited, "head-2")
    assert replay == ids
    snapshot = CanonicalPRBlockerLedger(path).get_snapshot("https://api.github.com", "owner/repo", 5467)
    assert len(snapshot.blockers) == 1
    blocker = snapshot.get_blocker(ids[0])
    assert blocker.accepted_scope.description == comment.body
    assert blocker.qualified_requirements == ()
    assert blocker.original_objective_anchor is None
    assert snapshot.get_blockers_for_alias("github_root_comment", "4191399314") == (blocker,)
    assert snapshot.get_blockers_for_alias("github_thread", "external-thread") == (blocker,)


def test_identical_text_distinct_comments_and_repositories_have_distinct_ids(tmp_path):
    ledger = CanonicalPRBlockerLedger(tmp_path / "blockers.sqlite3")
    first = ReviewThreadComment(database_id=1, body="Same feedback")
    second = replace(first, database_id=2)
    thread = ReviewThread(id="thread", comments=[first, second])
    first_ids = retain_external_review_feedback(ledger, "owner/repo", 1, thread, first, "head")
    second_ids = retain_external_review_feedback(ledger, "owner/repo", 1, thread, second, "head")
    other_ids = retain_external_review_feedback(ledger, "other/repo", 1, thread, first, "head")
    assert len(set(first_ids + second_ids + other_ids)) == 3
    snapshot = ledger.get_snapshot("https://api.github.com", "owner/repo", 1)
    assert tuple(b.blocker_id for b in snapshot.get_blockers_for_alias("github_thread", "thread")) == first_ids


@pytest.mark.parametrize("resolved,truncated,body", [(True, False, "Feedback"), (False, True, "Feedback"), (False, False, ""), (False, False, "### Auto-Coder adversarial finding\nmanaged")])
def test_ineligible_comments_are_not_admitted(tmp_path, resolved, truncated, body):
    comment = ReviewThreadComment(database_id=1, body=body)
    thread = ReviewThread(id="thread", is_resolved=resolved, comments_truncated=truncated, comments=[comment])
    ledger = CanonicalPRBlockerLedger(tmp_path / "blockers.sqlite3")
    assert retain_external_review_feedback(ledger, "owner/repo", 1, thread, comment, "head") == ()


def test_parallel_intake_allocates_one_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "blockers.sqlite3"
    comment = ReviewThreadComment(database_id=1, body="Preserve source-data semantics")
    thread = ReviewThread(id="thread", comments=[comment])

    def admit(_index):
        return retain_external_review_feedback(CanonicalPRBlockerLedger(path), "owner/repo", 1, thread, comment, "head")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(admit, range(8)))
    assert len(set(results)) == 1
    assert len(results[0]) == 1
    snapshot = CanonicalPRBlockerLedger(path).get_snapshot("https://api.github.com", "owner/repo", 1)
    assert len(snapshot.blockers) == 1
