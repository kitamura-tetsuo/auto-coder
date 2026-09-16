"""Issue #2026: authorship-verified findings-comment confirmation and publication.

``find_confirmed_publication`` is the new authoritative-reread check that both
``SpecificationValidationLifecycle`` and ``DecompositionValidationLifecycle``
use in place of a body-substring marker match: a pre-existing comment only
counts as already-published when its body is an exact match *and* it is
authored by the resolved reviewer App identity (REQ-005). ``publish_findings_comment``
translates the never-raising ``IssuePublicationResult`` from the reviewer-App
client into the typed-exception contract the rest of the codebase already
relies on for durable retry (REQ-002, REQ-006).
"""

from pathlib import Path

import httpx
import pytest

from auto_coder.github_app_reviewer import GitHubApiOutcome, ReviewerAppIdentity
from auto_coder.issue_review_publication import PublicationReceipt, find_confirmed_publication, publish_findings_comment
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubResponseMetadata,
    RequestProvenance,
)

IDENTITY = ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=8888)
MARKER = "auto-coder-specification-validation:abc123"
BODY = f"<!-- {MARKER} -->\n## Auto-Coder specification validation\n\nDetails."


def _comment(comment_id, body, login="auto-coder-reviewer[bot]", app_id=8888, via_app=True):
    comment = {"id": comment_id, "body": body, "user": {"login": login}}
    if via_app:
        comment["performed_via_github_app"] = {"id": app_id}
    return comment


class TestFindConfirmedPublication:
    def test_no_comments_returns_unconfirmed_and_not_conflicting(self):
        receipt, conflicting = find_confirmed_publication([], MARKER, BODY, IDENTITY)
        assert receipt is None
        assert conflicting is False

    def test_no_marker_present_returns_unconfirmed_and_not_conflicting(self):
        comments = [_comment(1, "Unrelated comment")]
        receipt, conflicting = find_confirmed_publication(comments, MARKER, BODY, IDENTITY)
        assert receipt is None
        assert conflicting is False

    def test_exact_body_and_matching_app_identity_confirms(self):
        comments = [_comment(42, BODY)]
        receipt, conflicting = find_confirmed_publication(comments, MARKER, BODY, IDENTITY)
        assert receipt == PublicationReceipt(42, IDENTITY.login, IDENTITY.app_id)
        assert conflicting is False

    def test_missing_performed_via_github_app_does_not_block_confirmation(self):
        """Absence of the field is not itself proof of mismatch (only a present-and-different id is)."""
        comments = [_comment(42, BODY, via_app=False)]
        receipt, conflicting = find_confirmed_publication(comments, MARKER, BODY, IDENTITY)
        assert receipt == PublicationReceipt(42, IDENTITY.login, IDENTITY.app_id)
        assert conflicting is False

    def test_different_login_is_an_unconfirmed_conflict_not_a_silent_pass(self):
        comments = [_comment(42, BODY, login="someone-else")]
        receipt, conflicting = find_confirmed_publication(comments, MARKER, BODY, IDENTITY)
        assert receipt is None
        assert conflicting is True

    def test_mismatched_via_app_id_is_a_conflict_even_with_matching_login(self):
        """A lookalike login is not enough once GitHub's own App id disagrees."""
        comments = [_comment(42, BODY, app_id=1234)]
        receipt, conflicting = find_confirmed_publication(comments, MARKER, BODY, IDENTITY)
        assert receipt is None
        assert conflicting is True

    def test_marker_present_but_body_diverges_is_a_conflict(self):
        divergent = BODY + "\nA different report."
        comments = [_comment(42, divergent)]
        receipt, conflicting = find_confirmed_publication(comments, MARKER, BODY, IDENTITY)
        assert receipt is None
        assert conflicting is True

    def test_first_page_conflict_second_page_confirmed_still_confirms(self):
        """A copy on an earlier page must not block finding the real one on a later page."""
        comments = [_comment(1, BODY, login="human-imitator"), _comment(2, BODY)]
        receipt, conflicting = find_confirmed_publication(comments, MARKER, BODY, IDENTITY)
        assert receipt == PublicationReceipt(2, IDENTITY.login, IDENTITY.app_id)

    def test_non_dict_comments_are_ignored(self):
        receipt, conflicting = find_confirmed_publication([None, "not-a-comment", 42], MARKER, BODY, IDENTITY)
        assert receipt is None
        assert conflicting is False


def _outcome(classification, delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED, status=200):
    return GitHubRequestOutcome(
        context=GitHubRequestContext("op", "attempt", "reviewer-app", "https://api.github.com", "POST", "mutation", "/repos/{repo}/issues/{n}/comments"),
        status=status,
        classification=classification,
        provenance=RequestProvenance.NETWORK,
        delivery=delivery,
        metadata=GitHubResponseMetadata(),
        elapsed_ms=1.0,
        message="test",
    )


