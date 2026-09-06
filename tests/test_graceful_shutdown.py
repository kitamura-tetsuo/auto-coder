import asyncio
import os
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine, EngineLifecycle
from auto_coder.entity_invalidation import EntityIdentity
from auto_coder.issue_processor import _process_issue_codex_cloud_mode
from auto_coder.pr_processor import _apply_github_actions_fix


def test_worker_drain_owns_real_to_thread_operation_until_durable_completion(monkeypatch, tmp_path):
    """Exercise invalidation -> production worker -> actual executor thread."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    commits = 0
    dispatches = 0

    authoritative = Candidate(type="issue", data={"number": 1777, "state": "open"}, priority=0, issue_number=1777)
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args: authoritative)

    def blocking_processing(_repo, candidate):
        nonlocal commits, dispatches
        entered.set()
        assert release.wait(5)
        commits += 1
        # This represents the production post-validation drain checkpoint.
        if not engine.is_draining:
            dispatches += 1
        return CandidateProcessingResult(type=candidate.type, number=1777, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate", blocking_processing)

    async def scenario():
        assert await engine.invalidate_entity("owner/repo", "issue", 1777, "delivery-1")
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        assert await asyncio.to_thread(entered.wait, 2)

        assert engine.request_graceful_shutdown("SIGTERM")
        worker.cancel()  # The orchestration cancellation must not release the claim.
        await asyncio.sleep(0.05)
        assert not worker.done()
        assert engine.invalidations.claim("owner/repo") is None
        assert engine.active_workers[0].data["number"] == 1777

        # Webhook work remains durable and is not admitted during the drain.
        assert await engine.invalidate_entity("owner/repo", "issue", 1778, "delivery-2")
        assert engine.queue.empty()
        release.set()
        await worker

    asyncio.run(scenario())

    assert commits == 1
    assert dispatches == 0
    assert engine.active_workers[0] is None
    assert engine.invalidations.pending_count("owner/repo") == 1
    assert engine.invalidations.claim("owner/repo").identity == EntityIdentity("owner/repo", "issue", 1778)


def test_child_parent_validation_remains_owned_through_worker_drain(monkeypatch, tmp_path):
    """Exercise the production invalidation hook before candidate dispatch."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    validations = 0
    dispatches = 0
    candidate = Candidate(type="issue", data={"number": 22, "state": "open"}, priority=0, issue_number=22)
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args: candidate)

    def validate_parent(*_args):
        nonlocal validations
        entered.set()
        assert release.wait(5)
        validations += 1

    def dispatch(*_args):
        nonlocal dispatches
        dispatches += 1
        return CandidateProcessingResult(type="issue", number=22, success=True)

    monkeypatch.setattr(engine, "_validate_submitted_parent_generation_for_child", validate_parent)
    monkeypatch.setattr(engine, "_process_single_candidate", dispatch)

    async def scenario():
        assert await engine.invalidate_entity("owner/repo", "issue", 22, "child-delivery")
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        worker.cancel()
        await asyncio.sleep(0.05)
        assert not worker.done()
        assert list(engine._critical_operations.values()) == ["worker 0 submitted-parent validation for issue #22"]
        assert engine.invalidations.claim("owner/repo") is None
        release.set()
        await worker

    asyncio.run(scenario())
    assert validations == 1
    assert dispatches == 0
    assert engine.invalidations.pending_count("owner/repo") == 1


