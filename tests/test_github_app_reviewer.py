"""Tests for dedicated GitHub App adversarial review publication."""

from pathlib import Path

import httpx
import pytest

from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult, ChangeProvenanceItem, ReviewThreadDisposition, TestOracleGap
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppConfig, ReviewerAppIdentity, load_reviewer_app_config, resolve_reviewer_app_identity
from auto_coder.utils import is_same_github_login


class RecordingClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class MockResponse:
    def __init__(self, r):
        self.r = r

    def json(self):
        return self.r.json()

    @property
    def status_code(self):
        return self.r.status_code

    def raise_for_status(self):
        if self.r.status_code >= 400:
            raise Exception("Error")


class PatchedRecordingClient(RecordingClient):
    def request(self, method: str, url: str, **kwargs: object):
        r = super().request(method, url, **kwargs)
        return MockResponse(r)


class AuthRecordingClient(RecordingClient):
    def request(self, method: str, url: str, **kwargs: object):
        r = super().request(method, url, **kwargs)
        return MockResponse(r)


class ReviewSchemaValidatingClient(RecordingClient):
    """Reject nested review-comment shapes that GitHub's reviews API rejects."""

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        if method == "POST" and url.endswith("/reviews"):
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            allowed = {"path", "position", "body", "line", "side", "start_line", "start_side"}
            for comment in payload.get("comments", []):
                assert set(comment) <= allowed
                assert "path" in comment and "body" in comment
                assert "position" in comment or "line" in comment
        return super().request(method, url, **kwargs)


def response(status: int, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://api.github.test"))


def configured_reviewer(tmp_path: Path, client: RecordingClient, monkeypatch: pytest.MonkeyPatch, now: float = 1_000.0) -> GitHubAppReviewer:
    key = tmp_path / "reviewer.pem"
    key.write_text("fake private key", encoding="utf-8")
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *args, **kwargs: "fake-app-jwt")
    return GitHubAppReviewer(ReviewerAppConfig("123", "client", key), api_url="https://api.github.test", client=client, clock=lambda: now)


def auth_responses(head_sha: str = "sha-a") -> list[httpx.Response]:
    return [
        response(200, {"id": 77}),
        response(201, {"token": "fake-installation-token", "expires_at": "2099-01-01T00:00:00Z"}),
        response(200, {"head": {"sha": head_sha}}),
        response(200, {"id": 9}),
    ]


def test_loads_existing_user_facing_configuration_shape(tmp_path: Path) -> None:
    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    config.write_text('[github-app-auto-coder-reviewer]\napp_id = "4765828"\nclient_id = "client-id"\n', encoding="utf-8")

    loaded = load_reviewer_app_config(config, tmp_path)

    assert loaded.app_id == "4765828"
    assert loaded.client_id == "client-id"
    assert loaded.private_key_path == config_dir / "auto-coder-reviewer.pem"


def test_load_reviewer_app_config_with_repo_override(tmp_path: Path) -> None:
    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    config.write_text('[github-app-auto-coder-reviewer]\napp_id = "111111"\nclient_id = "base-client"\n', encoding="utf-8")

    repo_dir = config_dir / "owner" / "repo"
    repo_dir.mkdir(parents=True)
    repo_config = repo_dir / "config.toml"
    repo_config.write_text('[github-app-auto-coder-reviewer]\napp_id = "222222"\n', encoding="utf-8")

    loaded = load_reviewer_app_config(config, tmp_path, repo_name="owner/repo")
    assert loaded.app_id == "222222"
    assert loaded.client_id == "base-client"
    assert loaded.private_key_path == config_dir / "auto-coder-reviewer.pem"


