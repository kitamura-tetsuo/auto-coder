"""Incremental /api/logs pages, driven over HTTP against the real collector and ``create_app``."""

import base64
import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from src.auto_coder import public_api
from src.auto_coder.automation_config import AutomationConfig, Candidate
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from src.auto_coder.implementation_slots import ImplementationSlotRepository
from src.auto_coder.webhook_server import create_app

REPO = "owner/repo"
MAX_BYTES = 256 * 1024


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


def _unscoped(n, label="x"):
    collector = get_trace_collector()
    for i in range(n):
        collector.record_event(EventKind.STAGE_RESULT, "free", "worker", label=f"{label}{i}")


def _run():
    return get_trace_collector().process_run_id


def _walk(client, query, limit=None):
    """Follow cursors to exhaustion; returns (pages, raw bodies)."""
    pages, sizes = [], []
    response = client.get(query)
    while True:
        assert response.status_code == 200, response.text
        sizes.append(len(response.content))
        body = response.json()
        pages.append(body)
        if not body["has_more"]:
            assert body["next_cursor"] is None
            return pages, sizes
        suffix = f"&limit={limit}" if limit else ""
        response = client.get(f"/api/logs?cursor={body['next_cursor']}{suffix}")


def test_observed_gap_is_fully_readable_across_bounded_pages(client):
    _unscoped(1915, label="L" * 1900)
    pages, sizes = _walk(client, f"/api/logs?after_sequence=290&process_run_id={_run()}&limit=500")
    seqs = [e["sequence"] for p in pages for e in p["events"]]
    assert seqs == list(range(291, 1916))
    assert max(sizes) <= MAX_BYTES and len(pages) > 3
    assert all(p["snapshot_upper_sequence"] == 1915 for p in pages)
    assert all(len(p["events"]) <= 500 for p in pages)
    for prev, nxt in zip(pages, pages[1:]):
        assert prev["has_more"] and prev["next_after_sequence"] == prev["events"][-1]["sequence"]
        assert nxt["after_sequence"] == prev["next_after_sequence"]
    assert pages[-1]["next_after_sequence"] == 1915 and pages[0]["response"]["truncated"] is True
    assert pages[0]["events_truncated"] is False


def test_concurrent_publication_does_not_move_the_finishing_line(client):
    _unscoped(30)
    first = client.get(f"/api/logs?after_sequence=0&process_run_id={_run()}&limit=10").json()
    assert first["snapshot_upper_sequence"] == 30 and first["has_more"]
    _unscoped(5, label="late")  # published after the snapshot, before the next page
    pages, _ = _walk(client, f"/api/logs?cursor={first['next_cursor']}", limit=10)
    seqs = [e["sequence"] for e in first["events"]] + [e["sequence"] for p in pages for e in p["events"]]
    assert seqs == list(range(1, 31)) and all(p["snapshot_upper_sequence"] == 30 for p in pages)
    retry = client.get(f"/api/logs?cursor={first['next_cursor']}&limit=10").json()
    assert [e["sequence"] for e in retry["events"]] == list(range(11, 21))
    later = client.get(f"/api/logs?after_sequence=30&process_run_id={_run()}").json()
    assert [e["sequence"] for e in later["events"]] == list(range(31, 36)) and not later["has_more"]