def test_parent_validation_error_joins_running_sibling_before_releasing_claim(monkeypatch, tmp_path):
    """An early batch ERROR cannot outlive production worker ownership."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    object.__setattr__(config, "validation_concurrency", 2)
    engine = AutomationEngine(MagicMock(), config)
    decomposition_started = threading.Event()
    child_started = threading.Event()
    release_decomposition = threading.Event()
    release_child = threading.Event()
    candidate = Candidate(type="issue", data={"number": 22, "state": "open"}, priority=0, issue_number=22)
    parent = {"number": 1, "state": "open", "labels": [{"name": "implementation-ready"}]}
    child = {"number": 22, "state": "open"}
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args: candidate)
    monkeypatch.setattr(engine, "_get_authoritative_parent_number", lambda *_args: 1)
    monkeypatch.setattr(engine, "_reconcile_parent_issue", lambda _repo, _number, snapshot: snapshot)
    monkeypatch.setattr(engine, "_fetch_authoritative_decomposition_set", lambda *_args: (parent, [child]))
    monkeypatch.setattr(engine, "_defer_initial_issue_stabilization", lambda *_args: False)

    def decomposition():
        decomposition_started.set()
        assert release_decomposition.wait(5)
        return SimpleNamespace(verdict="ERROR")

    def individual():
        child_started.set()
        assert release_child.wait(5)
        return SimpleNamespace(verdict="READY")

    def schedule(*_args):
        return (
            engine.validation_scheduler.submit("decomposition:test", decomposition),
            {22: engine.validation_scheduler.submit("individual:test", individual)},
        )

    monkeypatch.setattr(engine, "_schedule_parent_validations", schedule)

    async def scenario():
        assert await engine.invalidate_entity("owner/repo", "issue", 22, "batch-delivery")
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        assert await asyncio.to_thread(decomposition_started.wait, 2)
        assert await asyncio.to_thread(child_started.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        worker.cancel()
        release_decomposition.set()
        await asyncio.sleep(0.05)
        assert not worker.done()
        assert engine.invalidations.claim("owner/repo") is None
        assert engine._critical_operations
        release_child.set()
        await worker

    asyncio.run(scenario())
    assert engine.invalidations.pending_count("owner/repo") == 1


def test_queued_child_validation_is_not_started_during_drain(monkeypatch, tmp_path):
    """The production worker leaves executor-backlogged validation for restart."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    object.__setattr__(config, "validation_concurrency", 1)
    engine = AutomationEngine(MagicMock(), config)
    decomposition_started = threading.Event()
    release_decomposition = threading.Event()
    child_calls = 0
    candidate = Candidate(type="issue", data={"number": 22, "state": "open"}, priority=0, issue_number=22)
    parent = {"number": 1, "state": "open", "labels": [{"name": "implementation-ready"}]}
    child = {"number": 22, "state": "open"}
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args: candidate)
    monkeypatch.setattr(engine, "_get_authoritative_parent_number", lambda *_args: 1)
    monkeypatch.setattr(engine, "_reconcile_parent_issue", lambda _repo, _number, snapshot: snapshot)
    monkeypatch.setattr(engine, "_fetch_authoritative_decomposition_set", lambda *_args: (parent, [child]))
    monkeypatch.setattr(engine, "_defer_initial_issue_stabilization", lambda *_args: False)

    def decomposition():
        decomposition_started.set()
        assert release_decomposition.wait(5)
        return SimpleNamespace(verdict="READY")

    def individual():
        nonlocal child_calls
        child_calls += 1
        return SimpleNamespace(verdict="READY")

    def schedule(*_args):
        return (
            engine.validation_scheduler.submit("decomposition:queued", decomposition),
            {22: engine.validation_scheduler.submit("individual:queued", individual)},
        )

    monkeypatch.setattr(engine, "_schedule_parent_validations", schedule)

    async def scenario():
        assert await engine.invalidate_entity("owner/repo", "issue", 22, "queued-delivery")
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        assert await asyncio.to_thread(decomposition_started.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        worker.cancel()
        release_decomposition.set()
        await worker

    asyncio.run(scenario())
    assert child_calls == 0
    assert engine.invalidations.pending_count("owner/repo") == 1


def test_producer_returns_at_repository_update_drain_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    updates = 0

    monkeypatch.setattr(engine, "_check_and_handle_closed_branch", lambda *_: True)
    monkeypatch.setattr("auto_coder.automation_engine.check_for_updates_and_restart", lambda: None)
    monkeypatch.setattr(engine, "_claim_jules_session_list_refresh", lambda: False)

    def blocking_pull():
        nonlocal updates
        updates += 1
        entered.set()
        assert release.wait(5)
        return MagicMock(success=True)

    monkeypatch.setattr("auto_coder.automation_engine.git_pull", blocking_pull)
    monkeypatch.setattr(engine, "_sleep_or_wake", MagicMock(side_effect=AssertionError("maintenance sleep started while draining")))

    async def scenario():
        producer = asyncio.create_task(engine._producer_loop("owner/repo"))
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        producer.cancel()
        release.set()
        await producer

    asyncio.run(scenario())
    assert updates == 1


def test_initial_pr_repair_rechecks_drain_after_context_acquisition(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    llm_calls = 0

    def context_lookup(**_kwargs):
        entered.set()
        assert release.wait(5)
        return "context"

    def run_llm(*_args, **_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "fixed"

    monkeypatch.setattr("auto_coder.pr_processor.get_commit_log", context_lookup)
    monkeypatch.setattr("auto_coder.pr_processor.get_linked_issues_context", lambda *_args: "")
    monkeypatch.setattr("auto_coder.pr_processor.run_llm_prompt", run_llm)

    async def scenario():
        repair = asyncio.create_task(
            engine._run_local_critical(
                "worker 0 pr #88",
                _apply_github_actions_fix,
                "owner/repo",
                {"number": 88, "title": "Repair", "body": ""},
                AutomationConfig(),
                "failed",
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        repair.cancel()
        release.set()
        actions = await repair
        assert actions == ["Deferred GitHub Actions repair for PR #88: graceful shutdown is draining"]

    asyncio.run(scenario())
    assert llm_calls == 0


def test_recurrent_provider_scan_is_owned_and_cannot_launch_during_drain(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    prompt = tmp_path / "recurrent.md"
    prompt.write_text("---\ntags: [jules, recurrent]\nname: [maintenance]\n---\nMaintain it.\n", encoding="utf-8")
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    client = MagicMock()

    def list_sessions(**_kwargs):
        entered.set()
        assert release.wait(5)
        return []

    client.list_sessions.side_effect = list_sessions
    monkeypatch.setattr("auto_coder.jules_engine.os.path.isdir", lambda *_: True)
    monkeypatch.setattr("auto_coder.jules_engine.glob.glob", lambda *_: [str(prompt)])
    monkeypatch.setattr("auto_coder.jules_engine.JulesClient", lambda: client)

    async def scenario():
        scanner = asyncio.create_task(engine.check_and_start_recurrent_jules_tasks_async("owner/repo"))
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        scanner.cancel()
        await asyncio.sleep(0.05)
        assert not scanner.done()
        assert list(engine._critical_operations.values()) == ["recurrent provider scan"]
        release.set()
        await scanner

    asyncio.run(scenario())
    client.start_session.assert_not_called()


def test_sigint_and_sigterm_share_drain_transition_and_second_sigint_forces(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())

    assert engine.request_graceful_shutdown(signal.Signals(signal.SIGTERM).name)
    assert engine.lifecycle is EngineLifecycle.DRAINING
    assert not engine.request_graceful_shutdown(signal.Signals(signal.SIGINT).name)
    engine.request_force_stop("second SIGINT")
    assert engine.lifecycle is EngineLifecycle.FORCED


def test_process_issues_disables_real_uvicorn_signal_capture(monkeypatch):
    import uvicorn

    from auto_coder.cli_commands_main import _disable_uvicorn_signal_capture

    async def app(_scope, _receive, _send):
        return None

    server = uvicorn.Server(uvicorn.Config(app, port=0))
    _disable_uvicorn_signal_capture(server)
    captured = []
    monkeypatch.setattr(signal, "signal", lambda *args: captured.append(args))

    with server.capture_signals():
        pass

    assert captured == []


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
def test_production_webhook_orchestration_drains_one_real_os_signal(monkeypatch, tmp_path, shutdown_signal):
    from fastapi import FastAPI

    from auto_coder.cli_commands_main import _run_process_issues_daemon

    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    graceful_requests = []
    original_request = engine.request_graceful_shutdown

    def blocking_operation():
        entered.set()
        assert release.wait(5)

    async def start_automation(_repo):
        await engine._run_local_critical("signal regression validation", blocking_operation)

    def request(reason):
        graceful_requests.append(reason)
        return original_request(reason)

    monkeypatch.setattr(engine, "start_automation", start_automation)
    monkeypatch.setattr(engine, "request_graceful_shutdown", request)
    monkeypatch.setattr(engine, "request_force_stop", MagicMock(side_effect=AssertionError("one signal forced shutdown")))
    monkeypatch.setattr("auto_coder.webhook_server.create_app", lambda *_args: FastAPI())

    async def scenario():
        daemon = asyncio.create_task(_run_process_issues_daemon(engine, "owner/repo", True, "127.0.0.1", 0, "critical", None, None))
        assert await asyncio.to_thread(entered.wait, 2)
        os.kill(os.getpid(), shutdown_signal)
        await asyncio.sleep(0.1)
        assert not daemon.done()
        assert list(engine._critical_operations.values()) == ["signal regression validation"]
        assert graceful_requests == [signal.Signals(shutdown_signal).name]
        release.set()
        await daemon

    asyncio.run(scenario())
    engine.request_force_stop.assert_not_called()


def test_remote_cloud_ownership_survives_graceful_stop_and_restart(monkeypatch, tmp_path):
    """Production cloud dispatch remains durable without joining remote work."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    issue = {"number": 1777, "title": "Remote work", "body": "Implement it", "labels": []}

    with (
        patch("auto_coder.issue_processor.CloudManager"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient") as client_type,
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
        patch("auto_coder.issue_processor.get_current_attempt", return_value=0),
    ):
        client_type.return_value.start_task.return_value = "remote-task"
        client_type.return_value.task_urls = {}
        first = _process_issue_codex_cloud_mode("owner/repo", issue, AutomationConfig(), MagicMock(), backend_name="codex-cloud-luna")
        assert first == ["Started Codex Cloud task 'remote-task' for issue #1777"]

        engine = AutomationEngine(MagicMock(), AutomationConfig())
        engine.request_graceful_shutdown("SIGTERM")
        asyncio.run(engine.start_automation("owner/repo", concurrency=0))
        assert engine.lifecycle is EngineLifecycle.STOPPED

        # A fresh process reaches the same production dispatcher. The durable
        # CloudRun, not in-memory engine state, suppresses duplicate launch.
        restarted_client = MagicMock()
        client_type.return_value = restarted_client
        second = _process_issue_codex_cloud_mode("owner/repo", issue, AutomationConfig(), MagicMock(), backend_name="codex-cloud-luna")
        restarted_client.start_task.assert_not_called()
        assert second == ["Codex Cloud task 'remote-task' already running for issue #1777 attempt 0; skipped duplicate dispatch"]


def test_container_entrypoint_and_compose_grace_preserve_sigterm_delivery():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["auto-coder"]' in dockerfile

    compose = yaml.safe_load(Path("compose.channels.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"release", "beta"}
    for service in compose["services"].values():
        assert service["stop_grace_period"] == "30m"
        assert service["command"][0] == "process-issues"