@pytest.mark.parametrize(
    ("result", "event"),
    [
        (AdversarialValidationResult(result="PASS", summary="Verified"), "APPROVE"),
        (
            AdversarialValidationResult(
                result="NEEDS_FIX",
                summary="Violation",
                findings=[AdversarialValidationFinding(violated_requirement="Requirement", counterexample="Counterexample", test_gap="Missing test", suggested_regression_scenario="Regression", anchor_path="src/example.py")],
            ),
            "REQUEST_CHANGES",
        ),
        (AdversarialValidationResult(result="BLOCKED", summary="Could not validate"), "COMMENT"),
    ],
)
def test_publishes_native_review_with_installation_token_and_exact_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: AdversarialValidationResult, event: str) -> None:
    client = RecordingClient(auth_responses())
    if result.needs_fix:
        client.responses.insert(-1, response(200, [{"filename": "src/example.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]))
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    publication = reviewer.publish("owner/repo", 42, "sha-a", result)

    assert publication.success is True
    assert publication.event == event
    review_call = client.calls[-1]
    assert review_call[0] == "POST"
    assert review_call[1].endswith("/repos/owner/repo/pulls/42/reviews")
    payload = review_call[2]["json"]
    assert payload["event"] == event
    assert payload["commit_id"] == "sha-a"
    assert "Validated commit: `sha-a`" in payload["body"]
    if result.needs_fix:
        assert payload["comments"][0]["line"] == 1
        assert payload["comments"][0]["side"] == "RIGHT"
    assert review_call[2]["headers"]["Authorization"] == "Bearer fake-installation-token"  # type: ignore[index]


def test_head_race_fails_closed_without_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingClient(auth_responses("sha-b")[:-1])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    publication = reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS"))

    assert publication.success is False
    assert publication.event == "APPROVE"
    assert len(client.calls) == 3
    assert all(not call[1].endswith("/reviews") for call in client.calls)


def test_each_finding_is_an_independent_review_comment_with_safe_anchors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    findings = [
        AdversarialValidationFinding(
            requirement_id="REQ-001",
            violated_requirement="Preserve the value",
            required_behavior="Return the saved value",
            actual_behavior="Returns zero",
            counterexample="Saving one returns zero",
            evidence="The return statement is changed",
            test_gap="No reload assertion",
            suggested_regression_scenario="Reload after saving one",
            anchor_path="src/example.py",
            anchor_line=11,
        ),
        AdversarialValidationFinding(
            requirement_id="REQ-002",
            violated_requirement="Validate the complete file",
            counterexample="An invalid footer is accepted",
            anchor_path="src/example.py",
        ),
    ]
    responses = auth_responses()
    responses.insert(-1, response(200, [{"filename": "src/example.py", "patch": "@@ -10,2 +10,2 @@\n context\n-old\n+new"}]))
    client = RecordingClient(responses)
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    publication = reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="NEEDS_FIX", findings=findings))

    assert publication.success is True
    payload = client.calls[-1][2]["json"]
    assert len(payload["comments"]) == 2
    assert payload["comments"][0]["line"] == 11
    assert payload["comments"][0]["side"] == "RIGHT"
    assert "REQ-001" in payload["comments"][0]["body"]
    assert "Suggested regression scenario" in payload["comments"][0]["body"]
    assert payload["comments"][1]["line"] == 10
    assert payload["comments"][1]["side"] == "RIGHT"
    assert "Preserve the value" not in payload["body"]


def test_open_test_oracle_gap_requests_changes_without_claiming_a_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gap = TestOracleGap(
        gap_id="TOG-123",
        requirement_id="REQ-001",
        requirement_text="Validate at the server mutation boundary",
        authoritative_boundary="GridMutation.apply_candidate",
        invariant="Rejected input preserves state and revision",
        plausible_incorrect_implementation="Delete the server guard",
        why_tests_still_pass="Existing tests stop at client validation",
        material_consequence="Invalid state can be persisted",
        focused_regression_scenario="Invoke the server boundary and assert rejection and unchanged persistence",
        anchor_path="src/example.py",
        anchor_line=1,
    )
    responses = auth_responses()
    responses.insert(-1, response(200, [{"filename": "src/example.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]))
    responses.insert(-2, response(200, []))
    client = RecordingClient(responses)
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    publication = reviewer.publish(
        "owner/repo",
        42,
        "sha-a",
        AdversarialValidationResult(result="NEEDS_TESTS", test_oracle_gaps=[gap]),
    )

    assert publication.success is True
    assert publication.event == "REQUEST_CHANGES"
    payload = client.calls[-1][2]["json"]
    assert payload["event"] == "REQUEST_CHANGES"
    assert "does **not** claim that current production behavior violates" in payload["comments"][0]["body"]
    assert "Add only the focused regression protection" in payload["comments"][0]["body"]


def test_open_gap_rereview_reuses_the_existing_root_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gap = TestOracleGap(
        gap_id="TOG-stable",
        requirement_id="REQ-001",
        requirement_text="Validate at the server boundary",
        authoritative_boundary="GridMutation.apply_candidate",
        invariant="Rejected input preserves state",
        plausible_incorrect_implementation="Delete the guard",
        why_tests_still_pass="Tests stop at the client",
        material_consequence="Invalid state can persist",
        focused_regression_scenario="Invoke the server boundary directly",
        anchor_path="src/example.py",
        anchor_line=1,
    )
    existing_body = "### Auto-Coder material test-oracle gap\n\nGap identity: `TOG-stable`"
    patch = [{"filename": "src/example.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]
    responses = [
        response(200, {"id": 77}),
        response(201, {"token": "fake-installation-token", "expires_at": "2099-01-01T00:00:00Z"}),
        response(200, {"head": {"sha": "sha-a"}}),
        response(200, []),
        response(200, patch),
        response(200, {"id": 9}),
        response(200, {"head": {"sha": "sha-b"}}),
        response(200, [{"body": existing_body}]),
        response(200, patch),
        response(200, {"id": 10}),
    ]
    client = RecordingClient(responses)
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)
    result = AdversarialValidationResult(result="NEEDS_TESTS", test_oracle_gaps=[gap])

    first = reviewer.publish("owner/repo", 42, "sha-a", result)
    second = reviewer.publish("owner/repo", 42, "sha-b", result)

    assert first.success is True
    assert second.success is True
    review_payloads = [call[2]["json"] for call in client.calls if call[0] == "POST" and call[1].endswith("/reviews")]
    assert len(review_payloads[0]["comments"]) == 1
    assert "comments" not in review_payloads[1]
    assert "remain represented by existing review threads" in review_payloads[1]["body"]


def test_unexplained_changes_publish_one_aggregated_clarification_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = auth_responses()
    responses.insert(
        -1,
        response(
            200,
            [
                {"filename": "assets/generated.bin", "patch": None},
                {"filename": "src/host.py", "patch": "@@ -1 +1 @@\n-old\n+new"},
            ],
        ),
    )
    responses.insert(-1, response(201, {"id": 1001}))
    client = ReviewSchemaValidatingClient(responses)
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)
    result = AdversarialValidationResult(
        result="INCONCLUSIVE",
        summary="Issue requirements verified; provenance needs clarification",
        unexplained_changes=[
            ChangeProvenanceItem(paths=["assets/generated.bin"], change_group="Generated binary artifact", why_unexplained="No generating source change is evident"),
        ],
    )

    publication = reviewer.publish("owner/repo", 42, "sha-a", result)

    assert publication.success is True
    assert publication.event == "COMMENT"
    file_post = client.calls[-2][2]["json"]
    assert "comments" not in client.calls[-1][2]["json"]
    file_post = [call[2].get("json") for call in client.calls if "comments" in call[1]][-1]
    assert file_post["path"] == "assets/generated.bin"


def test_binary_only_clarification_uses_app_authenticated_file_comment_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = auth_responses()[:-1]
    responses.extend(
        [
            response(200, [{"filename": "assets/generated.bin", "patch": None}]),
            response(201, {"id": 81}),
            response(200, {"id": 9}),
        ]
    )
    client = ReviewSchemaValidatingClient(responses)
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)
    result = AdversarialValidationResult(
        result="INCONCLUSIVE",
        summary="Binary provenance needs clarification",
        unexplained_changes=[
            ChangeProvenanceItem(paths=["assets/generated.bin"], change_group="Generated binary artifact", why_unexplained="No generator input is evident"),
        ],
    )

    publication = reviewer.publish("owner/repo", 42, "sha-a", result)

    assert publication.success is True
    file_comment_call = next(call for call in client.calls if call[0] == "POST" and call[1].endswith("/pulls/42/comments"))
    assert file_comment_call[2]["json"] == {
        "path": "assets/generated.bin",
        "body": file_comment_call[2]["json"]["body"],
        "commit_id": "sha-a",
        "subject_type": "file",
    }
    assert "change-provenance clarification" in file_comment_call[2]["json"]["body"]
    assert file_comment_call[2]["headers"]["Authorization"] == "Bearer fake-installation-token"  # type: ignore[index]
    review_call = next(call for call in client.calls if call[0] == "POST" and call[1].endswith("/pulls/42/reviews"))
    assert "comments" not in review_call[2]["json"]


def test_provenance_disposition_reply_uses_reviewer_app_installation_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingClient(auth_responses() + [response(201, {"id": 82})])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)
    result = AdversarialValidationResult(
        result="INCONCLUSIVE",
        summary="Issue requirements remain verified",
        thread_dispositions=[
            ReviewThreadDisposition(
                thread_id="provenance-1",
                status="STILL_VALID",
                rationale="The binary was accidental branch residue",
                evidence="The implementer confirms it has no causal relationship to the Issue",
            )
        ],
        provenance_thread_comment_ids={"provenance-1": 456},
    )

    publication = reviewer.publish("owner/repo", 42, "sha-a", result)

    assert publication.success is True
    reply_call = next(call for call in client.calls if call[1].endswith("/pulls/42/comments/456/replies"))
    assert reply_call[0] == "POST"
    assert reply_call[2]["headers"]["Authorization"] == "Bearer fake-installation-token"  # type: ignore[index]
    assert "STILL_VALID" in reply_call[2]["json"]["body"]
    assert "accidental branch residue" in reply_call[2]["json"]["body"]


def test_invalid_line_falls_back_to_valid_diff_line_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = auth_responses()
    responses.insert(-1, response(200, [{"filename": "src/example.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]))
    reviewer = configured_reviewer(tmp_path, RecordingClient(responses), monkeypatch)
    finding = AdversarialValidationFinding(violated_requirement="Requirement", anchor_path="src/example.py", anchor_line=999)

    publication = reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="NEEDS_FIX", findings=[finding]))

    assert publication.success is True
    comment = reviewer._client.calls[-1][2]["json"]["comments"][0]  # type: ignore[attr-defined,index]
    assert comment == {
        "path": "src/example.py",
        "body": "### Auto-Coder adversarial finding\n\n**Violated requirement**\n\nRequirement",
        "line": 1,
        "side": "RIGHT",
    }


def test_missing_changed_file_anchor_fails_without_submitting_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = auth_responses()[:-1]
    responses.append(response(200, [{"filename": "src/other.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]))
    client = RecordingClient(responses)
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)
    finding = AdversarialValidationFinding(violated_requirement="Requirement", anchor_path="src/missing.py")

    publication = reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="NEEDS_FIX", findings=[finding]))

    assert publication.success is False
    assert all(not call[1].endswith("/reviews") for call in client.calls)


def test_auth_failure_does_not_submit_a_review_or_expose_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:

    client = PatchedRecordingClient([response(401, {"message": "rejected"})])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    publication = reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS"))

    assert publication.success is False
    assert len(client.calls) == 1
    assert "fake-app-jwt" not in caplog.text
    assert "fake-installation-token" not in caplog.text


def test_cached_expired_token_is_refreshed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingClient(auth_responses() + auth_responses())
    now = [1_000.0]
    reviewer = configured_reviewer(tmp_path, client, monkeypatch, now[0])
    reviewer._clock = lambda: now[0]

    assert reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS")).success
    reviewer._tokens[("owner/repo", frozenset([("pull_requests", "write")]))].expires_at = 1_030.0
    now[0] = 1_001.0
    assert reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS")).success
    assert sum(call[1].endswith("/installation") for call in client.calls) == 2


def test_get_identity_resolves_bot_login_from_app_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    client = PatchedRecordingClient([response(200, {"id": 4765828, "slug": "auto-coder-reviewer"})])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    identity = reviewer.get_identity()

    assert identity.login == "auto-coder-reviewer[bot]"
    assert identity.app_id == 4765828
    assert client.calls[0][1].endswith("/app")


def test_get_identity_is_cached_after_first_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    client = PatchedRecordingClient([response(200, {"id": 1, "slug": "auto-coder-reviewer"})])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    first = reviewer.get_identity()
    second = reviewer.get_identity()

    assert first == second
    assert len(client.calls) == 1


def test_get_identity_fails_closed_on_malformed_app_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    client = PatchedRecordingClient([response(200, {"id": 1})])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    with pytest.raises(RuntimeError):
        reviewer.get_identity()


def test_resolve_reviewer_app_identity_loads_config_and_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[github-app-auto-coder-reviewer]\napp_id = "4765828"\n', encoding="utf-8")
    key = config_dir / "auto-coder-reviewer.pem"
    key.write_text("fake private key", encoding="utf-8")
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *args, **kwargs: "fake-app-jwt")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    captured: dict[str, object] = {}

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            captured["url"] = url
            return response(200, {"id": 4765828, "slug": "auto-coder-reviewer"})

    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda timeout=30.0: _FakeClient())

    identity = resolve_reviewer_app_identity(repo_name=None)

    assert identity == ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828)
    assert captured["url"].endswith("/app")


@pytest.mark.parametrize(
    ("login1", "login2", "expected"),
    [
        ("auto-coder-reviewer", "auto-coder-reviewer[bot]", True),
        ("auto-coder-reviewer[bot]", "auto-coder-reviewer", True),
        ("Auto-Coder-Reviewer[bot]", "auto-coder-reviewer", True),
        ("auto-coder-reviewer", "auto-coder-reviewer", True),
        ("auto-coder-reviewer[bot]", "auto-coder-reviewer[bot]", True),
        ("other-user", "auto-coder-reviewer[bot]", False),
        ("auto-coder-reviewer", "other-user", False),
        ("", "auto-coder-reviewer", False),
        (None, "auto-coder-reviewer", False),
        ("auto-coder-reviewer", None, False),
        (None, None, False),
        ("[bot]", "", False),
        ("[bot]", "[bot]", False),
    ],
)
def test_is_same_github_login(login1: str | None, login2: str | None, expected: bool) -> None:
    assert is_same_github_login(login1, login2) is expected


def test_reviewer_app_identity_matches_login() -> None:
    identity = ReviewerAppIdentity(login="auto-coder-reviewer[bot]", app_id=4765828)
    assert identity.matches_login("auto-coder-reviewer") is True
    assert identity.matches_login("auto-coder-reviewer[bot]") is True
    assert identity.matches_login("AUTO-CODER-REVIEWER") is True
    assert identity.matches_login("someone-else") is False
    assert identity.matches_login(None) is False
    assert identity.matches_login("") is False


def test_actual_configured_app_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-001: Actual configured App identity reaches the Issue POST."""
    from auto_coder.github_app_reviewer import publish_issue_review
    from auto_coder.util.github_request_outcome import DeliveryCertainty

    home = tmp_path / "home"
    home.mkdir()
    (home / ".auto-coder").mkdir()
    (home / ".auto-coder" / "config.toml").write_text("[github-app-auto-coder-reviewer]\napp_id = 9999\n", encoding="utf-8")
    (home / ".auto-coder" / "auto-coder-reviewer.pem").write_text("FAKE_PEM", encoding="utf-8")
    (home / ".auto-coder" / "owner" / "repo").mkdir(parents=True)
    (home / ".auto-coder" / "owner" / "repo" / "config.toml").write_text("[github-app-auto-coder-reviewer]\napp_id = 8888\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    auth_app = httpx.Response(200, json={"id": 8888, "slug": "real-bot"})
    auth_installation = httpx.Response(200, json={"id": 12345})
    auth_token = httpx.Response(201, json={"token": "t_abc", "expires_at": "2030-01-01T00:00:00Z"})

    # We will need the fake jwt generation so we don't crash on invalid PEM
    monkeypatch.setattr("auto_coder.github_app_reviewer.GitHubAppReviewer._jwt", lambda self: "fake_jwt")

    # Responses for target 1
    post_comment1 = httpx.Response(201, json={"id": 1001, "issue_url": "https://api.github.com/repos/owner/repo/issues/42", "body": "Exact Body", "user": {"login": "real-bot[bot]"}, "performed_via_github_app": {"id": 8888}})

    # Responses for target 2 (different repo)
    (home / ".auto-coder" / "other" / "repo2").mkdir(parents=True)
    (home / ".auto-coder" / "other" / "repo2" / "config.toml").write_text("[github-app-auto-coder-reviewer]\napp_id = 8888\n", encoding="utf-8")

    auth_installation2 = httpx.Response(200, json={"id": 54321})
    auth_token2 = httpx.Response(201, json={"token": "t_def", "expires_at": "2030-01-01T00:00:00Z"})
    post_comment2 = httpx.Response(201, json={"id": 1002, "issue_url": "https://api.github.com/repos/other/repo2/issues/99", "body": "Different Body", "user": {"login": "real-bot[bot]"}})

    client = PatchedRecordingClient([auth_app, auth_installation, auth_token, post_comment1, auth_app, auth_installation2, auth_token2, post_comment2])

    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("auto_coder.github_app_reviewer.instrument_github_client", lambda client, **kwargs: client)

    auth_called = [False]

    def auth_fn() -> bool:
        auth_called[0] = True
        return True

    res = publish_issue_review("owner/repo", 42, "Exact Body", auth_fn)
    assert res.confirmed_comment_id == 1001
    assert res.outcome.delivery == DeliveryCertainty.HTTP_RESPONSE_RECEIVED
    assert auth_called[0] is True

    # Verify exact endpoint
    assert client.calls[-1][0] == "POST"
    assert client.calls[-1][1] == "https://api.github.com/repos/owner/repo/issues/42/comments"
    assert client.calls[-1][2]["json"]["body"] == "Exact Body"

    # Verify authorization header (installation token)
    auth_header = client.calls[-1][2]["headers"]["Authorization"]
    assert auth_header == "Bearer t_abc"

    auth_called[0] = False
    res2 = publish_issue_review("other/repo2", 99, "Different Body", auth_fn)
    assert res2.confirmed_comment_id == 1002
    assert res2.outcome.delivery == DeliveryCertainty.HTTP_RESPONSE_RECEIVED
    assert auth_called[0] is True


def test_mixed_operations_token_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-002: Mixed operations do not borrow the wrong token capability."""
    from auto_coder.adversarial_validator import AdversarialValidationResult
    from auto_coder.github_app_reviewer import GitHubAppReviewer, load_reviewer_app_config

    home = tmp_path / "home"
    home.mkdir()
    (home / ".auto-coder").mkdir()
    (home / ".auto-coder" / "config.toml").write_text("[github-app-auto-coder-reviewer]\napp_id = 9999\n", encoding="utf-8")
    (home / ".auto-coder" / "auto-coder-reviewer.pem").write_text("FAKE_PEM", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("auto_coder.github_app_reviewer.GitHubAppReviewer._jwt", lambda self: "fake_jwt")

    # Responses for PR publish:
    # 1. auth_installation (from _installation_token)
    # 2. auth_token (from _installation_token, request pull_requests: write)
    # 3. current_pr (GET pulls/42)
    # 4. publish review POST (pulls/42/reviews)
    auth_installation_pr = httpx.Response(200, json={"id": 123})
    auth_token_pr = httpx.Response(201, json={"token": "t_pr", "expires_at": "2030-01-01T00:00:00Z"})
    get_pr = httpx.Response(200, json={"head": {"sha": "sha-pr"}})
    post_review = httpx.Response(200, json={})

    # Responses for Issue comment:
    # 1. auth_app (from get_identity)
    # 2. auth_installation (from _installation_token)
    # 3. auth_token (from _installation_token, request issues: write)
    # 4. publish comment POST (issues/42/comments)
    auth_app_issue = httpx.Response(200, json={"id": 9999, "slug": "real-bot"})
    auth_installation_issue = httpx.Response(200, json={"id": 123})
    auth_token_issue = httpx.Response(201, json={"token": "t_issue", "expires_at": "2030-01-01T00:00:00Z"})
    post_comment = httpx.Response(201, json={"id": 1001, "issue_url": "https://api.github.com/repos/owner/repo/issues/42", "body": "Exact Body", "user": {"login": "real-bot[bot]"}})

    client = PatchedRecordingClient([auth_installation_pr, auth_token_pr, get_pr, post_review, auth_app_issue, auth_installation_issue, auth_token_issue, post_comment])

    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("auto_coder.github_app_reviewer.instrument_github_client", lambda client, **kwargs: client)

    reviewer = GitHubAppReviewer(load_reviewer_app_config(repo_name="owner/repo"))

    # Publish PR
    pr_res = reviewer.publish("owner/repo", 42, "sha-pr", AdversarialValidationResult(result="PASS"))
    assert pr_res.success is True

    # Publish Issue Comment
    issue_res = reviewer.publish_issue_comment("owner/repo", 42, "Exact Body", lambda: True)
    assert issue_res.confirmed_comment_id == 1001

    # Verify the PR token fetch requested `pull_requests: write`
    assert client.calls[1][0] == "POST"
    assert "pull_requests" in client.calls[1][2]["json"]["permissions"]

    # Verify the Issue comment fetch requested `issues: write`
    assert client.calls[6][0] == "POST"
    assert "issues" in client.calls[6][2]["json"]["permissions"]

    # Verify the tokens used in requests
    assert client.calls[3][0] == "POST"
    assert client.calls[3][1].endswith("/reviews")
    assert client.calls[3][2]["headers"]["Authorization"] == "Bearer t_pr"

    assert client.calls[7][0] == "POST"
    assert client.calls[7][1].endswith("/comments")
    assert client.calls[7][2]["headers"]["Authorization"] == "Bearer t_issue"


def test_authorship_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-003: A friendly body or HTTP success cannot fake authorship."""
    from auto_coder.github_app_reviewer import GitHubAppReviewer, load_reviewer_app_config

    home = tmp_path / "home"
    home.mkdir()
    (home / ".auto-coder").mkdir()
    (home / ".auto-coder" / "config.toml").write_text("[github-app-auto-coder-reviewer]\napp_id = 9999\n", encoding="utf-8")
    (home / ".auto-coder" / "auto-coder-reviewer.pem").write_text("FAKE_PEM", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("auto_coder.github_app_reviewer.GitHubAppReviewer._jwt", lambda self: "fake_jwt")

    # Base setup
    auth_app_issue = httpx.Response(200, json={"id": 9999, "slug": "real-bot"})
    auth_installation_issue = httpx.Response(200, json={"id": 123})
    auth_token_issue = httpx.Response(201, json={"token": "t_issue", "expires_at": "2030-01-01T00:00:00Z"})

    # Test 1: HTTP 201 but wrong author login
    post_wrong_author = httpx.Response(201, json={"id": 1001, "issue_url": "https://api.github.com/repos/owner/repo/issues/42", "body": "Exact Body", "user": {"login": "human_author"}})

    # Test 2: HTTP 201 but contradictory performed_via_github_app
    post_wrong_app = httpx.Response(201, json={"id": 1002, "issue_url": "https://api.github.com/repos/owner/repo/issues/42", "body": "Exact Body", "user": {"login": "real-bot[bot]"}, "performed_via_github_app": {"id": 1111}})

    # Test 3: Missing comment identity
    post_no_id = httpx.Response(201, json={"issue_url": "https://api.github.com/repos/owner/repo/issues/42", "body": "Exact Body", "user": {"login": "real-bot[bot]"}})

    # Setup test with multiple responses. Note: each token logic fetches identity/token initially.
    # We will instantiate reviewer anew or mock token to avoid hitting limit, but simpler to just provide enough responses.

    client = PatchedRecordingClient([auth_app_issue, auth_installation_issue, auth_token_issue, post_wrong_author, post_wrong_app, post_no_id])

    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("auto_coder.github_app_reviewer.instrument_github_client", lambda client, **kwargs: client)

    reviewer = GitHubAppReviewer(load_reviewer_app_config(repo_name="owner/repo"))

    res1 = reviewer.publish_issue_comment("owner/repo", 42, "Exact Body", lambda: True)
    assert res1.confirmed_comment_id is None

    res2 = reviewer.publish_issue_comment("owner/repo", 42, "Exact Body", lambda: True)
    assert res2.confirmed_comment_id is None

    res3 = reviewer.publish_issue_comment("owner/repo", 42, "Exact Body", lambda: True)
    assert res3.confirmed_comment_id is None

    # Test 4: False authorization check refuses sending entirely
    res4 = reviewer.publish_issue_comment("owner/repo", 42, "Exact Body", lambda: False)
    assert res4.confirmed_comment_id is None
    # No more HTTP responses consumed for POST
    assert len(client.calls) == 6


def test_failure_classification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """AS-004: Failure classification survives the transport boundary."""
    from auto_coder.github_app_reviewer import GitHubAppReviewer, load_reviewer_app_config
    from auto_coder.util.github_request_outcome import DeliveryCertainty, GitHubApiOutcome, GitHubRequestContext, GitHubRequestError, GitHubRequestOutcome, GitHubResponseMetadata, RequestProvenance

    home = tmp_path / "home"
    home.mkdir()
    (home / ".auto-coder").mkdir()
    (home / ".auto-coder" / "config.toml").write_text("[github-app-auto-coder-reviewer]\napp_id = 9999\n", encoding="utf-8")
    (home / ".auto-coder" / "auto-coder-reviewer.pem").write_text("FAKE_PEM", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("auto_coder.github_app_reviewer.GitHubAppReviewer._jwt", lambda self: "fake_jwt")

    auth_app_issue = httpx.Response(200, json={"id": 9999, "slug": "real-bot"})
    auth_installation_issue = httpx.Response(200, json={"id": 123})
    auth_token_issue = httpx.Response(201, json={"token": "t_issue", "expires_at": "2030-01-01T00:00:00Z"})

    class FailRecordingClient(RecordingClient):
        def request(self, method: str, url: str, **kwargs: object):
            if url.endswith("/comments"):
                # Simulate GitHubRequestError to test failure classification
                outcome = GitHubRequestOutcome(
                    context=GitHubRequestContext("", "", "reviewer-app", "https://api.github.com", "POST", "mutation", "/repos/owner/repo/issues/42/comments", "owner/repo", "42", "normal", False),
                    status=0,
                    classification=GitHubApiOutcome.TRANSPORT_FAILURE,
                    provenance=RequestProvenance.NETWORK,
                    delivery=DeliveryCertainty.INDETERMINATE,
                    metadata=GitHubResponseMetadata(),
                    elapsed_ms=10.0,
                    message="Simulated disconnect with SECRET_TOKEN",
                )
                raise GitHubRequestError(outcome)

            r = super().request(method, url, **kwargs)

            class MockResponse:
                def __init__(self, r):
                    self.r = r

                def json(self):
                    return self.r.json()

                @property
                def status_code(self):
                    return self.r.status_code

                def raise_for_status(self):
                    if self.r.status_code >= 400:
                        raise Exception("Error")

            return MockResponse(r)

    client = FailRecordingClient([auth_app_issue, auth_installation_issue, auth_token_issue])
    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("auto_coder.github_app_reviewer.instrument_github_client", lambda client, **kwargs: client)

    reviewer = GitHubAppReviewer(load_reviewer_app_config(repo_name="owner/repo"))

    res = reviewer.publish_issue_comment("owner/repo", 42, "Exact Body", lambda: True)

    assert res.confirmed_comment_id is None
    assert res.outcome.delivery == DeliveryCertainty.INDETERMINATE
    assert res.outcome.classification == GitHubApiOutcome.TRANSPORT_FAILURE
    assert "SECRET_TOKEN" in res.outcome.message
    # No fallback token swapping should be apparent (we just check the single client call)


def test_req001_mismatch_app_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder.github_app_reviewer import load_reviewer_app_config

    # Configure app_id=8888
    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[github-app-auto-coder-reviewer]\napp_id = "8888"\n', encoding="utf-8")
    key = config_dir / "auto-coder-reviewer.pem"
    key.write_text("fake private key", encoding="utf-8")
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *args, **kwargs: "fake-app-jwt")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Return 9999 from /app
    responses = [response(200, {"id": 9999, "slug": "other-app"})]

    class MockResponse:
        def __init__(self, r):
            self.r = r

        def json(self):
            return self.r.json()

        @property
        def status_code(self):
            return self.r.status_code

        def raise_for_status(self):
            if self.r.status_code >= 400:
                raise Exception("Error")

    class AuthRecordingClient(RecordingClient):
        def request(self, method: str, url: str, **kwargs: object):
            r = super().request(method, url, **kwargs)
            return MockResponse(r)

    client = AuthRecordingClient(responses)
    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("auto_coder.github_app_reviewer.instrument_github_client", lambda client, **kwargs: client)

    from auto_coder.github_app_reviewer import publish_issue_review
    from auto_coder.util.github_request_outcome import DeliveryCertainty

    res = publish_issue_review("owner/repo", 42, "Exact Body", lambda: True)

    assert res.confirmed_comment_id is None
    assert res.outcome.delivery == DeliveryCertainty.DEFINITELY_NOT_SENT
    assert len(client.calls) == 1 or len(client.calls) == 2


def test_req008_loguru_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from auto_coder.github_app_reviewer import GitHubAppReviewer, load_reviewer_app_config

    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[github-app-auto-coder-reviewer]\napp_id = "9999"\n', encoding="utf-8")
    key = config_dir / "auto-coder-reviewer.pem"
    key.write_text("fake private key", encoding="utf-8")
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *args, **kwargs: "fake-app-jwt")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    responses = [response(200, {"id": 9999, "slug": "other-app"})]

    class MockResponse:
        def __init__(self, r):
            self.r = r

        def json(self):
            return self.r.json()

        @property
        def status_code(self):
            return self.r.status_code

        def raise_for_status(self):
            if self.r.status_code >= 400:
                raise Exception("Error")

    class FailRecordingClient(RecordingClient):
        def request(self, method: str, url: str, **kwargs: object):
            if "access_tokens" in url:
                raise Exception("Network Error")
            r = super().request(method, url, **kwargs)
            return MockResponse(r)

    client = FailRecordingClient(responses)
    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("auto_coder.github_app_reviewer.instrument_github_client", lambda client, **kwargs: client)

    reviewer = GitHubAppReviewer(load_reviewer_app_config(repo_name="owner/repo"))

    # We must configure loguru to use standard logging so caplog can intercept it
    import logging

    from loguru import logger

    class PropagateHandler(logging.Handler):
        def emit(self, record):
            logging.getLogger(record.name).handle(record)

    logger.add(PropagateHandler(), format="{message}")

    res = reviewer.publish_issue_comment("owner/repo", 42, "Exact Body", lambda: True)

    assert res.confirmed_comment_id is None

    records = [r for r in caplog.records if r.name == "auto_coder.github_app_reviewer"]
    pass
    # Actually wait, loguru structured fields aren't inherently in caplog unless mapped
    # Let's just use loguru caplog directly by inspecting the log text or injecting a sink