def test_filters_empty_interval_and_allocated_hole(client):
    collector = get_trace_collector()

    class Boom:
        def __deepcopy__(self, memo):
            raise RuntimeError("no copy")

    a = collector.start_execution(REPO, "issue", 5, origin="worker")
    p = collector.start_execution(REPO, "pr", 5, origin="worker")
    other = collector.start_execution("other/repo", "issue", 5, origin="worker")
    collector.record_event(EventKind.STAGE_RESULT, "free", "worker", facts={"reason": Boom()})  # consumes a sequence, stores nothing
    collector.record_event(EventKind.STAGE_RESULT, "free", "worker", label="unscoped")
    a.finish(Outcome.COMPLETED)
    p.finish(Outcome.FAILED)
    other.finish(Outcome.FAILED)
    run = _run()
    rows = client.get(f"/api/logs?after_sequence=0&process_run_id={run}&item_type=issue&item_number=5&execution_id={a.scope.execution_id}").json()
    assert {e["execution_id"] for e in rows["events"]} == {a.scope.execution_id} and rows["has_more"] is False
    assert {e["item_type"] for e in client.get(f"/api/logs?after_sequence=0&process_run_id={run}&item_type=pr&item_number=5").json()["events"]} == {"pr"}
    everything = client.get(f"/api/logs?after_sequence=0&process_run_id={run}").json()
    assert [e["label"] for e in everything["events"] if e["scope"] == "unscoped"] == ["unscoped"]
    assert REPO not in json.dumps([e for e in everything["events"] if e["scope"] == "unscoped"])
    seqs = [e["sequence"] for e in everything["events"]]
    assert any(b - a_ > 1 for a_, b in zip(seqs, seqs[1:]))  # hole is not loss
    empty = client.get(f"/api/logs?after_sequence=0&process_run_id={run}&item_type=issue&item_number=999").json()
    assert (empty["events"], empty["result"], empty["has_more"], empty["next_cursor"]) == ([], "no_retained_match", False, None)
    assert empty["next_after_sequence"] == empty["snapshot_upper_sequence"]


def test_invalid_requests_are_bounded_and_non_echoing(client):
    _unscoped(3)
    run = _run()
    cursor = client.get(f"/api/logs?after_sequence=0&process_run_id={run}&limit=1").json()["next_cursor"]
    forged = base64.urlsafe_b64encode(b'{"v":1,"secret":"SECRETVALUE"}').decode().rstrip("=")
    queries = [
        f"after_sequence=0",
        f"after_sequence=-1&process_run_id={run}",
        f"after_sequence=x&process_run_id={run}",
        f"after_sequence=99&process_run_id={run}",
        f"after_sequence=0&after_sequence=1&process_run_id={run}",
        f"cursor={cursor}&after_sequence=0",
        f"cursor={cursor}&process_run_id={run}",
        f"cursor={cursor}&item_type=issue&item_number=1",
        f"cursor={cursor}&execution_id=abc",
        f"cursor={cursor}&cursor={cursor}",
        "cursor=!!!SECRETVALUE",
        "cursor=" + "A" * 5000,
        f"cursor={forged}",
        "cursor=",
        f"cursor={cursor}&bogus=1",
        f"after_sequence=0&process_run_id={run}&item_type=issue",
    ]
    for query in queries:
        response = client.get(f"/api/logs?{query}")
        assert response.status_code == 422, query
        assert "SECRETVALUE" not in response.text and len(response.content) < 600
    assert client.get(f"/api/logs?cursor={cursor}&limit=500").status_code == 200


def test_cursor_from_other_repository_is_rejected(client, engine, monkeypatch):
    _unscoped(3)
    cursor = client.get(f"/api/logs?after_sequence=0&process_run_id={_run()}&limit=1").json()["next_cursor"]
    with patch("src.auto_coder.webhook_server.init_dashboard"), patch("src.auto_coder.webhook_server.init_dashboard_adjudication"):
        other = TestClient(create_app(engine, "other/repo"))
    assert other.get(f"/api/logs?cursor={cursor}").status_code == 422


