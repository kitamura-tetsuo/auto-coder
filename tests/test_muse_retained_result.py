"""A resumed result must reconcile predecessor terminals without another model turn."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.execution_trace import get_trace_collector
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.issue_local_job import IssueLocalJobAdapter, issue_job_authorization_ports
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.local_job_handoff import ContinuationState, LocalJobKind
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.muse_background_observation import MuseBackgroundObservation, MuseObservationBinding, persist_muse_observation
from auto_coder.muse_retained_result import confirm_muse_retained_result

from .test_dashboard_observability import _assert_required_stage_visible, _mounted_detail
from .test_explicit_issue_resume import retained_job
from .test_muse_msp import _host


def retained_candidate(tmp_path, monkeypatch, count=1):
    source, store, slots, job, provider = retained_job(tmp_path)
    session = "retained-session"
    with store._connect() as connection:
        connection.execute("UPDATE local_job_continuations SET provider_session=? WHERE job_id=?", (session, job.job_id))
    for index in range(count):
        assert store.observe_continuation_task(job.job_id, job.runner_owner, f"old-{index}", "running", provider_position="task/list:5")
    output = "ACTION_SUMMARY: retained edits and verified tests"
    observation = MuseBackgroundObservation(MuseObservationBinding(job.backend_name, session, job.execution_incarnation, job.upstream_attempt, Path(job.workspace_path)), "resumed-turn", foreground_state="completed", final_assistant=output, final_assistant_turn="resumed-turn")
    assert persist_muse_observation(store, job.job_id, job.runner_owner, observation)
    continuation = store.get_continuation(job.job_id)
    assert store.record_candidate_output(job.job_id, job.runner_owner, continuation.frontier, output)
    assert not store.confirm_final_output(job.job_id, job.runner_owner, continuation.frontier)
    data = tmp_path / "native-data"
    root = data / "muse"
    journal = root / "sessions" / "2026" / "10" / "14" / session / "session.jsonl"
    journal.parent.mkdir(parents=True)
    with sqlite3.connect(root / "session-index.db") as connection:
        connection.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, workspace_root TEXT, session_log_path TEXT)")
        connection.execute("INSERT INTO sessions VALUES (?,?,?)", (session, str(provider), str(journal)))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))

    def record(timestamp, payload, identifier):
        return {"recorded_at": timestamp, "id": identifier, "payload": payload}

    records = [record(1, {"kind": "metadata", "record": {"workspace_root": str(provider)}}, "metadata")]
    records.extend(record(100 + index, {"kind": "task", "event": {"kind": "completed", "task_id": f"old-{index}"}}, f"terminal-{index}") for index in range(count))
    records.extend(
        [
            record(1000, {"kind": "run", "run_id": "resumed-turn", "event": {"kind": "started"}}, "start"),
            record(1100, {"kind": "run", "run_id": "resumed-turn", "event": {"kind": "assistant_message_committed", "text": output}}, "message"),
            record(1200, {"kind": "run", "run_id": "resumed-turn", "event": {"kind": "terminal", "terminal": "completed"}}, "completed"),
            record(1300, {"kind": "session_end", "record": {"session_id": session, "exit_reason": "clean"}}, "end"),
        ]
    )
    journal.write_text("".join(json.dumps(record) + "\n" for record in records))
    return source, store, slots, job, provider, journal, records, observation


def omitted_task_delta(session="retained-session", sequence=98):
    """Native Muse journal marker captured from Issue #5629's retained log."""
    return {
        "retained_marker": "omitted_live_only",
        "schema_version": 1,
        "stream": {"kind": "session", "id": session},
        "position": {"id": f"omitted-delta-{sequence}", "sequence": sequence},
        "omitted_record": {"record_type": "status", "durability": "ephemeral", "payload_type": "runtime.session", "payload_schema_version": 1, "payload_kind": "task", "omission_class": "task_tool_delta_v1"},
    }


@pytest.mark.parametrize("count", [1, 113, 174])
def test_retained_terminal_evidence_confirms_later_output_atomically(tmp_path, monkeypatch, _use_real_commands, count):
    _, store, _, job, _, journal, records, _ = retained_candidate(tmp_path, monkeypatch, count)
    if count == 174:
        records[1:1] = [omitted_task_delta(sequence=index + 1) for index in range(375)]
        journal.write_text("".join(json.dumps(record) + "\n" for record in records))
    before = store.get_continuation(job.job_id)
    assert confirm_muse_retained_result(store, job)
    after = store.get_continuation(job.job_id)
    assert after.state is ContinuationState.FINAL_CONFIRMED
    assert after.final_output == before.candidate_output
    assert after.frontier == after.candidate_frontier == before.frontier + count
    assert after.executor_token == before.executor_token
    assert store.unresolved_continuation_tasks(job.job_id) == ()
    with store._connect() as connection:
        outcomes = connection.execute("SELECT lifecycle, terminal_outcome FROM local_continuation_tasks WHERE job_id=?", (job.job_id,)).fetchall()
    assert [(row["lifecycle"], row["terminal_outcome"]) for row in outcomes] == [("terminal", "unknown")] * count
    assert not confirm_muse_retained_result(store, job)
    assert store.get_continuation(job.job_id) == after


