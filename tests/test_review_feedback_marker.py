"""Tests for the cloud implementation agent's review-thread addressed protocol (issue #1618)."""

from auto_coder.prompt_loader import get_prompt_template
from auto_coder.review_feedback_marker import (
    REVIEW_ADDRESSED_MARKER,
    reply_claims_review_addressed,
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
