"""Tests for the anonymous read-only public diagnostic API, mounted via the real ``create_app``."""

import asyncio
import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from src.auto_coder.automation_config import AutomationConfig, Candidate
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from src.auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from src.auto_coder.webhook_server import create_app

REPO = "owner/repo"


@pytest.fixture(autouse=True)
def fresh_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


@pytest.fixture
def engine(tmp_path):
    eng = AutomationEngine(MagicMock(), config=AutomationConfig())
    eng.implementation_slots = ImplementationSlotRepository(REPO, 3, tmp_path / "slots.json")
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_PUBLIC_API_ENABLED", "1")
    with patch("src.auto_coder.webhook_server.init_dashboard"), patch("src.auto_coder.webhook_server.init_dashboard_adjudication"):
        yield TestClient(create_app(engine, REPO))


def _trace(item_type="issue", number=7, outcome=Outcome.BLOCKED, reason="waiting for slot"):
    collector = get_trace_collector()
    with collector.start_execution(REPO, item_type, number, origin="worker") as execution:
        collector.record_event(EventKind.STAGE_RESULT, "pr.ci-observation", "worker", label="CI observed", outcome=outcome, facts={"reason": reason, "secret_blob": "x", "exit_code": 3})
        execution.finish(Outcome.COMPLETED)
    return execution.scope


def test_disabled_returns_404_before_validation(engine, monkeypatch):
    monkeypatch.delenv("AUTO_CODER_PUBLIC_API_ENABLED", raising=False)
    with patch("src.auto_coder.webhook_server.init_dashboard"), patch("src.auto_coder.webhook_server.init_dashboard_adjudication"):
        c = TestClient(create_app(engine, REPO))
    for path in ("/api/", "/api/status", "/api/logs?bogus=1"):
        assert c.get(path).status_code == 404
        assert c.post(path).status_code == 404


def test_anonymous_index_follows_to_logs_and_sees_new_events(client):
    index = client.get("/api/").json()
    assert index["schema_version"] == 1 and index["repository"] == REPO
    assert index["process_run_id"] == get_trace_collector().process_run_id
    scope = _trace(number=7)
    body = client.get(index["routes"]["logs"]["path"]).json()
    assert [e["sequence"] for e in body["events"]] == sorted(e["sequence"] for e in body["events"])
    result = next(e for e in body["events"] if e["stage_id"] == "pr.ci-observation")
    assert (result["item_type"], result["item_number"], result["outcome"]) == ("issue", 7, "blocked")
    assert result["facts"] == {"reason": "waiting for slot", "exit_code": 3}
    assert result["execution_id"] == scope.execution_id
    finished = next(e for e in body["events"] if e["kind"] == "execution-finished")
    assert finished["outcome"] == "completed"
    _trace(number=8)
    again = client.get("/api/logs?item_type=issue&item_number=8").json()
    assert {e["item_number"] for e in again["events"]} == {8}
    assert "secret_blob" not in json.dumps(body)


def test_filters_keep_identity_and_unscoped_stays_unscoped(client):
    a = _trace("issue", 5)
    b = _trace("issue", 5)
    _trace("pr", 5)
    collector = get_trace_collector()
    collector.record_event(EventKind.STAGE_RESULT, "free", "worker", label="unscoped")
    collector.record_legacy_or_raw({"repository": "", "label": "raw legacy", "kind": "legacy"})
    run = collector.process_run_id
    rows = client.get(f"/api/logs?item_type=issue&item_number=5&execution_id={a.execution_id}&process_run_id={run}").json()["events"]
    assert rows and {e["execution_id"] for e in rows} == {a.execution_id}
    assert b.execution_id not in json.dumps(rows)
    assert {e["item_type"] for e in client.get("/api/logs?item_type=pr&item_number=5").json()["events"]} == {"pr"}
    unfiltered = client.get("/api/logs").json()["events"]
    unscoped = [e for e in unfiltered if e["scope"] == "unscoped"]
    assert [e["label"] for e in unscoped] == ["unscoped"]
    assert unscoped[0]["execution_id"] is None and unscoped[0]["item_number"] is None
    assert "raw legacy" not in json.dumps(unfiltered)
    empty = client.get("/api/logs?item_type=issue&item_number=999").json()
    assert (empty["availability"], empty["result"], empty["events"]) == ("available", "no_retained_match", [])


