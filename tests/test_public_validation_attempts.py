"""Production-origin regressions for Issue #2434 (``GET /api/validation-attempts``).

Every positive scenario starts at the real PR-processing entry (``_handle_pr_merge``),
so native attempt allocation, resolver/manifest/prompt/parser/coverage code and the
attempt-evidence capture produce the durable record that the normally mounted
``create_app`` route then serves; the assertions inspect final HTTP bytes.
Only true I/O boundaries (GitHub client, worktree, reviewer response text) are controlled.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from auto_coder import public_validation_attempts as projection
from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.execution_trace import TraceCollector, get_trace_collector
from auto_coder.review_audit import ReviewAuditStore, StorageHealth
from auto_coder.webhook_server import create_app
from tests.test_pr_adversarial_review_audit import PR_NUMBER, REPO_NAME, audit_store  # noqa: F401  (audit_store is a fixture)
from tests.test_pr_adversarial_validation_evidence import (
    Scenario,
    _issue_body,
    _sha,
    _verified_payload,
)


@pytest.fixture(autouse=True)
def _real_commands(_use_real_commands):
    """These tests drive real git boundaries; never stub them."""


@pytest.fixture(autouse=True)
def fresh_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


def _app(monkeypatch, enabled: bool = True):
    if enabled:
        monkeypatch.setenv("AUTO_CODER_PUBLIC_API_ENABLED", "1")
    else:
        monkeypatch.delenv("AUTO_CODER_PUBLIC_API_ENABLED", raising=False)
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    with patch("auto_coder.webhook_server.init_dashboard"), patch("auto_coder.webhook_server.init_dashboard_adjudication"):
        return create_app(engine, REPO_NAME)


@pytest.fixture
def client(monkeypatch, audit_store):  # noqa: F811
    return TestClient(_app(monkeypatch))


def _url(**params) -> str:
    return "/api/validation-attempts?" + "&".join(f"{key}={value}" for key, value in params.items())


def _run(tmp_path, monkeypatch, store, body=None, responses=None):
    scenario = Scenario(tmp_path, monkeypatch, store, issue_body=body or _issue_body(12), responses=responses or [_verified_payload([f"REQ-{i:03d}" for i in range(1, 13)])])
    known = {r.review_id for r in store.get_recent_history(REPO_NAME, limit=50).records}
    scenario.run()
    (record,) = [r for r in store.get_recent_history(REPO_NAME, limit=50).records if r.review_id not in known]
    return scenario, record.native_report["attempt_id"], record.native_report["attempt_sequence"]


# -- AS-001 ------------------------------------------------------------------------


def test_mismatch_is_observable_through_the_mounted_route_by_id_and_sequence(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    scenario, attempt_id, sequence = _run(tmp_path, monkeypatch, audit_store)
    unqualified = [f"REQ-{i:03d}" for i in range(1, 13)]
    qualified = [f"#99/REQ-{i:03d}" for i in range(1, 13)]

    by_id = client.get(_url(pr_number=PR_NUMBER, attempt_id=attempt_id))
    by_sequence = client.get(_url(pr_number=PR_NUMBER, attempt_sequence=sequence))
    assert by_id.status_code == by_sequence.status_code == 200
    assert by_id.headers["content-type"] == "application/json"
    body = by_id.json()
    assert by_sequence.json()["evidence"] == body["evidence"]

    assert body["schema_version"] == 1 and body["result"] == "attempt" and body["repository"] == REPO_NAME
    evidence = body["evidence"]
    identity = evidence["identity"]
    assert (identity["attempt_id"], identity["attempt_sequence"], identity["repository"], identity["pr_number"]) == (attempt_id, sequence, REPO_NAME, PR_NUMBER)
    assert identity["head_sha"] == scenario.head_sha
    assert identity["process_run_id"] == get_trace_collector().process_run_id
    assert evidence["producing_artifact"]["observation"] == "recorded_by_producing_process_at_capture"
    assert evidence["producing_artifact"]["source_revision"]["available"] is False
    assert evidence["producing_artifact"]["distribution_version"]["origin"]
    assert body["serving"]["process_run_id"] == get_trace_collector().process_run_id

    consumed = evidence["input"]
    assert consumed["pr_body"]["sha256"] == _sha(scenario.pr_data["body"])
    issue = consumed["resolved_issues"][0]
    assert issue["body"]["sha256"] == _sha(scenario.issue_body) and issue["github_issue"] == f"https://github.com/{REPO_NAME}/issues/99"
    supplied = next(m for m in evidence["manifests"]["items"] if m["role"] == "supplied")
    checked = next(m for m in evidence["manifests"]["items"] if m["role"] == "checked")
    assert [e["id"] for e in supplied["entries"]] == qualified == [e["id"] for e in checked["entries"]]
    assert supplied["identity_sha256"] == checked["identity_sha256"] and supplied["mode"] == "explicit-contract"
    response = evidence["responses"]["items"][0]
    assert response["response"]["sha256"] == _sha(_verified_payload(unqualified))
    assert response["prompt"]["sha256"] == _sha(scenario.reviewer.prompts[0])
    assert [(e["id"], e["status"]) for e in response["parse"]["returned_entries"]] == [(rid, "VERIFIED") for rid in unqualified]
    assert response["interaction"]["backend_alias"] == "reviewer"
    check = evidence["coverage_checks"]["items"][0]
    assert check["returned"]["ids"] == unqualified and check["expected"]["ids"] == qualified and check["unknown"]["ids"] == unqualified
    assert check["unknown"]["count"]["source_count"] == 12 and check["unknown"]["count"]["http_clipped"] == 0
    assert (check["verdict_before"], check["verdict_after"], check["diagnostic_category"]) == ("PASS", "ERROR", "unknown_requirement_coverage_id")
    final = evidence["final"]
    assert (final["verdict"], final["kind"], final["source_response_id"]) == ("ERROR", "semantic_response", "r1")
    assert body["http_limits"]["clipped"] is False

    # Final bytes: no raw content, only fingerprints.
    for forbidden in (scenario.pr_data["body"], scenario.issue_body, scenario.reviewer.prompts[0][:120], '"summary"', "raw_response_preview"):
        assert forbidden not in by_id.text


# -- AS-002 ------------------------------------------------------------------------


def test_exact_selection_never_mixes_attempts_prs_or_processes(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    first, first_id, first_seq = _run(tmp_path / "a", monkeypatch, audit_store, responses=[_verified_payload(["REQ-001"])])
    second = Scenario(tmp_path / "b", monkeypatch, audit_store, issue_body=_issue_body(12, "Other"), responses=[_verified_payload([f"REQ-{i:03d}" for i in range(1, 13)])])
    second.run()
    records = {r.native_report["attempt_id"]: r for r in audit_store.get_recent_history(REPO_NAME, limit=50).records}
    second_id = next(i for i in records if i != first_id)
    second_seq = records[second_id].native_report["attempt_sequence"]
    assert first_seq != second_seq

    for attempt_id, sequence, expected_sha in ((first_id, first_seq, _sha(first.issue_body)), (second_id, second_seq, _sha(second.issue_body))):
        for selector in ({"attempt_id": attempt_id}, {"attempt_sequence": sequence}):
            body = client.get(_url(pr_number=PR_NUMBER, **selector)).json()
            assert body["evidence"]["identity"]["attempt_id"] == attempt_id
            assert body["evidence"]["input"]["resolved_issues"][0]["body"]["sha256"] == expected_sha

    for wrong in (_url(pr_number=PR_NUMBER + 1, attempt_id=first_id), _url(pr_number=PR_NUMBER, attempt_id="0" * 32), _url(pr_number=PR_NUMBER, attempt_sequence=first_seq + 1000)):
        body = client.get(wrong).json()
        assert (body["result"], body["evidence"]) == ("no_retained_match", None)

    # A different repository's app never sees this repository's record.
    monkeypatch.setenv("AUTO_CODER_PUBLIC_API_ENABLED", "1")
    with patch("auto_coder.webhook_server.init_dashboard"), patch("auto_coder.webhook_server.init_dashboard_adjudication"):
        other = TestClient(create_app(AutomationEngine(MagicMock(), config=AutomationConfig()), "other/repo"))
    assert other.get(_url(pr_number=PR_NUMBER, attempt_id=first_id)).json()["result"] == "audit_not_initialized"

    before = client.get(_url(pr_number=PR_NUMBER, attempt_id=first_id)).json()
    old_process = get_trace_collector().process_run_id
    TraceCollector._instance = None  # a restarted serving process over the preserved audit root
    restarted = TestClient(_app(monkeypatch))
    after = restarted.get(_url(pr_number=PR_NUMBER, attempt_id=first_id))
    assert after.status_code == 200
    after_body = after.json()
    assert after_body["evidence"] == before["evidence"]
    assert after_body["evidence"]["identity"]["process_run_id"] == old_process != after_body["serving"]["process_run_id"]
    assert after_body["evidence"]["producing_artifact"]["process_run_id"] == old_process
    # The process-local log history is separate and reset.
    assert restarted.get("/api/logs").json()["retention_scope"] == "process_local_bounded"


# -- AS-003 ------------------------------------------------------------------------


def _update_payload(store: ReviewAuditStore, mutate) -> None:
    connection = sqlite3.connect(store._get_db_path(REPO_NAME))
    with connection:
        (raw,) = connection.execute("SELECT payload FROM validation_evidence").fetchone()
        payload = json.loads(raw)
        mutate(payload)
        connection.execute("UPDATE validation_evidence SET payload = ?", (json.dumps(payload),))
    connection.close()


def test_unavailable_evidence_states_are_distinct_and_not_initialized_by_reads(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    root = audit_store._audit_root
    uninitialized = client.get(_url(pr_number=PR_NUMBER, attempt_sequence=1)).json()
    assert uninitialized["result"] == "audit_not_initialized" and uninitialized["evidence"] is None
    assert not root.exists() or not any(root.iterdir())

    scenario, attempt_id, sequence = _run(tmp_path, monkeypatch, audit_store)
    url = _url(pr_number=PR_NUMBER, attempt_id=attempt_id)

    def interrupt(payload):
        payload["completeness"] = "partial"
        payload["unrecorded"] = ["response", "coverage_check", "final_result"]
        for key in ("responses", "coverage_checks"):
            payload[key] = []
        payload.pop("final")

    _update_payload(audit_store, interrupt)
    connection = sqlite3.connect(audit_store._get_db_path(REPO_NAME))
    with connection:
        connection.execute("UPDATE validation_evidence SET completeness = 'partial'")
    partial = client.get(url).json()
    evidence = partial["evidence"]
    assert evidence["record"]["completeness"] == "partial" and evidence["input"]["state"]["availability"] == "available"
    assert evidence["responses"]["state"]["availability"] == "not_recorded" and evidence["responses"]["items"] == []
    assert evidence["coverage_checks"]["state"]["availability"] == "not_recorded"
    assert evidence["final"]["state"]["availability"] == "not_recorded" and evidence["final"]["verdict"] is None

    # A pre-feature audit: the evaluation retained the attempt but not the extension.
    with connection:
        connection.execute("DELETE FROM validation_evidence")
    pre = client.get(url).json()
    assert (pre["result"], pre["reason"]) == ("evidence_unavailable", "pre_feature_audit_record")
    known = pre["evidence"]
    assert (known["identity"]["attempt_id"], known["identity"]["attempt_sequence"], known["identity"]["head_sha"]) == (attempt_id, sequence, scenario.head_sha)
    assert known["identity"]["review_id"] and known["identity"]["repository"] == REPO_NAME
    for section in ("input", "manifests", "responses", "coverage_checks", "final"):
        assert known[section]["state"] == {"availability": "unavailable", "reason": "pre_feature_capture_not_recorded", "incomplete": True}
    assert client.get(_url(pr_number=PR_NUMBER, attempt_sequence=sequence)).json()["evidence"] == known  # either selector, same metadata
    assert "sha256" not in json.dumps(known)  # nothing is reconstructed
    with connection:
        connection.execute("DROP TABLE validation_evidence")
    assert client.get(url).json()["evidence"] == known
    connection.close()


def test_failed_reads_and_projection_return_503_with_null_data(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    scenario, attempt_id, sequence = _run(tmp_path, monkeypatch, audit_store)
    url = _url(pr_number=PR_NUMBER, attempt_id=attempt_id)
    assert client.get(url).status_code == 200

    def assert_unavailable(response):
        assert response.status_code == 503
        assert response.json() == {"repository": REPO_NAME, "error_code": "observation_unavailable", "message": "Validation-attempt evidence could not be read or projected.", "data": None, "schema_version": 1}

    real = audit_store._connect_readonly
    monkeypatch.setattr(audit_store, "_connect_readonly", lambda repository: (None, StorageHealth.UNAVAILABLE))
    assert_unavailable(client.get(url))  # after a previously successful request: no cached last-known data
    monkeypatch.setattr(audit_store, "_connect_readonly", real)
    assert client.get(url).status_code == 200

    connection = sqlite3.connect(audit_store._get_db_path(REPO_NAME))
    with connection:
        connection.execute("UPDATE validation_evidence SET payload = 'not json'")
    assert_unavailable(client.get(url))
    with connection:
        connection.execute("UPDATE validation_evidence SET payload = '{}', schema_version = 99")
    assert_unavailable(client.get(url))
    connection.close()

    def explode(self, payload):
        raise RuntimeError("secret internal detail /var/lib/private")

    other, other_id, _ = _run(tmp_path / "again", monkeypatch, audit_store)
    monkeypatch.setattr(projection._Projector, "build", explode)
    failed = client.get(_url(pr_number=PR_NUMBER, attempt_id=other_id))
    assert_unavailable(failed)
    assert "secret internal detail" not in failed.text and "/var/lib" not in failed.text


# -- AS-004 ------------------------------------------------------------------------


def test_public_projection_is_bounded_redacted_and_never_rewrites_the_source(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    secret = "configured-secret-value-12345"
    monkeypatch.setenv("REVIEW_SERVICE_API_TOKEN", secret)
    token = "ghp_" + "a" * 36
    marker = "UNIQUE-ISSUE-BODY-MARKER"
    unknown = ["/etc/passwd", "https://example.com/private/path", "C:\\Users\\victim\\file.txt", f"id-{token}", f"id-{secret}", "x" * 3000, "REQ-777", "#99/REQ-778"] + [f"UNKNOWN-{index:04d}" for index in range(700)]
    scenario, attempt_id, _ = _run(tmp_path, monkeypatch, audit_store, body=_issue_body(600, marker), responses=[_verified_payload(unknown)])
    connection = sqlite3.connect(audit_store._get_db_path(REPO_NAME))
    stored_before = connection.execute("SELECT payload FROM validation_evidence").fetchone()[0]
    # Extra unknown/raw fields in the stored record must never be exported.
    _update_payload(audit_store, lambda payload: payload.update({"raw_response_preview": "RAW-PREVIEW-TEXT", "local_log_path": "/home/user/.auto-coder/logs/run.log", "extra": {"nested": f"Bearer {token}"}}))
    stored_before = connection.execute("SELECT payload FROM validation_evidence").fetchone()[0]

    response = client.get(_url(pr_number=PR_NUMBER, attempt_id=attempt_id))
    assert response.status_code == 200
    assert len(response.content) <= 256 * 1024
    for forbidden in (secret, token, marker, "/etc/passwd", "example.com/private", "victim", "RAW-PREVIEW-TEXT", "local_log_path", "/home/user", "extra", scenario.reviewer.prompts[0][:200]):
        assert forbidden not in response.text, forbidden
    body = response.json()
    parse = body["evidence"]["responses"]["items"][0]["parse"]
    count = parse["returned_entries_count"]
    assert count["source_count"] == len(unknown) and len(parse["returned_entries"]) <= 500
    assert count["retained_in_source"] >= len(parse["returned_entries"]) and count["source_omitted"] == len(unknown) - count["retained_in_source"]
    assert count["incomplete"] is True and count["returned"] == len(parse["returned_entries"])
    entries = {entry["id"] if isinstance(entry["id"], str) else None: entry for entry in parse["returned_entries"]}
    assert "REQ-777" in entries and "#99/REQ-778" in entries  # supported grammar stays plaintext
    omitted = [entry["id"] for entry in parse["returned_entries"] if isinstance(entry["id"], dict)]
    assert omitted and all(item["omitted"] is True and item["reason"] for item in omitted)
    assert "value" not in json.dumps(omitted)
    assert {"not_a_supported_requirement_id"} <= {item["reason"] for item in omitted} or any(item["reason"].startswith("source_omitted") for item in omitted)
    assert all(len(json.dumps(item)) < 400 for item in omitted)
    manifests = {m["role"]: m for m in body["evidence"]["manifests"]["items"]}
    assert manifests["supplied"]["entries_count"]["source_count"] == 600 and len(manifests["supplied"]["entries"]) <= 500
    final = body["evidence"]["final"]
    assert final["diagnostic_reason"] is None and final["diagnostic_reason_omitted"]["reason"] == "may_quote_omitted_identity"  # omitted IDs may be quoted in the reason
    assert len(final["diagnostic_reason_omitted"]["sha256"]) == 64 and final["diagnostic_reason_omitted"]["byte_length"] > 0
    assert body["evidence"]["manifests"]["state"]["incomplete"] is True  # nested omitted entries propagate to the section
    assert body["evidence"]["source_limits"]["omissions"]  # capture-time loss is reported separately
    assert connection.execute("SELECT payload FROM validation_evidence").fetchone()[0] == stored_before
    connection.close()


def test_malformed_returned_id_is_absent_from_every_public_byte(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    marker = "PRIVATE-RETURNED-ID-WITH-ISSUE-TEXT"
    scenario, attempt_id, _ = _run(tmp_path, monkeypatch, audit_store, body=_issue_body(1), responses=[_verified_payload([marker])])
    stored = sqlite3.connect(audit_store._get_db_path(REPO_NAME)).execute("SELECT payload FROM validation_evidence").fetchone()[0]
    assert marker in stored  # the diagnostic reason recorded at capture quotes it; only the public projection must not
    response = client.get(_url(pr_number=PR_NUMBER, attempt_id=attempt_id))
    assert response.status_code == 200 and marker not in response.text
    evidence = response.json()["evidence"]
    check = evidence["coverage_checks"]["items"][0]
    omitted = check["unknown"]["ids"][0]
    assert omitted["reason"] == "not_a_supported_requirement_id" and omitted["sha256"] == _sha(marker) and omitted["byte_length"] == len(marker)
    assert check["diagnostic_category"] == "unknown_requirement_coverage_id" and check["diagnostic_reason"] is None
    assert check["diagnostic_reason_omitted"]["reason"] == "may_quote_omitted_identity"
    assert evidence["final"]["diagnostic_reason"] is None and evidence["final"]["diagnostic_reason_omitted"]["sha256"]


def test_outer_capture_omissions_and_nested_loss_propagate_to_section_state(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    _scenario, attempt_id, _ = _run(tmp_path, monkeypatch, audit_store)
    url = _url(pr_number=PR_NUMBER, attempt_id=attempt_id)
    evidence = client.get(url).json()["evidence"]
    assert evidence["manifests"]["state"]["incomplete"] is False and evidence["manifests"]["count"]["source_omitted"] == 0

    def record_outer_omission(payload):
        payload["limits"]["omissions"].append({"section": "responses", "source_count": 3, "retained": 1, "omitted": 2})
        payload["manifests"][0]["count"] = 99  # nested loss: 99 entries originally, fewer retained

    _update_payload(audit_store, record_outer_omission)
    body = client.get(url).json()["evidence"]
    responses = body["responses"]
    assert responses["count"] == {"source_count": 3, "retained_in_source": 1, "returned": 1, "source_omitted": 2, "http_clipped": 0, "incomplete": True}
    assert responses["state"]["incomplete"] is True and responses["state"]["reason"] == "capture_omitted_entries"
    assert body["manifests"]["state"]["incomplete"] is True and body["manifests"]["items"][0]["entries_count"]["source_omitted"] > 0


def test_byte_bound_clips_collections_but_keeps_identity_and_verdicts(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    scenario, attempt_id, _ = _run(tmp_path, monkeypatch, audit_store)

    def inflate(payload):
        checks = payload["coverage_checks"]
        template = checks[0]
        for index in range(1, 60):
            clone = json.loads(json.dumps(template))
            clone["check_id"] = f"c{index + 10}"
            checks.append(clone)
        for entry in payload["manifests"]:
            entry["entries"] = entry["entries"] * 40
            entry["count"] = len(entry["entries"])

    _update_payload(audit_store, inflate)
    monkeypatch.setattr(projection, "MAX_RESPONSE_BYTES", 30 * 1024)
    response = client.get(_url(pr_number=PR_NUMBER, attempt_id=attempt_id))
    assert response.status_code == 200 and len(response.content) <= 30 * 1024
    body = response.json()
    assert body["http_limits"]["clipped"] is True and body["http_limits"]["applied_entry_cap"] < 500
    assert body["evidence"]["identity"]["attempt_id"] == attempt_id
    assert body["evidence"]["final"]["verdict"] == "ERROR"
    checks = body["evidence"]["coverage_checks"]
    assert checks["count"]["source_count"] == 61 and checks["count"]["http_clipped"] > 0 and checks["count"]["incomplete"] is True


# -- AS-005 ------------------------------------------------------------------------


def test_requests_are_validated_and_reads_do_not_change_state(tmp_path, monkeypatch, audit_store, client):  # noqa: F811
    _scenario, attempt_id, sequence = _run(tmp_path, monkeypatch, audit_store)
    connection = sqlite3.connect(audit_store._get_db_path(REPO_NAME))
    snapshot = lambda: [connection.execute(f"SELECT * FROM {t}").fetchall() for t in ("evaluation", "interaction", "effect", "validation_evidence")]  # noqa: E731
    before = snapshot()
    for _ in range(3):
        assert client.get(_url(pr_number=PR_NUMBER, attempt_id=attempt_id)).status_code == 200

    marker = "SECRET-CALLER-TEXT"
    invalid = [
        "/api/validation-attempts",
        _url(pr_number=PR_NUMBER),
        _url(pr_number=PR_NUMBER, attempt_id=attempt_id, attempt_sequence=sequence),
        _url(pr_number=0, attempt_sequence=1),
        _url(pr_number="abc" + marker, attempt_sequence=1),
        _url(pr_number=PR_NUMBER, attempt_sequence=0),
        _url(pr_number=PR_NUMBER, attempt_sequence=-1),
        _url(pr_number=PR_NUMBER, attempt_id="Z" * 40 + marker),
        _url(pr_number=PR_NUMBER, attempt_id=attempt_id.upper()),
        f"/api/validation-attempts?pr_number={PR_NUMBER}&pr_number={PR_NUMBER}&attempt_sequence=1",
        _url(pr_number=PR_NUMBER, attempt_sequence=1) + "&repository=other/repo",
        _url(pr_number=PR_NUMBER, attempt_sequence=1) + f"&path=/etc/passwd&url=http://x/{marker}&command=rm",
        _url(pr_number=PR_NUMBER, attempt_sequence="9" * 40),
    ]
    for path in invalid:
        response = client.get(path)
        assert response.status_code == 422, path
        assert response.json()["error"]["code"] == "invalid_request" and marker not in response.text and "other/repo" not in response.text
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)(_url(pr_number=PR_NUMBER, attempt_id=attempt_id)).status_code == 405
    assert snapshot() == before
    connection.close()


def test_disabled_route_is_404_before_method_and_query_validation(monkeypatch, audit_store):  # noqa: F811
    disabled = TestClient(_app(monkeypatch, enabled=False))
    for method in ("get", "post"):
        assert getattr(disabled, method)("/api/validation-attempts?bogus=1").status_code == 404
    assert not audit_store._audit_root.exists() or not any(audit_store._audit_root.iterdir())


def test_index_documents_the_route(client):
    route = client.get("/api/").json()["routes"]["validation_attempts"]
    assert route["path"] == "/api/validation-attempts" and set(route["parameters"]) == {"pr_number", "attempt_id", "attempt_sequence"}
    assert "validation_attempts" in client.get("/api/").json()["meanings"]
    assert client.get("/api/status").status_code == 200 and client.get("/api/logs").status_code == 200


def test_blocked_audit_read_does_not_block_the_event_loop(tmp_path, monkeypatch, audit_store):  # noqa: F811
    _scenario, attempt_id, _ = _run(tmp_path, monkeypatch, audit_store)
    asyncio.run(_blocked_read(monkeypatch, audit_store, attempt_id))


async def _blocked_read(monkeypatch, store, attempt_id):
    import httpx

    app = _app(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    real = store.get_validation_evidence

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "get_validation_evidence", blocked)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        task = asyncio.create_task(c.get(_url(pr_number=PR_NUMBER, attempt_id=attempt_id)))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        assert (await c.get("/api/")).status_code == 200 and not task.done()  # served while the read is held
        release.set()
        assert (await task).status_code == 200
