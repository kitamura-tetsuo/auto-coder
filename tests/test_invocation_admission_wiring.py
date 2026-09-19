"""Production-wiring regressions for Issue #2009.

Issue #2008 defined the standalone `InvocationAdmissionGate`/`InvocationHandle`
model (see `tests/test_invocation_admission.py`). These tests exercise the
real production boundaries this Issue connects to that model: the shared
`BackendManager._execute_backend_with_providers` invocation boundary, the
`SpecificationValidationLifecycle`/`DecompositionValidationLifecycle` decision
checkpoints, the Jules remote-handoff receipt, and the `AutomationEngine`
daemon lifetime that owns and drives the gate.
"""

import asyncio
import contextvars
import json
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.backend_manager import BackendManager, run_llm_prompt
from auto_coder.decomposition_analyzer import DecompositionIssue
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.exceptions import AutoCoderRetryableBackendError, AutoCoderUsageLimitError
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.invocation_admission import (
    GateState,
    InvocationAdmissionGate,
    InvocationState,
    bind_invocation_target,
    current_invocation_gate,
    install_invocation_gate,
    reset_invocation_gate,
    take_pending_invocation_handle,
)
from auto_coder.jules_engine import _recurrent_implementation_owner, check_and_start_recurrent_jules_tasks
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.shutdown_context import install_admission_check, reset_admission_check
from auto_coder.specification_analyzer import parse_specification_analysis_response
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.utils import CommandExecutor

READY_JSON = json.dumps({"verdict": "READY", "remediation": "NONE", "findings": []})
BLOCKED_JSON = json.dumps(
    {
        "verdict": "BLOCKED",
        "remediation": "EDIT_IN_PLACE",
        "findings": [
            {
                "category": "material_ambiguity",
                "requirement_ids": [],
                "explanation": "The current value is undefined.",
                "clarification": "Define its source.",
                "counterexample": "",
                "missing_normative_boundary": "",
            }
        ],
    }
)

NO_CONTRACT_BODY = "No formal Requirements section is present in this Issue body."


class ScriptedClient:
    """A fake backend CLI client whose calls are scripted per attempt."""

    def __init__(self, outcomes):
        self.model_name = "test-model"
        self.calls = 0
        self.outcomes = list(outcomes)

    def _run_llm_cli(self, prompt, is_noedit=False):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def continue_session(self, session_id, prompt, is_noedit=False):
        return self._run_llm_cli(prompt, is_noedit=is_noedit)

    def get_last_session_id(self):
        return None


def _manager(tmp_path: Path, clients: dict) -> BackendManager:
    first_name = next(iter(clients))
    with patch("pathlib.Path.home", return_value=tmp_path):
        return BackendManager(
            default_backend=first_name,
            default_client=clients[first_name],
            factories={name: (lambda client=client: client) for name, client in clients.items()},
            order=list(clients),
        )


# ---------------------------------------------------------------------------
# backend_manager.py -- the shared final invocation boundary (REQ-001/002/010)
# ---------------------------------------------------------------------------


def test_no_gate_installed_leaves_behavior_unchanged():
    """The default (no daemon lifetime, e.g. a standalone command) admits nothing."""
    assert current_invocation_gate() is None


def test_run_llm_cli_admits_and_settles_around_the_real_provider_call(tmp_path):
    client = ScriptedClient(["fresh response"])
    manager = _manager(tmp_path, {"claude": client})
    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        with bind_invocation_target("owner/repo", "issue#7", "implementation"):
            result = manager._run_llm_cli("prompt")
        assert result == "fresh response"
        assert client.calls == 1
        # A default (non-deferred) call settles as soon as the provider
        # returns: there is no separate durable write for the caller to wait
        # on, so nothing is left unsettled.
        assert gate.unsettled_snapshot() == []
    finally:
        reset_invocation_gate(token)


def test_run_llm_cli_refuses_when_gate_is_draining(tmp_path):
    client = ScriptedClient(["should never be reached"])
    manager = _manager(tmp_path, {"claude": client})
    gate = InvocationAdmissionGate()
    gate.close_admission("shutdown")
    token = install_invocation_gate(gate)
    try:
        with pytest.raises(AutoCoderRetryableBackendError):
            manager._run_llm_cli("prompt")
        assert client.calls == 0
    finally:
        reset_invocation_gate(token)