def test_other_repository_events_are_excluded(client):
    get_trace_collector().start_execution("other/repo", "issue", 1, origin="worker").finish(Outcome.FAILED)
    assert client.get("/api/logs?item_type=issue&item_number=1").json()["events"] == []


def test_process_run_conflict_and_validation(client):
    _trace()
    assert client.get("/api/logs?execution_id=abc&process_run_id=old").status_code == 409
    assert client.get("/api/logs?execution_id=abc").status_code == 422
    for query in ("limit=0", "limit=-1", "limit=x", "limit=501", "item_type=issue", "item_number=3", "item_type=bug&item_number=3", "item_type=pr&item_number=0", "repo=other/repo", "path=/etc/passwd", "url=http://x", "limit=1&limit=2"):
        response = client.get(f"/api/logs?{query}")
        assert response.status_code == 422, query
        assert "etc/passwd" not in response.text and "other/repo" not in response.text
    assert client.get("/api/status?item_type=issue").status_code == 422
    for method in ("post", "put", "patch", "delete"):
        for path in ("/api/", "/api/status", "/api/logs"):
            assert getattr(client, method)(path).status_code == 405


def test_collector_failure_is_503(client):
    with patch.object(TraceCollector, "get_snapshot", side_effect=RuntimeError("secret ghp_abcdef path /srv")):
        response = client.get("/api/logs")
    assert response.status_code == 503 and response.json()["error"]["code"] == "observation_unavailable"
    assert "ghp_" not in response.text and "/srv" not in response.text


def test_limit_selects_newest_matching_in_ascending_order(client):
    for n in range(4):
        _trace("issue", 9)
    _trace("issue", 10)
    body = client.get("/api/logs?item_type=issue&item_number=9&limit=3").json()
    seqs = [e["sequence"] for e in body["events"]]
    assert len(seqs) == 3 and seqs == sorted(seqs)
    assert {e["item_number"] for e in body["events"]} == {9}
    assert body["response"]["truncated"] is True and body["response"]["matching_count"] == 12
    assert body["events_truncated"] is False


def test_redaction_before_clipping_and_projection(client):
    secrets = [
        "ghp_" + "a" * 20,
        "github_pat_" + "b" * 20,
        "AIza" + "c" * 35,
        "sk-" + "d" * 20,
        "AKIA" + "E" * 16,
        "xoxb-" + "f" * 12,
        "glpat-" + "g" * 20,
    ]
    reason = "harmless reason " + " ".join(secrets) + " Bearer tok123 Basic dXNlcjpwYXNz https://user:pw@host/x?token=zzz"
    _trace(reason=reason)
    collector = get_trace_collector()
    boundary = "x" * 1995 + "ghp_" + "z" * 30
    _trace(number=11, reason=boundary)
    raw = client.get("/api/logs").text
    for secret in secrets + ["tok123", "dXNlcjpwYXNz", "user:pw", "token=zzz", "zzzzzzzz"]:
        assert secret not in raw
    assert "harmless reason" in raw and "[REDACTED]" in raw and "[REDACTED_URL]" in raw
    events = json.loads(raw)["events"]
    assert any(e["filtered"] for e in events)
    clipped = [e for e in events if e["item_number"] == 11 and e["stage_id"] == "pr.ci-observation"][0]
    assert clipped["text_truncated"] is True and len(clipped["facts"]["reason"]) <= 2000
    assert "ghp_" not in clipped["facts"]["reason"]
    assert collector.get_snapshot().events[1].facts["reason"] == reason  # source untouched


def test_byte_bound_and_label_clipping(client):
    for n in range(200):
        _trace("issue", 1, reason="r" * 1900)
    response = client.get("/api/logs?limit=500")
    assert len(response.content) <= 256 * 1024
    body = response.json()
    assert body["response"]["truncated"] is True and body["response"]["returned_count"] == len(body["events"])
    seqs = [e["sequence"] for e in body["events"]]
    assert seqs == sorted(seqs) and seqs[-1] == get_trace_collector().get_snapshot().events[-1].sequence


