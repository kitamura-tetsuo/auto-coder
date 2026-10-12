from pathlib import Path

from auto_coder.local_job_handoff import ContinuationState
from auto_coder.muse_background_observation import (
    MuseBackgroundObservation,
    MuseContinuationReadiness,
    MuseObservationBinding,
    MuseObservationFold,
    MuseRuntimeConformanceEvidence,
    MuseRuntimeSupport,
    MuseTaskOutcome,
    decode_muse_msp_frame,
    detect_muse_background_support,
    persist_muse_observation,
)
from tests.test_local_job_continuation import _running_job


def _binding(workspace: Path = Path("/work")) -> MuseObservationBinding:
    return MuseObservationBinding("muse", "session-1", "execution-1", "attempt-1", workspace)


def _support(native: bool = False) -> MuseRuntimeSupport:
    return MuseRuntimeSupport(True, "Muse Code 1.4.1-R4503.1", native_wake=native)


def _event(method: str, **params: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": {"sessionId": "session-1", **params}}


def test_live_inventory_disappearance_does_not_manufacture_success() -> None:
    fold = MuseObservationFold(_binding(), "turn-1", _support())
    fold.consume(_event("turn/completed", turnId="turn-1", terminal="completed", viewCursor="opaque-z"))
    fold.consume_task_inventory([{"taskId": "task-1", "turnId": "turn-1", "state": "running", "revision": 1}], "snapshot-a")
    fold.consume_task_inventory([], "snapshot-b")

    missing = fold.observation()
    assert missing.readiness is MuseContinuationReadiness.RECONCILIATION_REQUIRED
    assert missing.tasks == (missing.tasks[0],)
    assert missing.tasks[0].state == "running"
    assert missing.tasks[0].outcome is MuseTaskOutcome.UNKNOWN

    fold.consume(
        _event(
            "item/completed",
            item={"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 2, "status": "failed", "resultId": "result-1", "resultDelivered": True},
            viewCursor="opaque-a",
        )
    )
    failed = fold.observation()
    assert failed.tasks[0].outcome is MuseTaskOutcome.FAILURE
    assert failed.tasks[0].result_reference == "result-1"
    # A later task result cannot erase the observed view gap without an
    # authoritative snapshot/replay reconciliation.
    assert failed.readiness is MuseContinuationReadiness.RECONCILIATION_REQUIRED


def test_adverse_order_duplicate_foreign_and_native_continuation() -> None:
    fold = MuseObservationFold(_binding(), "turn-1", _support(native=True))
    terminal = _event(
        "item/completed",
        item={"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 2, "status": "completed", "resultId": "result-1", "resultDelivered": True},
        viewCursor="cursor-2",
    )
    fold.consume(terminal)  # notification before the command acknowledgement
    fold.consume(terminal)  # replay duplicate
    fold.consume({"method": "item/completed", "params": {"sessionId": "foreign", "item": {"itemId": "task-x", "kind": "toolCall", "background": True, "turnId": "turn-x"}}})
    fold.consume_turn_admission({"turnId": "turn-2", "disposition": "queued"})
    fold.consume(_event("turn/started", turnId="turn-2", viewCursor="cursor-3"))
    fold.consume_result_delivery("turn-2", ["result-1"], "cursor-3")
    fold.consume(_event("turn/completed", turnId="turn-1", terminal="completed", viewCursor="cursor-4"))

    observation = fold.observation()
    assert [task.task_id for task in observation.tasks] == ["task-1"]
    assert observation.active_turns == ("turn-2",)
    assert observation.incorporated_results == ("result-1",)
    assert observation.readiness is MuseContinuationReadiness.NATIVE_ACTIVE


def test_old_revision_and_old_summary_cannot_complete_new_work() -> None:
    fold = MuseObservationFold(_binding(), "turn-2", _support())
    fold.consume(_event("item/completed", item={"itemId": "old", "kind": "agentMessage", "turnId": "turn-1", "text": "old summary"}))
    fold.consume(_event("item/updated", item={"itemId": "task-2", "kind": "toolCall", "background": True, "turnId": "turn-2", "revision": 4, "status": "inProgress"}))
    fold.consume(_event("item/completed", item={"itemId": "task-2", "kind": "toolCall", "background": True, "turnId": "turn-2", "revision": 3, "status": "completed", "resultDelivered": True}))
    fold.consume(_event("turn/completed", turnId="turn-2", terminal="completed"))

    observation = fold.observation()
    assert observation.tasks[0].state == "running"
    assert observation.final_assistant == ""
    assert observation.readiness is MuseContinuationReadiness.NOT_READY


def test_conflicting_terminal_evidence_is_non_authorizing() -> None:
    fold = MuseObservationFold(_binding(), "turn-1", _support())
    for outcome in ("completed", "cancelled"):
        fold.consume(_event("item/completed", item={"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 2, "status": outcome, "resultDelivered": True}, viewCursor=outcome))
    assert fold.observation().readiness is MuseContinuationReadiness.RECONCILIATION_REQUIRED
    assert fold.observation().tasks[0].contradictory


def test_real_decoder_replay_preserves_adverse_notification_order() -> None:
    transcript = Path("tests/fixtures/muse_background_adverse.ndjson")
    frames = [decode_muse_msp_frame(line) for line in transcript.read_bytes().splitlines()]
    fold = MuseObservationFold(_binding(), "turn-1", _support(native=True))
    for frame in frames:
        fold.consume(frame)
    fold.consume_turn_admission({"turnId": "turn-2", "disposition": "queued"})
    fold.consume_result_delivery("turn-2", ["result-1"], "cursor-3")

    observation = fold.observation()
    assert observation.tasks[0].task_id == "task-1"
    assert observation.tasks[0].outcome is MuseTaskOutcome.SUCCESS
    assert observation.active_turns == ("turn-2",)
    assert observation.readiness is MuseContinuationReadiness.NATIVE_ACTIVE


def test_capability_profile_requires_observed_conformance_and_accepts_additions() -> None:
    initialized = {
        "serverInfo": {"version": "1.4.6-R6000.1", "additive": True},
        "schema": {"version": 1, "fingerprint": "any-fingerprint", "additive": True},
    }
    evidence = MuseRuntimeConformanceEvidence(True, True, True, "native")
    support = detect_muse_background_support(initialized, evidence)
    assert support == MuseRuntimeSupport(True, "Muse Code 1.4.6-R6000.1", native_wake=True)

    missing = detect_muse_background_support(initialized, MuseRuntimeConformanceEvidence(task_list_observed=True))
    assert not missing.supported
    assert missing.reason == "missing conformance observations: session replay, task-result delivery, continuation ownership"


def test_persistence_requires_exact_fenced_job_binding(tmp_path: Path) -> None:
    store, offer, claim = _running_job(tmp_path)
    assert store.bind_continuation(claim, "session-1", "executor") is not None
    binding = MuseObservationBinding("codex", "session-1", claim.record.execution_incarnation, "attempt-1", tmp_path.resolve())
    fold = MuseObservationFold(binding, "turn-1", _support())
    fold.consume(_event("turn/completed", turnId="turn-1", terminal="completed"))
    fold.consume(_event("item/completed", item={"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 1, "status": "failed", "resultId": "result-1", "resultDelivered": True}))
    fold.consume(_event("turn/started", turnId="turn-2"))
    fold.consume_result_delivery("turn-2", ["result-1"], "delivery-1")
    fold.consume(_event("turn/completed", turnId="turn-2", terminal="completed"))
    assert persist_muse_observation(store, offer.job_id, "executor", fold.observation())
    retained = store.get_continuation(offer.job_id)
    assert retained is not None and retained.state is ContinuationState.CONTINUATION_PENDING

    foreign = MuseBackgroundObservation(MuseObservationBinding("codex", "foreign", claim.record.execution_incarnation, "attempt-1", tmp_path.resolve()), "turn-1")
    assert not persist_muse_observation(store, offer.job_id, "executor", foreign)
