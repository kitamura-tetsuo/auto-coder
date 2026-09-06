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
from auto_coder.issue_processor import _process_issue_claude_routine_mode, _process_issue_codex_cloud_mode, _process_issue_jules_mode
from auto_coder.pr_processor import _apply_github_actions_fix, _apply_local_test_fix, _send_codex_cloud_error_feedback, _send_jules_error_feedback


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


def test_stale_parent_authorization_joins_child_validation_during_drain(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    object.__setattr__(config, "validation_concurrency", 2)
    github = MagicMock()
    github.get_issue_dispatch_snapshot_strict.return_value = {
        "number": 1,
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github.get_direct_sub_issues_strict.return_value = [{"number": 22, "state": "open"}]
    engine = AutomationEngine(github, config)
    decomposition_started = threading.Event()
    child_started = threading.Event()
    release_decomposition = threading.Event()
    release_child = threading.Event()
    authoritative = (github.get_issue_dispatch_snapshot_strict.return_value, github.get_direct_sub_issues_strict.return_value)
    monkeypatch.setattr(engine, "_fetch_authoritative_decomposition_set", lambda *_args: authoritative)
    monkeypatch.setattr(engine, "_defer_initial_issue_stabilization", lambda *_args: False)
    monkeypatch.setattr(engine, "_get_decomposition_validator", lambda *_args: MagicMock())

    def decomposition():
        decomposition_started.set()
        assert release_decomposition.wait(5)
        return SimpleNamespace(verdict="READY")

    def individual():
        child_started.set()
        assert release_child.wait(5)
        return SimpleNamespace(verdict="READY")

    monkeypatch.setattr(
        engine,
        "_schedule_parent_validations",
        lambda *_args: (
            engine.validation_scheduler.submit("decomposition:stale-parent", decomposition),
            {22: engine.validation_scheduler.submit("individual:stale-child", individual)},
        ),
    )

    async def scenario():
        authorization = asyncio.create_task(
            engine._run_local_critical(
                "stale remote session reconciliation",
                engine._authorize_stale_jules_dispatch,
                "owner/repo",
                1,
                github.get_issue_dispatch_snapshot_strict.return_value,
            )
        )
        assert await asyncio.to_thread(decomposition_started.wait, 2)
        assert await asyncio.to_thread(child_started.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        authorization.cancel()
        release_decomposition.set()
        await asyncio.sleep(0.05)
        assert not authorization.done()
        assert list(engine._critical_operations.values()) == ["stale remote session reconciliation"]
        release_child.set()
        assert await authorization is None

    asyncio.run(scenario())


def test_parent_routing_snapshot_failure_cannot_abandon_validation_batch(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    object.__setattr__(config, "validation_concurrency", 2)
    github = MagicMock()
    parent = {"number": 1, "state": "open", "labels": [{"name": "implementation-ready"}]}
    child = {"number": 22, "state": "open"}
    github.get_direct_sub_issues_strict.return_value = [child]
    github.get_issue_dispatch_snapshot_strict.side_effect = [parent, RuntimeError("snapshot unavailable")]
    engine = AutomationEngine(github, config)
    candidate = Candidate(type="issue", data=parent.copy(), priority=0, issue_number=1)
    decomposition_started = threading.Event()
    child_started = threading.Event()
    release_decomposition = threading.Event()
    release_child = threading.Event()

    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *_args: candidate)
    monkeypatch.setattr(engine, "_validate_submitted_parent_generation_for_child", lambda *_args: None)
    monkeypatch.setattr(engine, "_is_issue_author_allowed", lambda *_args: True)
    monkeypatch.setattr(engine, "_defer_initial_issue_stabilization", lambda *_args: False)
    monkeypatch.setattr(engine, "_fetch_authoritative_decomposition_set", lambda *_args: (parent, [child]))

    def decomposition():
        decomposition_started.set()
        assert release_decomposition.wait(5)
        return SimpleNamespace(verdict="READY")

    def individual():
        child_started.set()
        assert release_child.wait(5)
        return SimpleNamespace(verdict="READY")

    monkeypatch.setattr(
        engine,
        "_schedule_parent_validations",
        lambda *_args: (
            engine.validation_scheduler.submit("decomposition:routing", decomposition),
            {22: engine.validation_scheduler.submit("individual:routing", individual)},
        ),
    )
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, current: engine._process_single_candidate_unified(repo, current, config))

    async def scenario():
        assert await engine.invalidate_entity("owner/repo", "issue", 1, "parent-routing")
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


def test_refill_inherited_validation_error_joins_running_child(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig()
    object.__setattr__(config, "validation_concurrency", 2)
    github = MagicMock()
    parent = {"number": 1, "state": "open", "labels": [{"name": "implementation-ready"}]}
    child = {"number": 22, "state": "open", "title": "Child", "body": ""}
    github.get_direct_sub_issues_strict.return_value = []
    github.get_issue_dispatch_snapshot_strict.return_value = child
    engine = AutomationEngine(github, config)
    decomposition_started = threading.Event()
    child_started = threading.Event()
    release_decomposition = threading.Event()
    release_child = threading.Event()
    monkeypatch.setattr(engine, "_is_issue_author_allowed", lambda *_args: True)
    monkeypatch.setattr(engine, "_reconcile_validation_snapshot", lambda _repo, _number, snapshot: snapshot)
    monkeypatch.setattr(engine, "_get_authoritative_parent_number", lambda *_args: 1)
    monkeypatch.setattr(engine, "_fetch_authoritative_decomposition_set", lambda *_args: (parent, [child]))
    monkeypatch.setattr(engine, "_defer_initial_issue_stabilization", lambda *_args: False)
    monkeypatch.setattr(engine, "_get_decomposition_validator", lambda *_args: MagicMock())

    def decomposition():
        decomposition_started.set()
        assert release_decomposition.wait(5)
        return SimpleNamespace(verdict="ERROR")

    def individual():
        child_started.set()
        assert release_child.wait(5)
        return SimpleNamespace(verdict="READY")

    monkeypatch.setattr(
        engine,
        "_schedule_parent_validations",
        lambda *_args: (
            engine.validation_scheduler.submit("decomposition:refill", decomposition),
            {22: engine.validation_scheduler.submit("individual:refill", individual)},
        ),
    )

    async def scenario():
        refill = asyncio.create_task(
            engine._run_local_critical(
                "capacity refill issue #22",
                engine._process_single_candidate_unified,
                "owner/repo",
                Candidate(type="issue", data=child, priority=0, issue_number=22),
                config,
                False,
                False,
                False,
                False,
                False,
                True,
                1,
            )
        )
        assert await asyncio.to_thread(decomposition_started.wait, 2)
        assert await asyncio.to_thread(child_started.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        refill.cancel()
        release_decomposition.set()
        await asyncio.sleep(0.05)
        assert not refill.done()
        assert list(engine._critical_operations.values()) == ["capacity refill issue #22"]
        release_child.set()
        result = await refill
        assert result.actions == ["Deferred - decomposition validation error"]

    asyncio.run(scenario())


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


def test_local_test_repair_rechecks_drain_after_context_acquisition(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    manager = MagicMock()

    def context_lookup(**_kwargs):
        entered.set()
        assert release.wait(5)
        return "context"

    monkeypatch.setattr("auto_coder.pr_processor.extract_important_errors", lambda *_args: "failure")
    monkeypatch.setattr("auto_coder.pr_processor.get_commit_log", context_lookup)
    monkeypatch.setattr("auto_coder.pr_processor.get_linked_issues_context", lambda *_args: "")

    async def scenario():
        repair = asyncio.create_task(
            engine._run_local_critical(
                "worker 0 pr #89",
                _apply_local_test_fix,
                "owner/repo",
                {"number": 89, "title": "Repair", "body": ""},
                AutomationConfig(),
                {"success": False, "output": "failure", "errors": "failure", "return_code": 1},
                [],
                manager,
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        repair.cancel()
        release.set()
        actions, response = await repair
        assert actions == ["Deferred local repair for PR #89: graceful shutdown is draining"]
        assert response == ""

    asyncio.run(scenario())
    manager.run_test_fix_prompt.assert_not_called()


def test_cloud_ci_feedback_rechecks_drain_after_task_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    client = MagicMock()

    def resolve(*_args):
        entered.set()
        assert release.wait(5)
        return "task-89"

    monkeypatch.setattr("auto_coder.pr_processor._resolve_codex_cloud_task_id", resolve)
    monkeypatch.setattr("auto_coder.pr_processor.resolve_existing_pr_repair_target", lambda *_args: MagicMock())
    monkeypatch.setattr("auto_coder.codex_cloud_client.CodexCloudClient", lambda **_kwargs: client)

    async def scenario():
        feedback = asyncio.create_task(
            engine._run_local_critical(
                "worker 0 pr #89",
                _send_codex_cloud_error_feedback,
                "owner/repo",
                {"number": 89},
                [{"name": "tests"}],
                AutomationConfig(),
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        feedback.cancel()
        release.set()
        result = await feedback
        assert result.retryable
        assert result.actions == ("Deferred Codex Cloud continuation for PR #89: graceful shutdown is draining",)

    asyncio.run(scenario())
    client.continue_if_paused.assert_not_called()


def test_jules_ci_feedback_rechecks_drain_after_log_acquisition(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    client = MagicMock()

    def load_logs(*_args):
        entered.set()
        assert release.wait(5)
        return "failed output"

    monkeypatch.setattr("auto_coder.pr_processor._get_github_actions_logs", load_logs)
    monkeypatch.setattr("auto_coder.jules_client.JulesClient", lambda: client)

    async def scenario():
        feedback = asyncio.create_task(
            engine._run_local_critical(
                "worker 0 pr #90",
                _send_jules_error_feedback,
                "owner/repo",
                {"number": 90, "title": "Repair", "body": "", "_jules_session_id": "session-90", "user": {}},
                [{"name": "tests"}],
                AutomationConfig(),
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        feedback.cancel()
        release.set()
        actions = await feedback
        assert actions == ["Deferred Jules CI feedback for PR #90: graceful shutdown is draining"]

    asyncio.run(scenario())
    client.send_message.assert_not_called()


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


def test_initial_cloud_launch_rechecks_drain_after_prompt_context(monkeypatch, tmp_path):
    from auto_coder.cloud_run import CloudRunRepository

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    client = MagicMock()
    client.start_task.return_value = "task-after-restart"
    client.task_urls = {}
    issue = {"number": 1778, "title": "Cloud", "body": "", "labels": []}

    def context_lookup(**_kwargs):
        entered.set()
        assert release.wait(5)
        return "context"

    monkeypatch.setattr("auto_coder.issue_processor.get_commit_log", context_lookup)
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)
    monkeypatch.setattr("auto_coder.codex_cloud_client.CodexCloudClient", lambda **_kwargs: client)
    monkeypatch.setattr("auto_coder.issue_processor.CloudManager", MagicMock())

    async def scenario():
        launch = asyncio.create_task(
            engine._run_local_critical(
                "worker 0 issue #1778",
                _process_issue_codex_cloud_mode,
                "owner/repo",
                issue,
                AutomationConfig(),
                MagicMock(),
                "codex-cloud-luna",
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        launch.cancel()
        release.set()
        actions = await launch
        assert actions == ["Deferred Codex Cloud task for issue #1778: graceful shutdown is draining"]

    asyncio.run(scenario())
    client.start_task.assert_not_called()
    assert CloudRunRepository("owner/repo").get(1778, 0) is None

    # Restart has a fresh RUNNING admission context and no false durable run,
    # so the same production dispatcher can launch the still-eligible Issue.
    actions = _process_issue_codex_cloud_mode("owner/repo", issue, AutomationConfig(), MagicMock(), backend_name="codex-cloud-luna")
    assert actions == ["Started Codex Cloud task 'task-after-restart' for issue #1778"]
    client.start_task.assert_called_once()


@pytest.mark.parametrize("provider", ["jules", "claude-routine"])
def test_initial_session_launch_rechecks_drain_after_prompt_context(monkeypatch, tmp_path, provider):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    client = MagicMock()
    cloud_manager = MagicMock()
    cloud_manager.add_session.return_value = True
    issue = {"number": 1779, "title": "Remote", "body": "", "labels": [], "user": {}}

    def context_lookup(**_kwargs):
        entered.set()
        assert release.wait(5)
        return "context"

    monkeypatch.setattr("auto_coder.issue_processor.get_commit_log", context_lookup)
    monkeypatch.setattr("auto_coder.issue_processor.CloudManager", lambda *_args: cloud_manager)
    if provider == "jules":
        client.start_session.return_value = "jules-session"
        monkeypatch.setattr("auto_coder.issue_processor.JulesClient", lambda: client)
        operation = _process_issue_jules_mode
        operation_args = ("owner/repo", issue, AutomationConfig(), MagicMock())
        expected = ["Deferred Jules session for issue #1779: graceful shutdown is draining"]
    else:
        client.fire_routine.return_value = ("claude-session", "https://example.test/session")
        monkeypatch.setattr("auto_coder.claude_routine_client.ClaudeRoutineClient", lambda **_kwargs: client)
        operation = _process_issue_claude_routine_mode
        operation_args = ("owner/repo", issue, AutomationConfig(), MagicMock(), "claude-routine")
        expected = ["Deferred Claude Routine session for issue #1779: graceful shutdown is draining"]

    async def scenario():
        launch = asyncio.create_task(engine._run_local_critical(f"worker 0 issue #1779 {provider}", operation, *operation_args))
        assert await asyncio.to_thread(entered.wait, 2)
        engine.request_graceful_shutdown("SIGTERM")
        launch.cancel()
        release.set()
        assert await launch == expected

    asyncio.run(scenario())
    client.start_session.assert_not_called()
    client.fire_routine.assert_not_called()
    cloud_manager.add_session.assert_not_called()

    # With no durable session recorded, a fresh RUNNING context can still
    # dispatch the same authorized Issue after restart.
    restarted_actions = operation(*operation_args)
    assert restarted_actions != expected
    cloud_manager.add_session.assert_called_once()


def test_container_entrypoint_and_compose_grace_preserve_sigterm_delivery():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["auto-coder"]' in dockerfile

    compose = yaml.safe_load(Path("compose.channels.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"release", "beta"}
    for service in compose["services"].values():
        assert service["stop_grace_period"] == "30m"
        assert service["command"][0] == "process-issues"