def test_backend_rotation_after_usage_limit_admits_a_fresh_invocation_per_attempt(tmp_path):
    """Each backend/provider rotation attempt is its own admitted invocation (REQ-002)."""
    claude = ScriptedClient([AutoCoderUsageLimitError("usage limit")])
    codex = ScriptedClient(["fresh response"])
    manager = _manager(tmp_path, {"claude": claude, "codex": codex})
    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        with patch.object(gate, "try_admit", wraps=gate.try_admit) as spy:
            result = manager._run_llm_cli("prompt")
        assert result == "fresh response"
        assert claude.calls == 1
        assert codex.calls == 1
        assert spy.call_count == 2
        # The failed first attempt and the successful second attempt both
        # settled; nothing is left protected.
        assert gate.unsettled_snapshot() == []
    finally:
        reset_invocation_gate(token)


def test_deferred_checkpoint_stays_protected_until_caller_confirms(tmp_path):
    client = ScriptedClient(["fresh response"])
    manager = _manager(tmp_path, {"claude": client})
    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        with bind_invocation_target("owner/repo", "issue#9", "custom-checkpointed-stage", defer_checkpoint=True):
            result = manager._run_llm_cli("prompt")
        assert result == "fresh response"

        unsettled = gate.unsettled_snapshot()
        assert len(unsettled) == 1
        assert unsettled[0].state is InvocationState.CHECKPOINTING
        assert unsettled[0].repository == "owner/repo"
        assert unsettled[0].target == "issue#9"
        assert unsettled[0].stage == "custom-checkpointed-stage"

        handle = take_pending_invocation_handle()
        assert handle is not None
        assert handle.confirm_settled(confirmation_id="ckpt") is True
        assert gate.unsettled_snapshot() == []
    finally:
        reset_invocation_gate(token)


def test_failed_invocation_settles_immediately_with_no_reusable_result(tmp_path):
    client = ScriptedClient([RuntimeError("permanent failure")])
    manager = _manager(tmp_path, {"claude": client})
    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        with bind_invocation_target("owner/repo", "issue#11", "implementation", defer_checkpoint=True):
            with pytest.raises(RuntimeError, match="permanent failure"):
                manager._run_llm_cli("prompt")
        # A terminal failure settles on the spot, regardless of
        # defer_checkpoint: there is nothing reusable left to protect.
        assert gate.unsettled_snapshot() == []
        assert take_pending_invocation_handle() is None
    finally:
        reset_invocation_gate(token)


# ---------------------------------------------------------------------------
# specification_validation_lifecycle.py -- AS-001, REQ-003/004/007/009
# ---------------------------------------------------------------------------


def _run_llm_prompt_analyzer(backend_manager):
    """A `decide()` analyzer that performs a real run_llm_prompt call."""

    def analyzer(manifest, body):
        response = run_llm_prompt("irrelevant prompt text", backend_manager=backend_manager, is_noedit=True)
        return parse_specification_analysis_response(response, manifest)

    return analyzer


def test_specification_decision_settles_only_after_durable_save(tmp_path):
    cli = ScriptedClient([READY_JSON])
    backend_manager = _manager(tmp_path, {"claude": cli})
    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        lifecycle = SpecificationValidationLifecycle("owner/repo", "policy", tmp_path / "decisions.json", analyzer=_run_llm_prompt_analyzer(backend_manager))
        manifest = build_normative_issue_manifest(101, "Title", NO_CONTRACT_BODY)

        decision = lifecycle.decide(manifest, "Title", NO_CONTRACT_BODY)

        assert decision.verdict == "READY"
        assert cli.calls == 1
        assert gate.unsettled_snapshot() == []
        assert lifecycle.store.get(lifecycle.identity(101, "Title", NO_CONTRACT_BODY)) is not None
    finally:
        reset_invocation_gate(token)