def test_retention_gap_after_real_eviction_and_clear(client):
    TraceCollector._instance = None
    collector = TraceCollector(max_events=10)
    _unscoped(10)
    first = client.get(f"/api/logs?after_sequence=0&process_run_id={collector.process_run_id}&limit=3").json()
    assert first["has_more"]
    collector.start_execution("other/repo", "issue", 1, origin="worker")  # evicts seq 1, before consumed boundary 3
    ok = client.get(f"/api/logs?cursor={first['next_cursor']}&limit=3").json()
    assert ok["events"][0]["sequence"] == 4 and ok["events_truncated"] is True
    _unscoped(3)  # evicts through 5 > boundary 3 of first cursor
    response = client.get(f"/api/logs?cursor={first['next_cursor']}")
    body = response.json()
    assert response.status_code == 409 and body["error"]["code"] == "retention_gap"
    assert body["coverage"] == "unknown" and body["retention"]["discarded_through_sequence"] >= 4
    assert body["retention"]["oldest_retained_sequence"] > 4 and "next_cursor" not in body
    # Explicit new baseline at the high-water mark remains possible.
    high = body["retention"]["sequence_high_watermark"]
    assert client.get(f"/api/logs?after_sequence={high}&process_run_id={collector.process_run_id}").json()["has_more"] is False
    discarded = body["retention"]["discarded_through_sequence"]
    pending = client.get(f"/api/logs?after_sequence={discarded}&process_run_id={collector.process_run_id}&limit=1").json()
    collector.clear()
    assert client.get(f"/api/logs?cursor={pending['next_cursor']}").json()["error"]["code"] == "retention_gap"
    done_cursor = client.get(f"/api/logs?after_sequence={high}&process_run_id={collector.process_run_id}").json()
    assert done_cursor["next_after_sequence"] == high


def test_completed_interval_survives_later_eviction_and_new_run_conflicts(client):
    TraceCollector._instance = None
    collector = TraceCollector(max_events=5)
    _unscoped(4)
    run = collector.process_run_id
    _unscoped(20)
    done = client.get(f"/api/logs?after_sequence=24&process_run_id={run}")
    assert done.status_code == 200 and done.json()["events"] == [] and done.json()["has_more"] is False
    cursor = client.get(f"/api/logs?after_sequence=21&process_run_id={run}&limit=1").json()["next_cursor"]
    TraceCollector._instance = None
    _unscoped(2)
    stale = client.get(f"/api/logs?cursor={cursor}")
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "process_run_changed" and "events" not in stale.json()
    assert client.get(f"/api/logs?after_sequence=0&process_run_id={run}").status_code == 409


def test_byte_clipping_never_skips_and_omissions_are_explicit(client):
    collector = get_trace_collector()
    run = collector.process_run_id
    collector.start_execution(REPO, "issue", 1, origin="x" * 300)  # identity cannot be represented exactly
    _unscoped(2, "a")
    collector.record_event(EventKind.STAGE_RESULT, "bad stage!", "worker", label="invalid identity")
    collector.start_execution(REPO, "issue", 2, origin="worker", facts={"reason": "ghp_" + "q" * 30}).finish(Outcome.FAILED)
    boundary = "y" * 1990 + "ghp_" + "z" * 40
    collector.record_event(EventKind.STAGE_RESULT, "free", "worker", label=boundary, facts={"reason": "r" * 1999 + "sk-" + "k" * 30})
    _unscoped(250, "m" * 1900)
    pages, sizes = _walk(client, f"/api/logs?after_sequence=0&process_run_id={run}&limit=500")
    events = [e for p in pages for e in p["events"]]
    omitted = [o for p in pages for o in p["omissions"]]
    assert max(sizes) <= MAX_BYTES and len(pages) > 1
    assert {o["reason"] for o in omitted} == {"identity_unrepresentable"}
    assert sum(o["count"] for o in omitted) == 2 and (omitted[0]["first_sequence"], omitted[0]["last_sequence"]) == (1, 4)
    assert all(p["response"]["incomplete"] == bool(p["omissions"]) for p in pages)
    got = [e["sequence"] for e in events]
    assert got == sorted(set(got))
    retained = {e.sequence for e in collector.get_snapshot().events}
    assert set(got) == retained - {1, 4}
    raw = json.dumps(pages)
    assert "ghp_" not in raw and "sk-k" not in raw and "x" * 300 not in raw and "invalid identity" not in raw
    clipped = next(e for e in events if e["label"].startswith("y" * 100))
    assert "ghp_" not in clipped["label"] and clipped["filtered"] is True


