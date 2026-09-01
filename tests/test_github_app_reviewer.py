"""Tests for dedicated GitHub App adversarial review publication."""

from pathlib import Path

import httpx
import pytest

from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult, ChangeProvenanceItem, ReviewThreadDisposition
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppConfig, ReviewerAppIdentity, load_reviewer_app_config, resolve_reviewer_app_identity


class RecordingClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


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
    comments = client.calls[-1][2]["json"]["comments"]
    assert len(comments) == 1
    assert comments[0]["path"] == "src/host.py"
    assert comments[0]["line"] == 1
    assert comments[0]["side"] == "RIGHT"
    assert "subject_type" not in comments[0]
    assert "assets/generated.bin" in comments[0]["body"]


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
    client = RecordingClient([response(401, {"message": "rejected"})])
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
    reviewer._tokens["owner/repo"].expires_at = 1_030.0
    now[0] = 1_001.0
    assert reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS")).success
    assert sum(call[1].endswith("/installation") for call in client.calls) == 2


def test_get_identity_resolves_bot_login_from_app_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingClient([response(200, {"id": 4765828, "slug": "auto-coder-reviewer"})])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    identity = reviewer.get_identity()

    assert identity.login == "auto-coder-reviewer[bot]"
    assert identity.app_id == 4765828
    assert client.calls[0][1].endswith("/app")


def test_get_identity_is_cached_after_first_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingClient([response(200, {"id": 1, "slug": "auto-coder-reviewer"})])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    first = reviewer.get_identity()
    second = reviewer.get_identity()

    assert first == second
    assert len(client.calls) == 1


def test_get_identity_fails_closed_on_malformed_app_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingClient([response(200, {"id": 1})])
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