def test_status_workers_queue_slots_independent(client, engine, tmp_path):
    slots = engine.implementation_slots
    owner = ImplementationOwner("issue", 3)
    assert slots.reserve(owner)
    engine.active_workers[0] = None
    engine.active_workers[1] = Candidate(type="dependency", data={"number": 1, "title": "t"}, priority=0)
    engine.queue.put_nowait(Candidate(type="pr", data={"number": 4, "title": "secret title"}, priority=1))
    body = client.get("/api/status").json()
    assert body["repository"] == REPO
    workers = {w["worker_id"]: w for w in body["workers"]["entries"]}
    assert workers[0]["state"] == "idle" and workers[0]["target"] is None
    assert workers[1]["target"] == {"type": "dependency", "number": 1}
    assert body["queue"]["entries"] == [{"target": {"type": "pr", "number": 4}, "priority": 1}]
    s = body["implementation_slots"]
    assert s["availability"] == "available" and s["normal_usage"] == 1 and s["normal_limit"] == 3
    assert s["owners"][0]["owner_type"] == "issue" and s["owners"][0]["owner_number"] == 3
    assert s["owners"][0]["admission_pending"] is None or isinstance(s["owners"][0]["admission_pending"], bool)
    assert "secret title" not in json.dumps(body) and "slots.json" not in json.dumps(body)
    clipped = client.get("/api/status?limit=1").json()
    assert len(clipped["workers"]["entries"]) == 1 and clipped["workers"]["truncated"] is True and clipped["workers"]["total_count"] == 2
    # The slot remains owned with no local worker; reading never frees it.
    assert slots.active_owners() == (owner,)


def test_status_slot_failure_is_unavailable_with_null_data(client, engine):
    with patch.object(AutomationEngine, "get_implementation_slot_snapshot", side_effect=RuntimeError("boom /srv/x")):
        response = client.get("/api/status")
    body = response.json()
    assert response.status_code == 200
    slots = body["implementation_slots"]
    assert (slots["availability"], slots["error_code"], slots["owners"], slots["normal_usage"]) == ("unavailable", "slot_observation_unavailable", None, None)
    assert body["workers"]["availability"] == "available" and "/srv/x" not in response.text


def test_over_capacity_usage_not_clamped(client, engine, tmp_path):
    for n in (1, 2, 3):
        engine.implementation_slots.reserve(ImplementationOwner("issue", n))
    engine.implementation_slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    s = client.get("/api/status").json()["implementation_slots"]
    assert s["normal_usage"] == 3 and s["normal_limit"] == 1


def test_get_requests_cause_no_operational_effect(client, engine):
    engine.github.reset_mock()
    engine.invalidate_entity = MagicMock()
    before = (len(get_trace_collector().get_snapshot().events), engine.queue.qsize())
    for path in ("/api/", "/api/status", "/api/logs", "/api/logs?limit=0", "/api/logs?path=/etc/passwd"):
        client.get(path)
    client.post("/api/status")
    assert (len(get_trace_collector().get_snapshot().events), engine.queue.qsize()) == before
    assert engine.github.method_calls == [] and not engine.invalidate_entity.called


def test_blocked_slot_read_does_not_block_event_loop(engine, monkeypatch):
    asyncio.run(_blocked_slot_read(engine, monkeypatch))


async def _blocked_slot_read(engine, monkeypatch):
    import httpx

    monkeypatch.setenv("AUTO_CODER_PUBLIC_API_ENABLED", "1")
    with patch("src.auto_coder.webhook_server.init_dashboard"), patch("src.auto_coder.webhook_server.init_dashboard_adjudication"):
        app = create_app(engine, REPO)
    entered, release = threading.Event(), threading.Event()
    real = engine.get_implementation_slot_snapshot

    def blocked(repo):
        entered.set()
        release.wait(10)
        return real(repo)

    engine.get_implementation_slot_snapshot = blocked
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        task = asyncio.create_task(c.get("/api/status"))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        progressed = (await c.get("/api/")).status_code == 200  # served while the slot read is held
        assert progressed and not task.done()
        release.set()
        assert (await task).status_code == 200


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_value_is_redacted_in_http_response_bytes(client, scheme):
    """A live Bearer credential recorded through the collector never reaches the anonymous response."""
    reason = f"harmless control phrase; auth header {scheme} secret123 retained"
    _trace(reason=reason)
    response = client.get("/api/logs")
    assert "secret123" not in response.text
    assert "harmless control phrase" in response.text and "[REDACTED]" in response.text
    event = next(e for e in response.json()["events"] if e["stage_id"] == "pr.ci-observation")
    assert event["filtered"] is True and "secret123" not in event["facts"]["reason"]
    # The collector source still holds the original, unredacted text.
    stored = [e for e in get_trace_collector().get_snapshot().events if e.stage_id == "pr.ci-observation"]
    assert stored[0].facts["reason"] == reason
