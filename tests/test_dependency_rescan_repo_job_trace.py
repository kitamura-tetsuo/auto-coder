"""Issue #2001: production dependency-rescan intake/execution/handoff evidence.

These tests drive the real production entrypoints -- the `/hooks/github`
route (`create_app`/`process_github_payload`), the real
`DurableInvalidationQueue`, and the real `AutomationEngine._worker_loop` --
rather than calling `repo_job_trace.py` recorder methods directly. The
diagnostic evidence is read back from the real `RepoJobTraceCollector`
singleton, exactly as a future dashboard consumer would.
"""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.entity_invalidation import EntityIdentity
from src.auto_coder.execution_trace import TraceCollector
from src.auto_coder.repo_job_trace import (
    RepoJobKind,
    RepoJobTarget,
    RepoJobTraceCollector,
    executions_for_target,
    get_repo_job_trace_collector,
    observations_for_execution,
)
from src.auto_coder.util.gh_cache import OpenGitHubEntities, OpenGitHubIssue
from src.auto_coder.webhook_server import create_app

REPO = "owner/repo"
TARGET = RepoJobTarget(REPO, RepoJobKind.DEPENDENCY_RESCAN.value)


@pytest.fixture(autouse=True)
def reset_collectors():
    RepoJobTraceCollector._instance = None
    TraceCollector._instance = None
    yield
    RepoJobTraceCollector._instance = None
    TraceCollector._instance = None


def _candidate(repo_name, entity_type, number, propagate_errors=False):
    return Candidate(type=entity_type, data={"number": number, "state": "open"}, priority=0)


async def _run_worker_until_drained(engine, repo_name=REPO, iterations=500):
    worker = asyncio.create_task(engine._worker_loop(repo_name, 0))
    for _ in range(iterations):
        if engine.queue.qsize() == 0 and engine.invalidations.pending_count(repo_name) == 0 and engine.active_workers.get(0) is None:
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


def _stub_issue_processing(engine, monkeypatch, processed):
    monkeypatch.setattr(engine, "_create_candidate_from_single", _candidate)
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="issue", number=candidate.data["number"], success=True),
    )
    monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
        "number": number,
        "state": "open",
        "labels": [],
        "title": f"issue {number}",
        "body": "",
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2020-01-01T00:00:00Z",
    }
    engine.github.get_issue_details.side_effect = lambda issue: dict(issue)


def _post_dependency_webhook(engine, delivery_id="close-1", number=101):
    payload = {
        "action": "closed",
        "issue": {"number": number, "title": "closer"},
        "repository": {"full_name": REPO},
    }
    with patch("src.auto_coder.webhook_server.init_dashboard"), patch("src.auto_coder.webhook_server.init_dashboard_adjudication"):
        app = create_app(engine, REPO)
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json=payload,
            headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": delivery_id},
        )
    return response


