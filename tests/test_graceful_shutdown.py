import asyncio
import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine, EngineLifecycle
from auto_coder.entity_invalidation import EntityIdentity


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


def test_sigint_and_sigterm_share_drain_transition_and_second_sigint_forces(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())

    assert engine.request_graceful_shutdown(signal.Signals(signal.SIGTERM).name)
    assert engine.lifecycle is EngineLifecycle.DRAINING
    assert not engine.request_graceful_shutdown(signal.Signals(signal.SIGINT).name)
    engine.request_force_stop("second SIGINT")
    assert engine.lifecycle is EngineLifecycle.FORCED


def test_container_entrypoint_and_compose_grace_preserve_sigterm_delivery():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["auto-coder"]' in dockerfile

    compose = yaml.safe_load(Path("compose.channels.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"release", "beta"}
    for service in compose["services"].values():
        assert service["stop_grace_period"] == "30m"
        assert service["command"][0] == "process-issues"
