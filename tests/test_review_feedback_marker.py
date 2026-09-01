"""Tests for the cloud implementation agent's review-thread addressed protocol (issue #1618)."""

from auto_coder.prompt_loader import get_prompt_template, render_prompt
from auto_coder.review_feedback_marker import (
    REVIEW_ADDRESSED_MARKER,
    reply_claims_review_addressed,
)

ISSUE_ACTION_RENDER_KWARGS = dict(
    repo_name="owner/repo",
    issue_number=123,
    issue_title="Example issue",
    issue_body="Example body",
    issue_labels="",
    issue_state="open",
    issue_author="someone",
    commit_log="",
    linked_issues_context="",
)


class TestReviewAddressedMarker:
    def test_marker_is_stable_and_versioned(self):
        assert REVIEW_ADDRESSED_MARKER == "<!-- auto-coder-review-addressed:v1 -->"

    def test_marker_detected_in_reply(self):
        body = "Fixed the null check in foo.py; added a regression test.\n" + REVIEW_ADDRESSED_MARKER
        assert reply_claims_review_addressed(body) is True

    def test_ordinary_discussion_is_not_a_claim(self):
        # AC-002: natural language like "fixed" or "done" must never be treated
        # as an implementation-complete claim without the explicit marker.
        assert reply_claims_review_addressed("I think this is fixed") is False
        assert reply_claims_review_addressed("done") is False
        assert reply_claims_review_addressed("resolved this") is False

    def test_non_string_body_is_not_a_claim(self):
        assert reply_claims_review_addressed(None) is False
        assert reply_claims_review_addressed(123) is False

    def test_empty_body_is_not_a_claim(self):
        assert reply_claims_review_addressed("") is False


class TestPromptsRequireAddressWithoutResolve:
    """REQ-001/REQ-002/REQ-003/REQ-007: every cloud-agent review-feedback prompt
    must forbid resolving threads and require the machine-readable marker,
    without depending on any single reviewer's wording."""

    def _assert_protocol_present(self, template: str):
        lowered = template.lower()
        assert "not resolve" in lowered
        assert REVIEW_ADDRESSED_MARKER in template
        # REQ-004: disagreeing with or considering a finding invalid must also
        # withhold the marker, not just an inability to fix/verify it.
        assert "disagree with the finding" in lowered
        # REQ-007: must not hard-code one reviewer's identity as the only source of feedback.
        assert "codex" in lowered
        assert "adversarial" in lowered

    def test_issue_action_prompt(self):
        self._assert_protocol_present(get_prompt_template("issue.action"))

    def test_adversarial_validation_fix_prompt(self):
        self._assert_protocol_present(get_prompt_template("pr.adversarial_validation_fix"))

    def test_codex_cloud_continuation_prompt(self):
        self._assert_protocol_present(get_prompt_template("codex_cloud.continuation"))

    def test_codex_cloud_ci_review_repair_details_prompt(self):
        self._assert_protocol_present(get_prompt_template("codex_cloud.ci_review_repair_details"))

    def test_jules_issue_action_prompt(self):
        # jules.issue.action is the template actually used when a Jules-specific
        # template exists, so it must carry the protocol directly too.
        self._assert_protocol_present(get_prompt_template("jules.issue.action"))

    def test_effective_rendered_prompt_for_jules_issue_dispatch(self):
        # REQ-001/REQ-007: _process_issue_jules_mode, _process_issue_claude_routine_mode,
        # and _process_issue_codex_cloud_mode all call
        # render_prompt("issue.action", is_jules=True, ...), which redirects to
        # jules.issue.action. Exercise that real redirect path (not just the raw
        # "issue.action" template) so a jules.issue.action regression is caught.
        rendered = render_prompt("issue.action", is_jules=True, **ISSUE_ACTION_RENDER_KWARGS)
        self._assert_protocol_present(rendered)

    def test_effective_rendered_prompt_for_non_jules_issue_dispatch(self):
        rendered = render_prompt("issue.action", is_jules=False, **ISSUE_ACTION_RENDER_KWARGS)
        self._assert_protocol_present(rendered)
