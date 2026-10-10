"""Regression: a saved NEEDS_FIX review whose findings are attached threads must be delivered."""

from types import SimpleNamespace
from unittest.mock import patch

from src.auto_coder import pr_processor
from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment

FINDING = "### Auto-Coder adversarial finding\n\n**Violated requirement**\n\n`#1/REQ-001`: something specific that is broken"
REPORT = "## Auto-Coder adversarial validation: NEEDS_FIX\n\n2 actionable finding thread(s) are attached to this review.\n"


class _Client:
    def __init__(self, threads):
        self.threads = threads

    def get_pr_review_threads_strict(self, repo_name, pr_number):
        return self.threads


def _thread(thread_id, author, resolved=False):
    return ReviewThread(id=thread_id, is_resolved=resolved, comments=[ReviewThreadComment(database_id=1, body=FINDING, author_login=author)])


def _run(threads):
    identity = SimpleNamespace(matches_login=lambda login: login == "auto-coder-reviewer")
    sent = []

    def fake_send(repo, pr_data, head, report, client, bodies, config=None, canonical_blocker_ids=()):
        sent.append(tuple(bodies))
        return ["delivered"]

    with (
        patch.object(pr_processor, "resolve_reviewer_app_identity", return_value=identity),
        patch.object(pr_processor, "_send_adversarial_validation_feedback_to_cloud_task", side_effect=fake_send),
        patch.object(pr_processor, "_record_handoff_state"),
    ):
        result = pr_processor._replay_saved_nonpass_review(_Client(threads), "o/r", {"number": 5}, "abc", REPORT, None, AutomationConfig())
    return result, sent


def test_attached_reviewer_threads_are_delivered():
    (result, revalidate), sent = _run([_thread("T1", "auto-coder-reviewer")])
    assert sent == [(FINDING,)]
    assert list(result) == ["delivered"]
    assert revalidate is False


def test_no_actionable_threads_requests_revalidation():
    (result, revalidate), sent = _run([_thread("T1", "auto-coder-reviewer", resolved=True), _thread("T2", "someone-else")])
    assert sent == []
    assert revalidate is True
    assert "revalidating the current head" in result[0]