@pytest.mark.parametrize("problem", ["unknown_marker", "durable_omission", "terminal_omission", "foreign_session", "missing_position", "boolean_sequence", "missing_terminal"])
def test_omission_markers_never_replace_required_terminal_evidence(tmp_path, monkeypatch, _use_real_commands, problem):
    _, store, _, job, _, journal, records, _ = retained_candidate(tmp_path, monkeypatch)
    marker = omitted_task_delta()
    if problem == "unknown_marker":
        marker["retained_marker"] = "future-marker"
    elif problem == "durable_omission":
        marker["omitted_record"]["durability"] = "durable"
    elif problem == "terminal_omission":
        marker["omitted_record"]["omission_class"] = "task_terminal_v1"
    elif problem == "foreign_session":
        marker["stream"]["id"] = "another-session"
    elif problem == "missing_position":
        marker.pop("position")
    elif problem == "boolean_sequence":
        marker["position"]["sequence"] = True
    elif problem == "missing_terminal":
        records.pop(1)
    records.insert(1, marker)
    journal.write_text("".join(json.dumps(record) + "\n" for record in records))
    before = store.get_continuation(job.job_id)
    pending = store.unresolved_continuation_tasks(job.job_id)
    assert not confirm_muse_retained_result(store, job)
    assert store.get_continuation(job.job_id) == before
    assert store.unresolved_continuation_tasks(job.job_id) == pending


@pytest.mark.parametrize("problem", ["missing_terminal", "late_terminal", "wrong_output", "unclean_exit", "partial_journal", "wrong_workspace", "outside_journal", "new_turn", "queued_turn", "changed_frontier", "known_result", "resurrected_task", "malformed_metadata", "nonobject_record"])
def test_missing_or_contradictory_evidence_preserves_unfinished_result(tmp_path, monkeypatch, _use_real_commands, problem):
    _, store, _, job, provider, journal, records, observation = retained_candidate(tmp_path, monkeypatch)
    if problem == "missing_terminal":
        records.pop(1)
    elif problem == "late_terminal":
        records[1]["recorded_at"] = 1050
    elif problem == "wrong_output":
        records[-3]["payload"]["event"]["text"] = "ACTION_SUMMARY: unrelated"
    elif problem == "unclean_exit":
        records[-1]["payload"]["record"]["exit_reason"] = "failed"
    elif problem == "new_turn":
        records.append({"id": "new-turn", "recorded_at": 1400, "payload": {"kind": "run", "run_id": "new-turn", "event": {"kind": "started"}}})
    elif problem == "malformed_metadata":
        records[0]["payload"]["record"] = []
    elif problem == "nonobject_record":
        records.append([])
    elif problem == "resurrected_task":
        records.insert(-1, {"id": "reopened-task", "recorded_at": 1250, "payload": {"kind": "task", "event": {"kind": "started", "task_id": "old-0"}}})
    elif problem == "queued_turn":
        assert persist_muse_observation(store, job.job_id, job.runner_owner, replace(observation, queued_turns=("queued",)))
    elif problem == "changed_frontier":
        assert store.observe_continuation_task(job.job_id, job.runner_owner, "new-task", "running", provider_position="new-inventory")
    elif problem == "known_result":
        with store._connect() as connection:
            connection.execute("UPDATE local_continuation_tasks SET result_reference='undelivered-result' WHERE job_id=?", (job.job_id,))
    elif problem in ("wrong_workspace", "outside_journal"):
        with sqlite3.connect(journal.parents[5] / "session-index.db") as connection:
            if problem == "wrong_workspace":
                connection.execute("UPDATE sessions SET workspace_root=?", (str(provider / "other"),))
            else:
                connection.execute("UPDATE sessions SET session_log_path=?", (str(tmp_path / "forged.jsonl"),))
    journal.write_text("".join(json.dumps(record) + "\n" for record in records) + ("{" if problem == "partial_journal" else ""))
    before = store.get_continuation(job.job_id)
    pending = store.unresolved_continuation_tasks(job.job_id)
    assert not confirm_muse_retained_result(store, job)
    assert store.get_continuation(job.job_id) == before
    assert store.unresolved_continuation_tasks(job.job_id) == pending


