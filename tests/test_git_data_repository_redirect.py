"""Transport component checks for read-only GitHub repository moves."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import httpx
import pytest

from auto_coder.tested_source_refs import REQUIRED_JOBS, TEST_PROFILE, WORKFLOW_PATH
from auto_coder.tested_source_refs import TestedSourceError as SourceEvidenceError
from auto_coder.tested_source_refs import TestedSourcePublisher as SourcePublisher
from auto_coder.tested_source_refs import TestedSourceRecord as SourceRecord
from auto_coder.tested_source_refs import serialize_record
from auto_coder.util import gh_cache
from auto_coder.util.gh_cache import GitHubGitDataClient
from auto_coder.util.github_request_outcome import DeliveryCertainty, GitHubApiOutcome, GitHubRequestContext, GitHubRequestError, GitHubRequestOutcome, GitHubRequestRefused, github_http_client

API = "https://api.github.com"
REPOSITORY = "kitamura-tetsuo/auto-coder"
REPOSITORY_ID = 1032862876
SOURCE_SHA = "e742338b3f311e6d30a22a8f5341b8072513d813"
TAG_OBJECT = "19be9ea811d01aa973341451ce1216a2c05c25c1"
REQUEST_PATH = "actions/runs/17/attempts/2/jobs?per_page=100&cursor=a%2Fb"
DESTINATION = f"{API}/repositories/{REPOSITORY_ID}/{REQUEST_PATH}"


@dataclass
class TransportBoundary:
    handler: Callable[[httpx.Request], httpx.Response] | None = None
    sent: list[httpx.Request] = field(default_factory=list)
    admitted: list[GitHubRequestContext] = field(default_factory=list)
    outcomes: list[GitHubRequestOutcome] = field(default_factory=list)
    refuse_after: int | None = None
    clients_created: int = 0

    def send(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        assert self.handler is not None
        return self.handler(request)

    def admit(self, context: GitHubRequestContext) -> bool | None:
        self.admitted.append(context)
        if self.refuse_after is not None and len(self.admitted) > self.refuse_after:
            return False
        return None


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> TransportBoundary:
    observation = TransportBoundary()

    def client(*, subsystem: str, api_origin: str, timeout: float, follow_redirects: bool = False) -> httpx.Client:
        observation.clients_created += 1
        assert follow_redirects is False
        return github_http_client(
            subsystem=subsystem,
            api_origin=api_origin,
            timeout=timeout,
            follow_redirects=follow_redirects,
            transport=httpx.MockTransport(observation.send),
            admission_hook=observation.admit,
            observation_hook=observation.outcomes.append,
        )

    monkeypatch.setattr(gh_cache, "github_http_client", client)
    return observation


@pytest.mark.parametrize("api_url", (API, "https://github.example.test/api/v3", "http://127.0.0.1:8765/api/v3"))
def test_same_origin_repository_id_read_preserves_request_and_both_observations(boundary: TransportBoundary, api_url: str) -> None:
    original = f"{api_url}/repos/{REPOSITORY}/{REQUEST_PATH}"
    moved = f"{api_url}/repositories/{REPOSITORY_ID}/{REQUEST_PATH}"

    def response(request: httpx.Request) -> httpx.Response:
        if str(request.url) == original:
            return httpx.Response(301, headers={"Location": moved}, json={"message": "Moved Permanently"})
        assert str(request.url) == moved
        return httpx.Response(200, json={"total_count": 0, "jobs": []})

    boundary.handler = response
    client = GitHubGitDataClient("fixture-read-token", REPOSITORY, api_url=api_url, timeout=7.5)
    result = client.request("GET", REQUEST_PATH)

    assert result.status == 200 and result.data == {"total_count": 0, "jobs": []}
    assert [str(request.url) for request in boundary.sent] == [original, moved]
    for request in boundary.sent:
        assert request.method == "GET"
        assert request.url.query == b"per_page=100&cursor=a%2Fb"
        assert request.headers["Authorization"] == "Bearer fixture-read-token"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.extensions["timeout"] == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}
    assert boundary.clients_created == 1
    assert [entry.method for entry in boundary.admitted] == ["GET", "GET"]
    assert all(entry.subsystem == "git-data" and entry.kind == "read" for entry in boundary.admitted)
    assert [entry.status for entry in boundary.outcomes] == [301, 200]
    assert all(entry.delivery is DeliveryCertainty.HTTP_RESPONSE_RECEIVED for entry in boundary.outcomes)

    # A move is not cached as a new repository identity or a successful response.
    assert client.request("GET", REQUEST_PATH) == result
    assert [str(request.url) for request in boundary.sent] == [original, moved, original, moved]
    assert [entry.status for entry in boundary.outcomes] == [301, 200, 301, 200]


def test_public_read_does_not_add_authorization_after_move(boundary: TransportBoundary) -> None:
    boundary.handler = lambda request: httpx.Response(301, headers={"Location": DESTINATION}) if len(boundary.sent) == 1 else httpx.Response(200, json={"ok": True})
    assert GitHubGitDataClient("", REPOSITORY).request("GET", REQUEST_PATH).status == 200
    assert len(boundary.sent) == 2
    assert all("Authorization" not in request.headers for request in boundary.sent)


@pytest.mark.parametrize(
    "location",
    (
        None,
        "",
        "https://other.example/repositories/1032862876/" + REQUEST_PATH,
        "http://api.github.com/repositories/1032862876/" + REQUEST_PATH,
        "https://api.github.com:444/repositories/1032862876/" + REQUEST_PATH,
        "https://user:password@api.github.com/repositories/1032862876/" + REQUEST_PATH,
        DESTINATION + "#fragment",
        API + "/repos/outliner-online/auto-coder/" + REQUEST_PATH,
        API + "/repositories/0/" + REQUEST_PATH,
        API + "/repositories/-1/" + REQUEST_PATH,
        API + "/repositories/not-an-id/" + REQUEST_PATH,
        API + "/repositories/1032862876/actions/runs/18/attempts/2/jobs?per_page=100&cursor=a%2Fb",
        API + "/repositories/1032862876/actions/runs/17/attempts/2/jobs",
        DESTINATION + "&extra=1",
        API + "/repositories/1032862876/actions/runs/17/attempts/2/jobs?cursor=a%2Fb&per_page=100",
        API + "/repositories/1032862876/actions/runs/17/attempts/2/jobs?per_page=100&cursor=a%2fb",
        "https://[invalid/repositories/1032862876/" + REQUEST_PATH,
    ),
)
def test_unsupported_move_is_unavailable_before_any_second_send(boundary: TransportBoundary, location: str | None) -> None:
    headers = {} if location is None else {"Location": location}
    boundary.handler = lambda request: httpx.Response(301, headers=headers, json={"message": "Moved Permanently"})
    result = GitHubGitDataClient("fixture-read-token", REPOSITORY).request("GET", REQUEST_PATH)
    assert result.status == 301
    assert result.data == {"message": "Moved Permanently"}
    assert len(boundary.sent) == len(boundary.admitted) == len(boundary.outcomes) == 1


def test_malformed_redirect_protocol_fails_before_a_second_send(boundary: TransportBoundary) -> None:
    location = "https://api.github.com:invalid/repositories/1032862876/" + REQUEST_PATH
    boundary.handler = lambda request: httpx.Response(301, headers={"Location": location})
    # HTTPX parses next_request even when automatic redirects are disabled.
    with pytest.raises(RuntimeError, match="^GitHub Git-data request unavailable$") as failure:
        GitHubGitDataClient("fixture-read-token", REPOSITORY).request("GET", REQUEST_PATH)
    assert isinstance(failure.value.__cause__, httpx.RemoteProtocolError)
    assert len(boundary.sent) == len(boundary.admitted) == len(boundary.outcomes) == 1


@pytest.mark.parametrize("destination_prefix", (API, API + "/other/api/v3"))
def test_configured_api_path_prefix_cannot_change(boundary: TransportBoundary, destination_prefix: str) -> None:
    boundary.handler = lambda request: httpx.Response(301, headers={"Location": f"{destination_prefix}/repositories/{REPOSITORY_ID}/{REQUEST_PATH}"})
    assert GitHubGitDataClient("fixture-read-token", REPOSITORY, api_url=API + "/api/v3").request("GET", REQUEST_PATH).status == 301
    assert len(boundary.sent) == 1


@pytest.mark.parametrize("status", (302, 303, 307, 308))
def test_other_redirect_statuses_do_not_trigger_the_repository_move_rule(boundary: TransportBoundary, status: int) -> None:
    boundary.handler = lambda request: httpx.Response(status, headers={"Location": DESTINATION})
    assert GitHubGitDataClient("fixture-read-token", REPOSITORY).request("GET", REQUEST_PATH).status == status
    assert len(boundary.sent) == 1


@pytest.mark.parametrize("method", ("POST", "PUT", "PATCH", "DELETE", "HEAD", "get"))
def test_only_an_explicit_uppercase_read_can_retry(boundary: TransportBoundary, method: str) -> None:
    payload = {"marker": "unchanged-request"}
    boundary.handler = lambda request: httpx.Response(301, headers={"Location": DESTINATION})
    result = GitHubGitDataClient("fixture-read-token", REPOSITORY).request(method, REQUEST_PATH, payload)
    assert result.status == 301
    assert len(boundary.sent) == 1
    assert boundary.sent[0].method == method.upper()
    assert json.loads(boundary.sent[0].content) == payload


def test_a_second_move_is_not_followed(boundary: TransportBoundary) -> None:
    boundary.handler = lambda request: httpx.Response(301, headers={"Location": DESTINATION})
    result = GitHubGitDataClient("fixture-read-token", REPOSITORY).request("GET", REQUEST_PATH)
    assert result.status == 301
    assert [str(request.url) for request in boundary.sent] == [f"{API}/repos/{REPOSITORY}/{REQUEST_PATH}", DESTINATION]
    assert [entry.status for entry in boundary.outcomes] == [301, 301]


@pytest.mark.parametrize("status", (401, 403, 404, 429, 500))
def test_redirected_failure_is_returned_without_fallback_or_retry(boundary: TransportBoundary, status: int) -> None:
    body = {"message": "Read remains unavailable"}

    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": DESTINATION}) if len(boundary.sent) == 1 else httpx.Response(status, json=body)

    boundary.handler = response
    result = GitHubGitDataClient("fixture-read-token", REPOSITORY).request("GET", REQUEST_PATH)
    assert result.status == status and result.data == body
    assert len(boundary.sent) == 2
    assert [entry.status for entry in boundary.outcomes] == [301, status]


def test_redirected_read_remains_subject_to_shared_admission(boundary: TransportBoundary) -> None:
    boundary.handler = lambda request: httpx.Response(301, headers={"Location": DESTINATION})
    boundary.refuse_after = 1
    with pytest.raises(GitHubRequestRefused):
        GitHubGitDataClient("fixture-read-token", REPOSITORY).request("GET", REQUEST_PATH)
    assert len(boundary.sent) == 1 and len(boundary.admitted) == 2
    assert [entry.status for entry in boundary.outcomes] == [301, None]
    assert boundary.outcomes[-1].delivery is DeliveryCertainty.DEFINITELY_NOT_SENT


def test_redirected_transport_failure_stays_unavailable(boundary: TransportBoundary) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        if len(boundary.sent) == 1:
            return httpx.Response(301, headers={"Location": DESTINATION})
        raise httpx.ConnectError("controlled transport failure", request=request)

    boundary.handler = response
    with pytest.raises(GitHubRequestError) as failure:
        GitHubGitDataClient("fixture-read-token", REPOSITORY).request("GET", REQUEST_PATH)
    assert failure.value.outcome is boundary.outcomes[-1]
    assert failure.value.outcome.classification is GitHubApiOutcome.TRANSPORT_FAILURE
    assert failure.value.outcome.delivery is DeliveryCertainty.INDETERMINATE
    assert isinstance(failure.value.__cause__, httpx.ConnectError)
    assert len(boundary.sent) == 2


def historical_evidence(boundary: TransportBoundary, mutation: str | None = None) -> tuple[SourcePublisher, SourceRecord, dict[str, object]]:
    record = SourceRecord(
        schema_version=1,
        repository=REPOSITORY,
        source_sha=SOURCE_SHA,
        distribution_version="2026.11.12.2480+g" + SOURCE_SHA,
        lock_sha256="77b045a76ffa3dfe49fc3d636f73a8a32efd9e5179b7904ff998c3afa2be78ec",
        python_version="3.12",
        run_id=43226019627,
        run_attempt=1,
        workflow_id=395675928,
        workflow_path=WORKFLOW_PATH,
        test_profile=TEST_PROFILE,
        test_result="success",
    )
    tag = "auto-coder-tested-" + SOURCE_SHA
    run_path = f"actions/runs/{record.run_id}/attempts/{record.run_attempt}"
    annotation = replace(record, repository="unrelated/repository") if mutation == "annotation-repository" else record
    tag_body = {"tag": tag, "object": {"type": "commit", "sha": "b" * 40 if mutation == "direct-commit" else SOURCE_SHA}, "message": serialize_record(annotation)}
    run_body = {
        "id": record.run_id,
        "run_attempt": 2 if mutation == "recorded-attempt" else record.run_attempt,
        "workflow_id": record.workflow_id,
        "head_sha": "b" * 40 if mutation == "recorded-head" else record.source_sha,
        "path": record.workflow_path,
        "status": "completed",
        "conclusion": "success",
        "repository": {"id": REPOSITORY_ID, "full_name": "outliner-online/auto-coder"},
    }
    jobs = [{"name": name, "status": "completed", "conclusion": "success", "run_attempt": record.run_attempt} for name in sorted(REQUIRED_JOBS)]
    if mutation == "failed-job":
        jobs[0]["conclusion"] = "failure"
    if mutation == "missing-job":
        jobs.pop()
    bodies = {
        "git/ref/tags/" + tag: {"ref": "refs/tags/" + tag, "object": {"type": "tag", "sha": TAG_OBJECT}},
        "git/tags/" + TAG_OBJECT: tag_body,
        run_path: run_body,
        run_path + "/jobs?per_page=100": {"total_count": len(jobs), "jobs": jobs},
    }

    def response(request: httpx.Request) -> httpx.Response:
        original = f"/repos/{REPOSITORY}/".encode()
        retained = f"/repositories/{REPOSITORY_ID}/".encode()
        raw_path = request.url.raw_path
        if raw_path.startswith(original):
            suffix = raw_path.removeprefix(original).decode("ascii")
            return httpx.Response(301, headers={"Location": f"{API}{retained.decode('ascii')}{suffix}"})
        assert raw_path.startswith(retained)
        return httpx.Response(200, json=bodies[raw_path.removeprefix(retained).decode("ascii")])

    boundary.handler = response
    return SourcePublisher(GitHubGitDataClient("fixture-read-token", REPOSITORY), REPOSITORY), record, tag_body


def test_historical_source_annotation_and_exact_run_survive_only_a_transport_move(boundary: TransportBoundary) -> None:
    publisher, record, tag_body = historical_evidence(boundary)
    annotation = tag_body["message"]
    proof = publisher.read(SOURCE_SHA)
    assert proof is not None
    assert proof.record == record
    assert proof.record.repository == REPOSITORY
    assert proof.tag_object_oid == TAG_OBJECT and proof.direct_commit_sha == SOURCE_SHA
    assert proof.tag_ref == "refs/tags/auto-coder-tested-" + SOURCE_SHA
    publisher.verify_run(proof.record, recorded_attempt=True)
    assert tag_body["message"] == annotation == serialize_record(record)
    assert len(boundary.sent) == len(boundary.admitted) == len(boundary.outcomes) == 8
    assert [entry.status for entry in boundary.outcomes] == [301, 200] * 4
    assert all(request.method == "GET" for request in boundary.sent)
    assert boundary.sent[-1].url.query == b"per_page=100"


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("annotation-repository", "tested-source tag conflicts with the requested source"),
        ("direct-commit", "tested-source tag conflicts with the requested source"),
        ("recorded-head", "workflow run is not the exact completed successful source run"),
        ("recorded-attempt", "workflow run is not the exact completed successful source run"),
        ("failed-job", "required workflow job did not succeed"),
        ("missing-job", "required workflow job evidence is missing or duplicated"),
    ),
)
def test_redirect_cannot_replace_existing_source_evidence_checks(boundary: TransportBoundary, mutation: str, message: str) -> None:
    publisher, _record, _tag_body = historical_evidence(boundary, mutation)
    with pytest.raises(SourceEvidenceError, match=message):
        proof = publisher.read(SOURCE_SHA)
        assert proof is not None
        publisher.verify_run(proof.record, recorded_attempt=True)
    assert all(request.method == "GET" for request in boundary.sent)
    assert [entry.status for entry in boundary.outcomes] == [301, 200] * (len(boundary.sent) // 2)