def test_single_oversized_record_is_omitted_and_cannot_loop(client, monkeypatch):
    _unscoped(3)
    real = public_api.project_event

    def huge(event, repo):
        entry = real(event, repo)
        if entry is not None and event.sequence == 2:
            entry.facts = {"reason": "z" * (MAX_BYTES + 10)}
        return entry

    monkeypatch.setattr(public_api, "project_event", huge)
    pages, sizes = _walk(client, f"/api/logs?after_sequence=0&process_run_id={_run()}&limit=1")
    assert [e["sequence"] for p in pages for e in p["events"]] == [1, 3]
    omission = [o for p in pages for o in p["omissions"]]
    assert omission == [{"reason": "representation_exceeds_response_bound", "count": 1, "first_sequence": 2, "last_sequence": 2}]
    assert max(sizes) <= MAX_BYTES and len(pages) <= 4


def test_only_omissions_page_still_progresses(client):
    collector = get_trace_collector()
    for _ in range(3):
        collector.start_execution(REPO, "issue", 1, origin="x" * 300)
    pages, _ = _walk(client, f"/api/logs?after_sequence=0&process_run_id={_run()}&limit=1")
    assert pages[-1]["next_after_sequence"] == 3 and sum(o["count"] for p in pages for o in p["omissions"]) == 3
    assert pages[0]["result"] == "omitted_only" and pages[0]["events"] == []


def test_unavailability_and_unexpected_projection_failure_are_503_and_retry_works(client):
    _unscoped(5)
    run = _run()
    first = client.get(f"/api/logs?after_sequence=0&process_run_id={run}&limit=2").json()
    with patch.object(TraceCollector, "get_snapshot", side_effect=RuntimeError("secret ghp_abcdef /srv")):
        response = client.get(f"/api/logs?cursor={first['next_cursor']}")
    assert response.status_code == 503 and response.json()["error"]["code"] == "observation_unavailable"
    assert "next_cursor" not in response.text and "ghp_" not in response.text and "/srv" not in response.text
    with patch.object(public_api, "project_event", side_effect=RuntimeError("boom /srv")):
        response = client.get(f"/api/logs?cursor={first['next_cursor']}")
    assert response.status_code == 503 and "next_cursor" not in response.text and "boom" not in response.text
    retry = client.get(f"/api/logs?cursor={first['next_cursor']}&limit=2").json()
    assert [e["sequence"] for e in retry["events"]] == [3, 4]


def test_inconsistent_continuity_evidence_is_503(client):
    _unscoped(3)
    real = get_trace_collector().get_snapshot()
    import dataclasses

    bad = dataclasses.replace(real, discarded_through_sequence=99)
    with patch.object(TraceCollector, "get_snapshot", return_value=bad):
        assert client.get(f"/api/logs?after_sequence=0&process_run_id={_run()}").status_code == 503


def test_engine_admission_branch_reaches_incremental_http_and_reads_are_inert(client, engine):
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = []
    eng = AutomationEngine(MagicMock(), config)
    eng._process_single_candidate_unified(REPO, Candidate(type="issue", data={"number": 501, "title": "T", "body": "B", "labels": []}, priority=0), config)
    run = _run()
    body = client.get(f"/api/logs?after_sequence=0&process_run_id={run}&item_type=issue&item_number=501").json()
    gate = next(e for e in body["events"] if e["stage_id"] == "issue.author-admission")
    assert (gate["outcome"], gate["item_number"], gate["origin"]) == ("skipped", 501, "issue.author-admission")
    assert gate["execution_id"] and body["events"][-1]["kind"] == "execution-finished"
    # Existing surfaces keep their semantics, and reads cause no operational effect.
    engine.github.reset_mock()
    before = get_trace_collector().get_snapshot()
    recent = client.get("/api/logs?limit=2").json()
    assert "next_cursor" not in recent and "snapshot_upper_sequence" not in recent and recent["filter"].keys() == {"item_type", "item_number", "execution_id", "process_run_id", "limit"}
    assert client.get("/api/status").status_code == 200 and client.post("/api/logs").status_code == 405
    assert get_trace_collector().get_snapshot().events == before.events and engine.github.method_calls == []