def test_specification_checkpoint_write_failure_keeps_invocation_protected(tmp_path):
    cli = ScriptedClient([BLOCKED_JSON])
    backend_manager = _manager(tmp_path, {"claude": cli})
    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        lifecycle = SpecificationValidationLifecycle("owner/repo", "policy", tmp_path / "decisions.json", analyzer=_run_llm_prompt_analyzer(backend_manager))
        manifest = build_normative_issue_manifest(102, "Title", NO_CONTRACT_BODY)

        with patch.object(lifecycle.store, "save", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                lifecycle.decide(manifest, "Title", NO_CONTRACT_BODY)

        # The paid call happened exactly once; the write failure leaves the
        # invocation CHECKPOINTING (protected, retriable) rather than either
        # settling it or triggering a second inference.
        assert cli.calls == 1
        unsettled = gate.unsettled_snapshot()
        assert len(unsettled) == 1
        assert unsettled[0].state is InvocationState.CHECKPOINTING
        assert unsettled[0].checkpoint_failure_count == 1
    finally:
        reset_invocation_gate(token)


def test_specification_validation_refuses_new_inference_while_draining(tmp_path):
    cli = ScriptedClient([READY_JSON])
    backend_manager = _manager(tmp_path, {"claude": cli})
    gate = InvocationAdmissionGate()
    gate.close_admission("shutdown")
    token = install_invocation_gate(gate)
    try:
        lifecycle = SpecificationValidationLifecycle("owner/repo", "policy", tmp_path / "decisions.json", analyzer=_run_llm_prompt_analyzer(backend_manager))
        manifest = build_normative_issue_manifest(103, "Title", NO_CONTRACT_BODY)

        with pytest.raises(AutoCoderRetryableBackendError):
            lifecycle.decide(manifest, "Title", NO_CONTRACT_BODY)

        assert cli.calls == 0
        assert lifecycle.store.get(lifecycle.identity(103, "Title", NO_CONTRACT_BODY)) is None
    finally:
        reset_invocation_gate(token)


# ---------------------------------------------------------------------------
# decomposition_validation_lifecycle.py -- mirrors the specification lifecycle
# ---------------------------------------------------------------------------


def _decomposition_issue(number, title, body):
    return DecompositionIssue(build_normative_issue_manifest(number, title, body), body)


def _decomposition_run_llm_prompt_analyzer(backend_manager):
    """A `decide()` analyzer that performs a real run_llm_prompt call."""
    from auto_coder.decomposition_analyzer import parse_decomposition_analysis_response

    def analyzer(parent, children):
        response = run_llm_prompt("irrelevant prompt text", backend_manager=backend_manager, is_noedit=True)
        return parse_decomposition_analysis_response(response, parent, children)

    return analyzer


DECOMPOSITION_READY_JSON = json.dumps({"verdict": "READY", "remediation": "NONE", "findings": []})
DECOMPOSITION_BLOCKED_JSON = json.dumps(
    {
        "verdict": "BLOCKED",
        "remediation": "EDIT_IN_PLACE",
        "findings": [
            {
                "category": "cross_issue_contradiction",
                "affected_issues": [{"issue_number": 211, "requirement_ids": ["REQ-001"]}],
                "explanation": "The child contradicts the parent's stated scope.",
                "clarification": "Reconcile the child's Requirement with the parent's Objective.",
            }
        ],
    }
)


def test_decomposition_decision_settles_only_after_durable_save(tmp_path):
    cli = ScriptedClient([DECOMPOSITION_READY_JSON])
    backend_manager = _manager(tmp_path, {"claude": cli})

    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        lifecycle = DecompositionValidationLifecycle("owner/repo", "policy", tmp_path / "sets.json", analyzer=_decomposition_run_llm_prompt_analyzer(backend_manager))
        parent = _decomposition_issue(200, "Parent", "## Objective\nCoordinate work.")
        children = [_decomposition_issue(201, "Child", "## Requirements\n- REQ-001: Do the thing.")]
        identity = lifecycle.identity({"number": 200, "id": 2000, "title": "Parent", "body": "## Objective\nCoordinate work."}, [{"number": 201, "id": 2010, "title": "Child", "body": "## Requirements\n- REQ-001: Do the thing."}])

        decision = lifecycle.decide(identity, parent, children)

        assert decision.verdict == "READY"
        assert cli.calls == 1
        assert gate.unsettled_snapshot() == []
    finally:
        reset_invocation_gate(token)


def test_decomposition_checkpoint_write_failure_keeps_invocation_protected(tmp_path):
    cli = ScriptedClient([DECOMPOSITION_BLOCKED_JSON])
    backend_manager = _manager(tmp_path, {"claude": cli})

    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        lifecycle = DecompositionValidationLifecycle("owner/repo", "policy", tmp_path / "sets.json", analyzer=_decomposition_run_llm_prompt_analyzer(backend_manager))
        parent = _decomposition_issue(210, "Parent", "## Objective\nCoordinate work.")
        children = [_decomposition_issue(211, "Child", "## Requirements\n- REQ-001: Do the thing.")]
        identity = lifecycle.identity({"number": 210, "id": 2100, "title": "Parent", "body": "## Objective\nCoordinate work."}, [{"number": 211, "id": 2110, "title": "Child", "body": "## Requirements\n- REQ-001: Do the thing."}])

        with patch.object(lifecycle.store, "save", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                lifecycle.decide(identity, parent, children)

        assert cli.calls == 1
        unsettled = gate.unsettled_snapshot()
        assert len(unsettled) == 1
        assert unsettled[0].state is InvocationState.CHECKPOINTING
        assert unsettled[0].checkpoint_failure_count == 1
    finally:
        reset_invocation_gate(token)


# ---------------------------------------------------------------------------
# jules_engine.py -- REQ-006, AS-006
# ---------------------------------------------------------------------------


def _jules_prompt_file(directory: Path, tags: str = "[jules, recurrent]", name: str = "[maintenance]") -> Path:
    prompt_path = directory / "recurrent_prompt.md"
    prompt_path.write_text(f"---\ntags: {tags}\nname: {name}\n---\nMaintain the application.", encoding="utf-8")
    return prompt_path


@patch("auto_coder.jules_engine.JulesClient")
def test_jules_submission_admits_and_settles_after_receipt_recorded(mock_jules_client_cls):
    with TemporaryDirectory() as directory:
        prompt_path = _jules_prompt_file(Path(directory))
        slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")
        owner = _recurrent_implementation_owner("owner/repo", str(prompt_path))

        jules = mock_jules_client_cls.return_value
        jules.list_sessions.return_value = []
        jules.start_session.return_value = "submitted-session"

        gate = InvocationAdmissionGate()
        token = install_invocation_gate(gate)
        try:
            with (
                patch("auto_coder.jules_engine.os.path.isdir", return_value=True),
                patch("auto_coder.jules_engine.glob.glob", return_value=[str(prompt_path)]),
            ):
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

            jules.start_session.assert_called_once()
            assert slots.active_owners() == (owner,)
            # The local handoff receipt was durably recorded, so the
            # invocation settled without waiting on the remote task itself.
            assert gate.unsettled_snapshot() == []
        finally:
            reset_invocation_gate(token)


@patch("auto_coder.jules_engine.JulesClient")
def test_jules_submission_retains_slot_and_stays_protected_when_receipt_write_fails(mock_jules_client_cls):
    with TemporaryDirectory() as directory:
        prompt_path = _jules_prompt_file(Path(directory))
        slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")

        jules = mock_jules_client_cls.return_value
        jules.list_sessions.return_value = []
        jules.start_session.return_value = "submitted-session"
        slots.record_provider_session = MagicMock(return_value=False)

        gate = InvocationAdmissionGate()
        token = install_invocation_gate(gate)
        try:
            with (
                patch("auto_coder.jules_engine.os.path.isdir", return_value=True),
                patch("auto_coder.jules_engine.glob.glob", return_value=[str(prompt_path)]),
            ):
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

            # A failed receipt write is not a lost invocation: it settles
            # (nothing further can be retried without a fresh submission)
            # but capacity is retained by the caller's existing ownership
            # policy, matching today's non-gate behavior.
            assert gate.unsettled_snapshot() == []
        finally:
            reset_invocation_gate(token)


@patch("auto_coder.jules_engine.JulesClient")
def test_jules_submission_refused_while_draining(mock_jules_client_cls):
    with TemporaryDirectory() as directory:
        prompt_path = _jules_prompt_file(Path(directory))
        slots = ImplementationSlotRepository("owner/repo", 1, Path(directory) / "slots.json")

        jules = mock_jules_client_cls.return_value
        jules.list_sessions.return_value = []

        gate = InvocationAdmissionGate()
        gate.close_admission("shutdown")
        token = install_invocation_gate(gate)
        try:
            with (
                patch("auto_coder.jules_engine.os.path.isdir", return_value=True),
                patch("auto_coder.jules_engine.glob.glob", return_value=[str(prompt_path)]),
            ):
                check_and_start_recurrent_jules_tasks("owner/repo", slots)

            jules.start_session.assert_not_called()
            assert slots.active_owners() == ()
        finally:
            reset_invocation_gate(token)


# ---------------------------------------------------------------------------
# automation_engine.py -- gate ownership, propagation, and lifecycle (REQ-001)
# ---------------------------------------------------------------------------


def test_run_local_critical_installs_invocation_gate_for_worker_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    seen = {}

    def probe():
        seen["gate"] = current_invocation_gate()

    asyncio.run(engine._run_local_critical("probe", probe))

    assert seen["gate"] is engine.invocation_gate


def test_request_graceful_shutdown_closes_invocation_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    assert engine.invocation_gate.state is GateState.RUNNING

    engine.request_graceful_shutdown("test")

    assert engine.invocation_gate.state is GateState.DRAINING
    assert engine.invocation_gate.try_admit(repository="r", target="t", stage="s") is None


def test_request_force_stop_forces_invocation_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.request_graceful_shutdown("test")

    engine.request_force_stop("second interrupt")

    assert engine.invocation_gate.state is GateState.FORCED


def test_invocation_admission_snapshot_reports_daemon_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    snapshot = engine.invocation_admission_snapshot()
    assert snapshot.state is GateState.RUNNING
    assert snapshot.unsettled == ()


# ---------------------------------------------------------------------------
# Issue #2010 -- the admitted invocation's own subprocess/tool tree is never
# interrupted by graceful draining, while an unrelated local subprocess is.
# ---------------------------------------------------------------------------


def _sleep_command(seconds: float):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_admitted_invocations_subprocess_is_never_interrupted_by_drain(tmp_path):
    """`mark_invocation_active` around the real provider call protects its subprocess."""

    subprocess_started = threading.Event()

    class RealSubprocessClient:
        model_name = "test-model"

        def _run_llm_cli(self, prompt, is_noedit=False):
            # Admission already succeeded (this call only happens inside the
            # admitted window), so signal the main thread it is now safe to
            # close admission without racing `try_admit` itself.
            subprocess_started.set()
            result = CommandExecutor.run_command(_sleep_command(0.6), timeout=5, stream_output=False)
            return "done" if result.success else f"failed: {result.stderr}"

        def continue_session(self, session_id, prompt, is_noedit=False):
            return self._run_llm_cli(prompt, is_noedit=is_noedit)

        def get_last_session_id(self):
            return None

    manager = _manager(tmp_path, {"claude": RealSubprocessClient()})
    gate = InvocationAdmissionGate()
    gate_token = install_invocation_gate(gate)
    admission_token = install_admission_check(lambda: gate.state is GateState.RUNNING)

    results: dict = {}

    def run_call():
        with bind_invocation_target("owner/repo", "issue#42", "implementation"):
            results["output"] = manager._run_llm_cli("prompt")

    # See the sibling test below: a bare `threading.Thread` does not inherit
    # the calling context, so the installed gate/admission-check are copied
    # explicitly here (production code gets this via `asyncio.to_thread`).
    ctx = contextvars.copy_context()
    call_thread = threading.Thread(target=lambda: ctx.run(run_call))
    start = time.monotonic()
    call_thread.start()
    assert subprocess_started.wait(2)
    # Close admission only once the provider call's own subprocess is
    # already running: draining must never kill it (REQ-004).
    gate.close_admission("graceful shutdown")
    call_thread.join(timeout=5)
    elapsed = time.monotonic() - start

    reset_admission_check(admission_token)
    reset_invocation_gate(gate_token)

    assert not call_thread.is_alive()
    assert results["output"] == "done"
    # The sleep ran its real 0.6s to completion instead of being killed the
    # moment admission closed (which would show up as a near-instant return).
    assert elapsed >= 0.55


def test_unrelated_subprocess_outside_invocation_is_interrupted_by_drain():
    """A command run outside `mark_invocation_active` is killed, not awaited (REQ-003)."""
    draining = threading.Event()
    admission_token = install_admission_check(lambda: not draining.is_set())

    result_holder: dict = {}

    def run_unrelated_command():
        result_holder["result"] = CommandExecutor.run_command(_sleep_command(5), timeout=30, stream_output=False)

    # A bare `threading.Thread` starts with a fresh, empty contextvar context
    # (only `asyncio.to_thread` copies the calling context automatically), so
    # the installed admission check is copied across explicitly here to
    # exercise the same real cross-thread propagation production code relies
    # on (`_run_local_critical` does this via `asyncio.to_thread`).
    ctx = contextvars.copy_context()
    worker = threading.Thread(target=lambda: ctx.run(run_unrelated_command))
    start = time.monotonic()
    worker.start()
    time.sleep(0.2)
    draining.set()
    worker.join(timeout=5)
    elapsed = time.monotonic() - start

    reset_admission_check(admission_token)

    assert not worker.is_alive()
    result = result_holder["result"]
    assert result.success is False
    assert "graceful shutdown is draining" in result.stderr
    # Killed almost immediately after draining started, nowhere near the 5s sleep.
    assert elapsed < 2.0
