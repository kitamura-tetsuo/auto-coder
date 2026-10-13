import json
from pathlib import Path

from auto_coder.local_job_handoff import ContinuationState, LocalJobStore
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
    recover_muse_observation,
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


def test_unavailable_unrecognized_and_view_gap_retain_known_running_task() -> None:
    def running_fold() -> MuseObservationFold:
        fold = MuseObservationFold(_binding(), "turn-1", _support())
        raw = json.dumps(
            _event(
                "item/updated",
                item={
                    "itemId": "task-1",
                    "kind": "toolCall",
                    "background": True,
                    "turnId": "turn-1",
                    "revision": 1,
                    "status": "inProgress",
                },
                viewCursor="cursor-running",
            )
        ).encode()
        fold.consume(decode_muse_msp_frame(raw))
        return fold

    folds = []
    unavailable = running_fold()
    unavailable.consume_task_inventory(None, "task-list-unavailable")
    folds.append(unavailable)

    unrecognized = running_fold()
    unrecognized.consume_task_inventory([{"kind": "future-workload", "status": "inProgress"}], "task-list-malformed")
    folds.append(unrecognized)

    view_gap = running_fold()
    raw_gap = json.dumps(_event("view/gap", reason="pagination gap", viewCursor="cursor-gap")).encode()
    view_gap.consume(decode_muse_msp_frame(raw_gap))
    folds.append(view_gap)

    for fold in folds:
        observation = fold.observation()
        assert observation.readiness is MuseContinuationReadiness.RECONCILIATION_REQUIRED
        assert observation.readiness is not MuseContinuationReadiness.CONTROLLER_READY
        assert len(observation.tasks) == 1
        retained = observation.tasks[0]
        assert retained.task_id == "task-1"
        assert retained.turn_id == "turn-1"
        assert retained.state == "running"
        assert retained.outcome is MuseTaskOutcome.UNKNOWN
        assert retained.result_reference == ""
        assert not retained.result_delivered