def test_incremental_page_does_not_block_event_loop(engine, monkeypatch):
    import asyncio

    import httpx

    monkeypatch.setenv("AUTO_CODER_PUBLIC_API_ENABLED", "1")
    with patch("src.auto_coder.webhook_server.init_dashboard"), patch("src.auto_coder.webhook_server.init_dashboard_adjudication"):
        app = create_app(engine, REPO)
    _unscoped(3)
    entered, release = threading.Event(), threading.Event()
    real = TraceCollector.get_snapshot

    def blocked(self, *a, **k):
        entered.set()
        release.wait(10)
        return real(self, *a, **k)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            with patch.object(TraceCollector, "get_snapshot", blocked):
                task = asyncio.create_task(c.get(f"/api/logs?after_sequence=0&process_run_id={_run()}"))
                while not entered.is_set():
                    await asyncio.sleep(0.01)
                assert (await c.get("/api/")).status_code == 200 and not task.done()
                release.set()
                assert (await task).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("override", [{"sequence_high_watermark": 0, "oldest_retained_sequence": None, "discarded_through_sequence": 0}, {"oldest_retained_sequence": None}, {"sequence_high_watermark": 1}, {"oldest_retained_sequence": 2}])
def test_continuity_contradicting_retained_events_is_503(client, override):
    import dataclasses

    _unscoped(3)
    bad = dataclasses.replace(get_trace_collector().get_snapshot(), **override)
    with patch.object(TraceCollector, "get_snapshot", return_value=bad):
        response = client.get(f"/api/logs?after_sequence=0&process_run_id={_run()}")
    assert response.status_code == 503 and response.json()["error"]["code"] == "observation_unavailable"
    assert "next_cursor" not in response.text and "next_after_sequence" not in response.text


def test_filesystem_paths_in_free_text_are_redacted_on_both_surfaces(client):
    collector = get_trace_collector()
    reason = "Cannot open /srv/private/credentials.json and ~/.config/x/y or C:\\Users\\bob\\tok.txt; owner/repo#12 ok"
    with collector.start_execution(REPO, "pr", 41, origin="worker") as execution:
        collector.record_event(EventKind.STAGE_RESULT, "pr.review", "worker", label="read /var/lib/app/state.db", outcome=Outcome.FAILED, facts={"reason": reason, "error": "x" * 1990 + " /srv/private/secret.json"})
        execution.finish(Outcome.FAILED)
    run = collector.process_run_id
    first = client.get(f"/api/logs?after_sequence=0&process_run_id={run}&item_type=pr&item_number=41&limit=1").json()
    rest, _ = _walk(client, f"/api/logs?cursor={first['next_cursor']}")
    recent = client.get("/api/logs?item_type=pr&item_number=41").json()
    raw = json.dumps([first, *rest, recent])
    for leaked in ("/srv/private", "credentials.json", ".config/x", "Users", "bob", "/var/lib", "state.db", "secret.json"):
        assert leaked not in raw
    result = next(e for p in [first, *rest] for e in p["events"] if e["stage_id"] == "pr.review")
    assert result["outcome"] == "failed" and result["execution_id"] == execution.scope.execution_id
    assert result["facts"]["reason"].startswith("Cannot open [REDACTED_PATH] and") and "owner/repo#12 ok" in result["facts"]["reason"]
    assert result["filtered"] is True and result["label"] == "read [REDACTED_PATH]"
    stored = next(e for e in collector.get_snapshot().events if e.stage_id == "pr.review")
    assert stored.facts["reason"] == reason  # collector keeps the original