def test_executor_takeover_during_journal_read_cannot_confirm_old_output(tmp_path, monkeypatch, _use_real_commands):
    _, store, _, job, _, _, _, _ = retained_candidate(tmp_path, monkeypatch)
    before = store.get_continuation(job.job_id)
    tasks = store.unresolved_continuation_tasks(job.job_id)
    confirm = store.confirm_retained_task_result

    def contend(*args):
        assert store.take_over_continuation(job.job_id, before.executor_token, "new-executor", expected_runner_owner=job.runner_owner)
        return confirm(*args)

    monkeypatch.setattr(store, "confirm_retained_task_result", contend)
    assert not confirm_muse_retained_result(store, job)
    after = store.get_continuation(job.job_id)
    assert after.executor_token == "new-executor"
    assert after.final_output == ""
    assert after.frontier == before.frontier
    assert store.unresolved_continuation_tasks(job.job_id) == tasks


@patch("auto_coder.dashboard.ui")
def test_issue_resume_recovers_retained_candidate_without_model_call(mock_ui, tmp_path, monkeypatch, _use_real_commands):
    source, store, slots, job, _, journal, records, _ = retained_candidate(tmp_path, monkeypatch, 174)
    records.insert(1, omitted_task_delta())
    journal.write_text("".join(json.dumps(record) + "\n" for record in records))
    monkeypatch.setattr(IssueLocalJobAdapter, "authorize_recovery", lambda _self, _job: True)
    model = Mock(side_effect=AssertionError("Retained result must not invoke Muse again"))
    monkeypatch.setattr(IssueLocalJobAdapter, "resume_continuation", model)
    engine = AutomationEngine(Mock(), AutomationConfig())
    engine.local_job_store = store
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda _repo: slots)
    monkeypatch.setattr(engine, "_resume_local_issue_publications", lambda *_args, **_kwargs: [SimpleNamespace(disposition="published", pr_number=99)])
    get_trace_collector().clear()
    result = engine._resume_explicit_issue("owner/repo", 41)
    assert result.success, result.target_reason
    after = store.get(job.job_id)
    assert after.state.value == "downstream_effects_pending"
    assert after.execution_incarnation == job.execution_incarnation
    assert (Path(job.workspace_path) / "partial.txt").read_text() == "retained edit\n"
    artifact = store.get_result_artifact(after.result_reference)
    assert json.loads(artifact.output)["output"] == "ACTION_SUMMARY: retained edits and verified tests"
    model.assert_not_called()
    diagram = _mounted_detail(mock_ui, "issue", 41)
    _assert_required_stage_visible(diagram, "Retained Muse predecessor reconciliation")


def test_live_muse_resume_reconciles_predecessors_after_writer_settlement(tmp_path, monkeypatch, _use_real_commands, _use_real_sleep):
    source, store, slots, job, _, journal, _, _ = retained_candidate(tmp_path, monkeypatch, 113)
    host = _host(tmp_path)
    log = tmp_path / "msp.jsonl"
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(log))
    monkeypatch.setenv("MSP_EMPTY_INVENTORY", "1")
    monkeypatch.setenv("MSP_RETAINED_JOURNAL", str(journal))
    monkeypatch.setenv("MSP_ANSWER", "ACTION_SUMMARY: continued verified result")
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", timeout=10)})
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.muse_client.get_llm_config", lambda **_kwargs: config)
    adapter = IssueLocalJobAdapter(issue_job_authorization_ports(Mock(github=Mock())), "owner/repo", store, slots, source)
    monkeypatch.setattr(adapter, "authorize_recovery", lambda _job: True)
    # The earlier candidate was invalidated by a later recorded turn. The new
    # call must produce and confirm its own output, not recover that candidate.
    with journal.open("a") as stream:
        stream.write(json.dumps({"id": "later-turn", "recorded_at": 1400, "payload": {"kind": "run", "run_id": "later-turn", "event": {"kind": "started"}}}) + "\n")
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    runner.resume(job.job_id)
    after = store.get(job.job_id)
    assert after.state.value == "downstream_effects_pending"
    assert store.unresolved_continuation_tasks(job.job_id) == ()
    continuation = store.get_continuation(job.job_id)
    assert continuation.state is ContinuationState.FINAL_CONFIRMED
    assert continuation.final_output == "ACTION_SUMMARY: continued verified result"
    frames = [json.loads(line)["frame"] for line in log.read_text().splitlines()]
    assert sum(frame.get("method") == "session/resume" for frame in frames) == 1
    assert not any(frame.get("method") == "session/start" for frame in frames)
    assert sum(frame.get("method") == "turn/start" for frame in frames) == 1
    assert (Path(job.workspace_path) / "partial.txt").read_text() == "retained edit\n"
    runner.close(wait=True)