def test_adverse_order_duplicate_foreign_and_native_continuation() -> None:
    fold = MuseObservationFold(_binding(), "turn-1", _support(native=True))
    terminal = _event(
        "item/completed",
        item={"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 2, "status": "completed", "exitCode": 0, "resultId": "result-1", "resultDelivered": True},
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


def test_foreground_summary_never_inherits_native_result_frontier() -> None:
    fold = MuseObservationFold(_binding(), "turn-f", _support(native=True))
    fold.consume(_event("item/completed", item={"itemId": "summary-f", "kind": "agentMessage", "turnId": "turn-f", "text": "premature summary"}))
    fold.consume(_event("turn/completed", turnId="turn-f", terminal="completed"))
    fold.consume(_event("item/completed", item={"itemId": "task-a", "kind": "toolCall", "background": True, "turnId": "turn-f", "revision": 2, "status": "failed"}))
    fold.consume(_event("turn/started", turnId="turn-n"))
    fold.consume_result_delivery("turn-n", ["task-a@2"], "delivery-n")

    before_correction = fold.observation()
    assert before_correction.final_assistant == ""
    assert before_correction.final_assistant_turn == "turn-f"

    fold.consume(_event("item/completed", item={"itemId": "summary-n", "kind": "agentMessage", "turnId": "turn-n", "text": "corrected summary"}))
    fold.consume(_event("turn/completed", turnId="turn-n", terminal="completed"))
    corrected = fold.observation()
    assert corrected.final_assistant == "corrected summary"
    assert corrected.final_assistant_turn == "turn-n"


def test_late_admission_and_duplicate_start_do_not_resurrect_terminal_turn() -> None:
    fold = MuseObservationFold(_binding(), "turn-f", _support(native=True))
    fold.consume(_event("turn/started", turnId="turn-n"))
    fold.consume(_event("turn/completed", turnId="turn-n", terminal="completed"))
    fold.consume_turn_admission({"turnId": "turn-n", "disposition": "started"})
    fold.consume(_event("turn/started", turnId="turn-n"))

    observation = fold.observation()
    assert "turn-n" not in observation.active_turns
    assert "turn-n" not in observation.queued_turns
    assert observation.readiness is not MuseContinuationReadiness.NATIVE_ACTIVE


def test_completed_tool_transport_without_exit_evidence_is_not_success() -> None:
    fold = MuseObservationFold(_binding(), "turn-1", _support())
    fold.consume(_event("item/completed", item={"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 1, "status": "completed", "visibleOutput": "tests failed: exit 1"}))
    assert fold.observation().tasks[0].outcome is MuseTaskOutcome.UNKNOWN

    failed = MuseObservationFold(_binding(), "turn-1", _support())
    failed.consume(_event("item/completed", item={"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 1, "status": "completed", "result": {"exitCode": 1}}))
    assert failed.observation().tasks[0].outcome is MuseTaskOutcome.FAILURE


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


def test_actual_inline_session_replay_recovers_overlap_and_clears_gap() -> None:
    fold = MuseObservationFold(_binding(), "turn-1", _support(native=True))
    fold.consume_task_inventory(None, "unavailable")
    replay = {
        "session": {"sessionId": "session-1"},
        "viewCursor": "cursor-9",
        "history": {
            "mode": "inline",
            "items": [
                {"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 2, "status": "failed"},
                {"itemId": "summary-1", "kind": "agentMessage", "turnId": "turn-1", "text": "failure preserved"},
            ],
        },
        "lastTurn": {"turnId": "turn-1", "terminal": "completed"},
    }
    fold.consume_session_snapshot(replay, "cursor-9")
    fold.consume_session_snapshot(replay, "cursor-9")  # overlapping page/retry

    observation = fold.observation()
    assert observation.readiness is MuseContinuationReadiness.NOT_READY
    assert observation.reconciliation_reason == ""
    assert len(observation.tasks) == 1
    assert observation.tasks[0].task_id == "task-1"
    assert observation.tasks[0].outcome is MuseTaskOutcome.FAILURE
    assert observation.provider_positions.count("cursor-9") == 1


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
    assert retained is not None and retained.state is ContinuationState.CONTROLLER_READY
    recovered = recover_muse_observation(store, offer.job_id)
    assert recovered == fold.observation()
    assert recovered.active_turns == ()
    assert recovered.tasks[0].turn_id == "turn-1"
    assert recovered.tasks[0].revision == 1
    assert recovered.tasks[0].result_delivered
    assert recovered.incorporated_results == ("result-1",)

    native_root = tmp_path / "native"
    native_root.mkdir()
    native_store, native_offer, native_claim = _running_job(native_root)
    assert native_store.bind_continuation(native_claim, "session-1", "executor") is not None
    native_binding = MuseObservationBinding("codex", "session-1", native_claim.record.execution_incarnation, "attempt-1", native_root.resolve())
    native_fold = MuseObservationFold(native_binding, "turn-1", _support(native=True))
    native_fold.consume(_event("item/completed", item={"itemId": "task-1", "kind": "toolCall", "background": True, "turnId": "turn-1", "revision": 4, "status": "failed"}))
    native_fold.consume_result_delivery("turn-2", ["task-1@4"], "delivery-2")
    native_fold.consume(_event("turn/started", turnId="turn-2"))
    assert persist_muse_observation(native_store, native_offer.job_id, "executor", native_fold.observation())
    native_record = native_store.get_continuation(native_offer.job_id)
    assert native_record is not None and native_record.state is ContinuationState.NATIVE_CONTINUATION_ACTIVE
    recovered_native = recover_muse_observation(LocalJobStore(native_store.path), native_offer.job_id)
    assert recovered_native is not None
    assert recovered_native.readiness is MuseContinuationReadiness.NATIVE_ACTIVE
    assert recovered_native.active_turns == ("turn-2",)
    assert recovered_native.tasks[0].revision == 4

    foreign = MuseBackgroundObservation(MuseObservationBinding("codex", "foreign", claim.record.execution_incarnation, "attempt-1", tmp_path.resolve()), "turn-1")
    assert not persist_muse_observation(store, offer.job_id, "executor", foreign)