class TestAS001IntakeRunningScanAndHandoffs:
    def test_webhook_intake_scan_and_handoffs_are_recorded(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        github = MagicMock()
        github.get_open_entities_strict.return_value = OpenGitHubEntities(
            issues=[OpenGitHubIssue(number=201), OpenGitHubIssue(number=202), OpenGitHubIssue(number=203)],
            pull_requests=[999],
        )
        engine = AutomationEngine(github, AutomationConfig())
        processed = []
        _stub_issue_processing(engine, monkeypatch, processed)

        response = _post_dependency_webhook(engine, delivery_id="close-1", number=101)
        assert response.status_code == 200

        asyncio.run(_run_worker_until_drained(engine))

        assert sorted(processed) == [101, 201, 202, 203]
        assert engine.invalidations.pending_count(REPO) == 0

        collector = get_repo_job_trace_collector()
        snapshot = collector.get_snapshot(TARGET)

        intakes = [o for o in snapshot.observations if o.kind == "intake"]
        assert len(intakes) == 1
        assert intakes[0].facts.trigger_event == "issues"
        assert intakes[0].facts.trigger_action == "closed"
        assert intakes[0].facts.trigger_delivery_refs.values == ("close-1",)
        assert 101 in intakes[0].facts.source_issue_refs.numbers
        assert intakes[0].facts.queue_phase == "new_pending"

        executions = executions_for_target(snapshot, TARGET)
        assert len(executions) == 1
        assert executions[0].finished is True
        assert executions[0].outcome == "completed"

        events = observations_for_execution(snapshot, executions[0].execution_id)
        stage_ids = [o.stage_id for o in events]
        assert "dependency-rescan.enumeration-started" in stage_ids
        assert "dependency-rescan.enumeration-completed" in stage_ids
        enumeration_completed = next(o for o in events if o.stage_id == "dependency-rescan.enumeration-completed")
        assert enumeration_completed.facts.discovered_issue_count == 3

        handoffs = [o for o in events if o.stage_id == "dependency-rescan.handoff"]
        assert len(handoffs) == 3
        handed_off_numbers = sorted(h.facts.target_issue_refs.numbers[0] for h in handoffs)
        assert handed_off_numbers == [201, 202, 203]
        assert all(h.outcome == "completed" for h in handoffs)
        # The PR the strict enumerator also returns must never become a handoff target.
        assert all(999 not in h.facts.target_issue_refs.numbers for h in handoffs)

        completed = next(o for o in events if o.stage_id == "dependency-rescan.completed")
        assert completed.outcome == "completed"
        assert completed.facts.discovered_issue_count == 3
        assert completed.facts.attempted_handoff_count == 3
        assert completed.facts.confirmed_handoff_count == 3
        assert completed.facts.failed_or_unconfirmed_handoff_count == 0
        assert completed.facts.new_pending_handoff_count == 3

        ack = next(o for o in events if o.stage_id == "dependency-rescan.claim-acknowledged")
        assert ack.outcome == "completed"

        # Joined producer-to-mounted-page oracle: the page must expose the
        # values above from the shared collector rather than reconstructing
        # them from queue rows or downstream Issue results.
        pages = {}

        def capture_page(path):
            def decorator(callback):
                pages[path] = callback
                return callback

            return decorator

        with patch("src.auto_coder.dashboard.ui") as dashboard_ui:
            dashboard_ui.page.side_effect = capture_page
            from src.auto_coder.dashboard import init_dashboard

            init_dashboard(FastAPI(), engine, REPO)
            pages["/jobs/dependency-rescan"]()

        rendered = [str(call.args[0]) for call in dashboard_ui.label.call_args_list]
        assert any("dependency-rescan.enumeration-completed" in value for value in rendered)
        assert any("Discovered Issues: 3" in value for value in rendered)
        assert any("Confirmed handoffs: 3" in value for value in rendered)
        assert not any("999" in value for value in rendered)

    def test_scan_is_visible_as_a_running_job_while_it_scans(self, tmp_path: Path, monkeypatch):
        """REQ-001: `active_workers` keeps showing the dependency job busy through the whole scan."""
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        github = MagicMock()
        enumeration_started = asyncio.Event()
        allow_enumeration = asyncio.Event()
        loop_holder = {}

        def blocking_enumerate(_repo):
            loop = loop_holder["loop"]
            loop.call_soon_threadsafe(enumeration_started.set)
            fut = asyncio.run_coroutine_threadsafe(allow_enumeration.wait(), loop)
            fut.result(timeout=5)
            return OpenGitHubEntities(issues=[OpenGitHubIssue(number=301)], pull_requests=[])

        github.get_open_entities_strict.side_effect = blocking_enumerate
        engine = AutomationEngine(github, AutomationConfig())
        processed = []
        _stub_issue_processing(engine, monkeypatch, processed)

        async def scenario():
            loop_holder["loop"] = asyncio.get_running_loop()
            response = _post_dependency_webhook(engine, delivery_id="close-2", number=102)
            assert response.status_code == 200

            worker = asyncio.create_task(engine._worker_loop(REPO, 0))
            await asyncio.wait_for(enumeration_started.wait(), timeout=5)

            status = engine.get_status()
            active = status["active_workers"][0]
            assert active is not None
            assert active["type"] == "dependency"

            allow_enumeration.set()
            for _ in range(500):
                if engine.queue.qsize() == 0 and engine.invalidations.pending_count(REPO) == 0 and engine.active_workers.get(0) is None:
                    break
                await asyncio.sleep(0.01)
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        assert sorted(processed) == [102, 301]


class TestAS002TransitionDispositionsAreDistinct:
    def test_new_pending_coalesced_and_followup_dispositions(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        github = MagicMock()
        github.get_open_entities_strict.return_value = OpenGitHubEntities(
            issues=[OpenGitHubIssue(number=401), OpenGitHubIssue(number=402), OpenGitHubIssue(number=403)],
            pull_requests=[],
        )
        engine = AutomationEngine(github, AutomationConfig())

        # 401: no prior row -> new_pending.
        # 403: already claimed and being processed -> followup_required. Claim
        # it before 402 exists so `claim()` cannot pick 402 up instead.
        engine.invalidations.invalidate(EntityIdentity(REPO, "issue", 403))
        claim_403 = engine.invalidations.claim(REPO)
        assert claim_403 is not None and claim_403.identity.number == 403
        assert engine.invalidations.begin_processing(claim_403)
        # 402: already dirty (queued webhook work) -> coalesced.
        engine.invalidations.invalidate(EntityIdentity(REPO, "issue", 402))

        asyncio.run(engine._expand_dependency_obligation(REPO))

        snapshot = get_repo_job_trace_collector().get_snapshot(TARGET)
        executions = executions_for_target(snapshot, TARGET)
        assert len(executions) == 1
        events = observations_for_execution(snapshot, executions[0].execution_id)
        handoffs = {h.facts.target_issue_refs.numbers[0]: h.facts.handoff_disposition for h in events if h.stage_id == "dependency-rescan.handoff"}
        assert handoffs == {401: "new_pending", 402: "coalesced", 403: "followup_required"}

        completed = next(o for o in events if o.stage_id == "dependency-rescan.completed")
        assert completed.facts.attempted_handoff_count == 3
        assert completed.facts.confirmed_handoff_count == 3
        assert completed.facts.failed_or_unconfirmed_handoff_count == 0
        assert completed.facts.new_pending_handoff_count == 1
        assert completed.facts.coalesced_handoff_count == 1
        assert completed.facts.followup_required_handoff_count == 1

        # Issue #403's own in-flight claim is untouched by the scan's handoff:
        # completing it still reports the newer generation the scan produced.
        assert engine.invalidations.complete(claim_403) is True


class TestAS004FailurePartwayThroughFanout:
    def test_enumeration_failure_records_no_completed_scan(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        github = MagicMock()
        github.get_open_entities_strict.side_effect = RuntimeError("second page unavailable")
        engine = AutomationEngine(github, AutomationConfig())

        with pytest.raises(RuntimeError, match="second page unavailable"):
            asyncio.run(engine._expand_dependency_obligation(REPO))

        snapshot = get_repo_job_trace_collector().get_snapshot(TARGET)
        executions = executions_for_target(snapshot, TARGET)
        assert len(executions) == 1
        assert executions[0].finished is True
        assert executions[0].outcome == "failed"
        events = observations_for_execution(snapshot, executions[0].execution_id)
        stage_ids = [o.stage_id for o in events]
        assert "dependency-rescan.enumeration-failed" in stage_ids
        assert "dependency-rescan.enumeration-completed" not in stage_ids
        assert "dependency-rescan.completed" not in stage_ids

    def test_handoff_persistence_failure_stops_fanout_with_accurate_totals(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        github = MagicMock()
        github.get_open_entities_strict.return_value = OpenGitHubEntities(
            issues=[OpenGitHubIssue(number=501), OpenGitHubIssue(number=502), OpenGitHubIssue(number=503)],
            pull_requests=[],
        )
        engine = AutomationEngine(github, AutomationConfig())

        original = engine.invalidations.invalidate_with_transition
        calls = {"count": 0}

        def flaky(identity, *args, **kwargs):
            calls["count"] += 1
            if identity.entity_type == "issue" and identity.number == 502:
                raise sqlite3.OperationalError("database is locked")
            return original(identity, *args, **kwargs)

        monkeypatch.setattr(engine.invalidations, "invalidate_with_transition", flaky)

        with pytest.raises(sqlite3.OperationalError):
            asyncio.run(engine._expand_dependency_obligation(REPO))

        snapshot = get_repo_job_trace_collector().get_snapshot(TARGET)
        executions = executions_for_target(snapshot, TARGET)
        assert len(executions) == 1
        assert executions[0].outcome == "failed"
        events = observations_for_execution(snapshot, executions[0].execution_id)
        handoffs = [o for o in events if o.stage_id == "dependency-rescan.handoff"]
        assert len(handoffs) == 2
        outcomes = {h.facts.target_issue_refs.numbers[0]: h.outcome for h in handoffs}
        assert outcomes == {501: "completed", 502: "failed"}
        assert 503 not in outcomes

        completed = next(o for o in events if o.stage_id == "dependency-rescan.completed")
        assert completed.outcome == "failed"
        assert completed.facts.discovered_issue_count == 3
        assert completed.facts.attempted_handoff_count == 2
        assert completed.facts.confirmed_handoff_count == 1
        assert completed.facts.failed_or_unconfirmed_handoff_count == 1
        # This attempt's claim must never be reported acknowledged: the
        # caller (the real worker loop) owns that, and this attempt failed
        # before reaching it.
        assert not any(o.stage_id == "dependency-rescan.claim-acknowledged" for o in events)


class TestAS005RecoveryIsNotAFabricatedTrigger:
    def test_recovered_dependency_work_is_recorded_as_recovery(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        github = MagicMock()
        github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[], pull_requests=[])
        engine = AutomationEngine(github, AutomationConfig())

        # Simulate an interrupted prior process: the dependency obligation was
        # claimed but never completed before the process stopped.
        engine.invalidations.invalidate(EntityIdentity(REPO, "dependency", 1))
        claim = engine.invalidations.claim(REPO)
        assert claim is not None

        monkeypatch.setattr(engine, "_producer_loop", lambda repo: asyncio.Event().wait())
        monkeypatch.setattr(engine, "_get_implementation_slots", lambda repo: MagicMock())
        monkeypatch.setattr("src.auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
        monkeypatch.setattr("src.auto_coder.automation_engine.get_health_monitor", MagicMock())

        async def scenario():
            task = asyncio.create_task(engine.start_automation(REPO, concurrency=1))
            for _ in range(200):
                if engine.startup_reconciled:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())

        snapshot = get_repo_job_trace_collector().get_snapshot(TARGET)
        recovered = [o for o in snapshot.observations if o.kind == "recovered"]
        assert len(recovered) == 1
        assert recovered[0].execution_id is None  # recovery never manufactures an execution
        assert recovered[0].facts.trigger_event is None  # no invented original trigger
        assert recovered[0].facts.trigger_delivery_refs.values == ()


class TestAS006NoInventedAcceptanceOrScan:
    def test_rejected_webhook_creates_no_dependency_job_record(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        engine = AutomationEngine(MagicMock(), AutomationConfig())
        payload = {"action": "closed", "issue": {"number": 601}, "repository": {"full_name": "someone-else/other-repo"}}
        with patch("src.auto_coder.webhook_server.init_dashboard"), patch("src.auto_coder.webhook_server.init_dashboard_adjudication"):
            app = create_app(engine, REPO)
        with TestClient(app) as client:
            response = client.post("/hooks/github", json=payload, headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "foreign-1"})
        assert response.status_code == 403

        snapshot = get_repo_job_trace_collector().get_snapshot(TARGET)
        assert snapshot.observations == []

    def test_dependency_intake_persistence_failure_is_recorded_without_a_scan(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        engine = AutomationEngine(MagicMock(), AutomationConfig())
        original = engine.invalidations.invalidate_with_transition

        def flaky(identity, *args, **kwargs):
            if identity.entity_type == "dependency":
                raise RuntimeError("durable intake persistence failed")
            return original(identity, *args, **kwargs)

        monkeypatch.setattr(engine.invalidations, "invalidate_with_transition", flaky)

        payload = {"action": "closed", "issue": {"number": 701}, "repository": {"full_name": REPO}}
        with patch("src.auto_coder.webhook_server.init_dashboard"), patch("src.auto_coder.webhook_server.init_dashboard_adjudication"):
            app = create_app(engine, REPO)
        with TestClient(app) as client:
            with pytest.raises(RuntimeError, match="durable intake persistence failed"):
                client.post("/hooks/github", json=payload, headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "fail-1"})

        snapshot = get_repo_job_trace_collector().get_snapshot(TARGET)
        assert executions_for_target(snapshot, TARGET) == []
        intakes = [o for o in snapshot.observations if o.kind == "intake"]
        assert len(intakes) == 1
        assert intakes[0].facts.failure_reason == "persistence_failed"


class TestAS007RecorderFailureCannotChangeTheJob:
    def test_diagnostic_recorder_failure_does_not_change_real_outcomes(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
        github = MagicMock()
        github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(number=801), OpenGitHubIssue(number=802)], pull_requests=[])
        engine = AutomationEngine(github, AutomationConfig())
        processed = []
        _stub_issue_processing(engine, monkeypatch, processed)

        broken = MagicMock()
        broken.get_snapshot.side_effect = RuntimeError("diagnostics unavailable")
        broken.start_execution.side_effect = RuntimeError("diagnostics unavailable")
        broken.record_intake.side_effect = RuntimeError("diagnostics unavailable")
        broken.record_recovered.side_effect = RuntimeError("diagnostics unavailable")
        broken.record_stage_reached.side_effect = RuntimeError("diagnostics unavailable")
        monkeypatch.setattr("src.auto_coder.automation_engine.get_repo_job_trace_collector", lambda: broken)

        response = _post_dependency_webhook(engine, delivery_id="close-3", number=103)
        assert response.status_code == 200

        asyncio.run(_run_worker_until_drained(engine))

        assert sorted(processed) == [103, 801, 802]
        assert engine.invalidations.pending_count(REPO) == 0
