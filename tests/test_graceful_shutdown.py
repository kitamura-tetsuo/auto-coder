import asyncio
import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine, EngineLifecycle
from auto_coder.entity_invalidation import EntityIdentity
from auto_coder.issue_processor import _process_issue_codex_cloud_mode


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