class TestPublishFindingsComment:
    def test_confirmed_result_returns_publication_receipt(self, monkeypatch):
        from auto_coder import issue_review_publication
        from auto_coder.github_app_reviewer import IssuePublicationResult

        monkeypatch.setattr(
            issue_review_publication,
            "publish_issue_review",
            lambda repo, number, body, authorize_fn: IssuePublicationResult(555, IDENTITY, _outcome(GitHubApiOutcome.SUCCESS)),
        )
        receipt = publish_findings_comment("owner/repo", 42, "body", lambda: True)
        assert receipt == PublicationReceipt(555, IDENTITY.login, IDENTITY.app_id)

    def test_unconfirmed_result_raises_typed_github_request_error(self, monkeypatch):
        """Translation must preserve the original outcome (REQ-006), not swallow it into a generic failure."""
        from auto_coder import issue_review_publication
        from auto_coder.github_app_reviewer import IssuePublicationResult

        refused_outcome = _outcome(GitHubApiOutcome.REFUSED, delivery=DeliveryCertainty.DEFINITELY_NOT_SENT, status=0)
        monkeypatch.setattr(
            issue_review_publication,
            "publish_issue_review",
            lambda repo, number, body, authorize_fn: IssuePublicationResult(None, IDENTITY, refused_outcome),
        )
        with pytest.raises(GitHubRequestError) as excinfo:
            publish_findings_comment("owner/repo", 42, "body", lambda: False)
        assert excinfo.value.outcome is refused_outcome

    def test_missing_identity_also_raises(self, monkeypatch):
        from auto_coder import issue_review_publication
        from auto_coder.github_app_reviewer import IssuePublicationResult

        outcome = _outcome(GitHubApiOutcome.AUTHENTICATION_FAILURE, delivery=DeliveryCertainty.DEFINITELY_NOT_SENT, status=0)
        monkeypatch.setattr(
            issue_review_publication,
            "publish_issue_review",
            lambda repo, number, body, authorize_fn: IssuePublicationResult(None, None, outcome),
        )
        with pytest.raises(GitHubRequestError):
            publish_findings_comment("owner/repo", 42, "body", lambda: True)


class _PatchedRecordingClient:
    """Minimal HTTP double reused from tests/test_github_app_reviewer.py's pattern."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _response(status: int, data: dict) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://api.github.test"))


def test_publish_findings_comment_reaches_real_reviewer_app_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Production-path regression: publish_findings_comment -> publish_issue_review ->
    the real GitHubAppReviewer, against an HTTP-shaped transport double -- not a
    publisher stub. Proves the confirmed-receipt translation and the raised
    typed error on an unconfirmed (body-mismatched) response both flow through
    unmodified from the real client (Issue #2026 REQ-002/REQ-005/REQ-006)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".auto-coder").mkdir()
    (home / ".auto-coder" / "config.toml").write_text("[github-app-auto-coder-reviewer]\napp_id = 8888\n", encoding="utf-8")
    (home / ".auto-coder" / "auto-coder-reviewer.pem").write_text("FAKE_PEM", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("auto_coder.github_app_reviewer.GitHubAppReviewer._jwt", lambda self: "fake_jwt")

    auth_app = _response(200, {"id": 8888, "slug": "auto-coder-reviewer"})
    auth_installation = _response(200, {"id": 12345})
    auth_token = _response(201, {"token": "t_abc", "expires_at": "2099-01-01T00:00:00Z"})
    confirmed_comment = _response(
        201,
        {"id": 4242, "issue_url": "https://api.github.com/repos/owner/repo/issues/42", "body": "Exact Body", "user": {"login": "auto-coder-reviewer[bot]"}, "performed_via_github_app": {"id": 8888}},
    )
    client = _PatchedRecordingClient([auth_app, auth_installation, auth_token, confirmed_comment])
    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("auto_coder.github_app_reviewer.instrument_github_client", lambda client, **kwargs: client)

    receipt = publish_findings_comment("owner/repo", 42, "Exact Body", lambda: True)

    assert receipt == PublicationReceipt(4242, "auto-coder-reviewer[bot]", 8888)
    assert client.calls[-1][0] == "POST"
    assert client.calls[-1][1] == "https://api.github.com/repos/owner/repo/issues/42/comments"
    assert client.calls[-1][2]["json"]["body"] == "Exact Body"


def test_publish_findings_comment_raises_on_body_mismatch_over_real_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A response whose returned body does not match must never be swallowed into a false success."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".auto-coder").mkdir()
    (home / ".auto-coder" / "config.toml").write_text("[github-app-auto-coder-reviewer]\napp_id = 8888\n", encoding="utf-8")
    (home / ".auto-coder" / "auto-coder-reviewer.pem").write_text("FAKE_PEM", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("auto_coder.github_app_reviewer.GitHubAppReviewer._jwt", lambda self: "fake_jwt")

    auth_app = _response(200, {"id": 8888, "slug": "auto-coder-reviewer"})
    auth_installation = _response(200, {"id": 12345})
    auth_token = _response(201, {"token": "t_abc", "expires_at": "2099-01-01T00:00:00Z"})
    mismatched_comment = _response(
        201,
        {"id": 4242, "issue_url": "https://api.github.com/repos/owner/repo/issues/42", "body": "A different body", "user": {"login": "auto-coder-reviewer[bot]"}, "performed_via_github_app": {"id": 8888}},
    )
    client = _PatchedRecordingClient([auth_app, auth_installation, auth_token, mismatched_comment])
    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("auto_coder.github_app_reviewer.instrument_github_client", lambda client, **kwargs: client)

    with pytest.raises(GitHubRequestError):
        publish_findings_comment("owner/repo", 42, "Exact Body", lambda: True)
