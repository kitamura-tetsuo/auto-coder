"""Recover predecessor task terminals from a settled Muse retained journal."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

from .execution_trace import EventKind, Outcome, get_trace_collector
from .local_job_handoff import LocalJobRecord, LocalJobStore
from .muse_background_observation import recover_muse_observation


def _is_omitted_task_delta(record: dict[str, object], session_id: str) -> bool:
    """Recognize only native markers for omitted ephemeral tool-output deltas."""
    stream = record.get("stream")
    position = record.get("position")
    omitted = record.get("omitted_record")
    if not isinstance(stream, dict) or not isinstance(position, dict) or not isinstance(omitted, dict):
        return False
    return (
        record.get("retained_marker") == "omitted_live_only"
        and type(record.get("schema_version")) is int
        and record["schema_version"] == 1
        and stream.get("kind") == "session"
        and stream.get("id") == session_id
        and isinstance(position.get("id"), str)
        and bool(position["id"])
        and type(position.get("sequence")) is int
        and position["sequence"] > 0
        and omitted.get("record_type") == "status"
        and omitted.get("durability") == "ephemeral"
        and omitted.get("payload_type") == "runtime.session"
        and type(omitted.get("payload_schema_version")) is int
        and omitted["payload_schema_version"] == 1
        and omitted.get("payload_kind") == "task"
        and omitted.get("omission_class") == "task_tool_delta_v1"
    )


def confirm_muse_retained_result(store: LocalJobStore, job: LocalJobRecord) -> bool:
    """Confirm a later assistant candidate only with positive historical evidence.

    Caller must establish writer settlement first. Empty inventory is never
    evidence of completion: every pending predecessor requires its own retained
    terminal event before the candidate's turn began.
    """
    try:
        continuation = store.get_continuation(job.job_id)
        observation = recover_muse_observation(store, job.job_id)
        pending = store.unresolved_continuation_tasks(job.job_id)
        if continuation is None or observation is None or not pending or not continuation.candidate_output or continuation.candidate_frontier != continuation.frontier:
            return False
        if (
            observation.binding.provider_session != continuation.provider_session
            or observation.binding.execution_incarnation != job.execution_incarnation
            or observation.binding.backend_name != job.backend_name
            or observation.binding.upstream_attempt != job.upstream_attempt
            or observation.binding.workspace.resolve() != Path(job.workspace_path).resolve()
            or observation.foreground_state != "completed"
            or observation.queued_turns
            or observation.active_turns
            or observation.final_assistant_turn != observation.foreground_turn
            or observation.final_assistant != continuation.candidate_output
            or any(task.state != "terminal" or not task.result_delivered or task.contradictory for task in observation.tasks)
            or any(task.result_reference or not task.provider_position.startswith("task/list:") for task in pending)
        ):
            return False
        root = (Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "muse").resolve()
        with sqlite3.connect(f"file:{root / 'session-index.db'}?mode=ro", uri=True) as connection:
            metadata = connection.execute("SELECT workspace_root, session_log_path FROM sessions WHERE session_id=?", (continuation.provider_session,)).fetchone()
        if metadata is None or Path(metadata[0]).resolve() != Path(continuation.provider_workspace).resolve():
            return False
        journal = Path(metadata[1]).resolve()
        if not journal.is_relative_to(root / "sessions") or journal.parent.name != continuation.provider_session:
            return False
        starts: dict[str, int] = {}
        last_start = ""
        task_events: dict[str, tuple[str, int, str]] = {}
        messages: dict[str, tuple[str, int]] = {}
        completed: dict[str, int] = {}
        clean_end = 0
        last_timestamp = 0
        metadata_matches = False
        for line in journal.open():
            record = json.loads(line)
            if not isinstance(record, dict):
                return False
            if record.get("retained_frame"):
                continue
            if "retained_marker" in record:
                if not _is_omitted_task_delta(record, continuation.provider_session):
                    return False
                # This marker carries no task lifecycle or terminal evidence.
                # Required terminals must still exist as ordinary records.
                continue
            timestamp = record.get("recorded_at")
            payload = record.get("payload")
            if type(timestamp) is not int or not isinstance(payload, dict):
                return False
            last_timestamp = max(last_timestamp, timestamp)
            kind = payload.get("kind")
            event = payload.get("event", {})
            turn = payload.get("run_id")
            if kind == "metadata":
                native = payload.get("record", {})
                if not isinstance(native, dict):
                    return False
                metadata_matches = native.get("workspace_root") == continuation.provider_workspace
            elif kind == "run" and isinstance(event, dict) and isinstance(turn, str):
                if event.get("kind") == "started":
                    starts[turn] = timestamp
                    last_start = turn
                elif event.get("kind") == "assistant_message_committed" and isinstance(event.get("text"), str):
                    messages[turn] = (event["text"], timestamp)
                elif event.get("kind") == "terminal" and event.get("terminal") == "completed":
                    completed[turn] = timestamp
            elif kind == "task" and isinstance(event, dict) and isinstance(event.get("task_id"), str):
                task_events[event["task_id"]] = (str(event.get("kind", "")), timestamp, str(record.get("id", "")))
            elif kind == "session_end":
                native = payload.get("record", {})
                if not isinstance(native, dict):
                    return False
                if native.get("session_id") != continuation.provider_session:
                    return False
                clean_end = timestamp if native.get("exit_reason") == "clean" else 0
        turn = observation.foreground_turn
        start = starts.get(turn, 0)
        message = messages.get(turn)
        terminal = completed.get(turn, 0)
        if not metadata_matches or not start or last_start != turn or message is None or message[0] != continuation.candidate_output or not (start < message[1] <= terminal <= clean_end) or clean_end != last_timestamp:
            return False
        terminals = []
        for task in pending:
            evidence = task_events.get(task.task_id)
            if evidence is None or evidence[0] != "completed" or not evidence[2] or evidence[1] >= start:
                return False
            # Native transport completion proves termination, not successful
            # tool execution. Keep the domain outcome explicitly unknown.
            terminals.append(replace(task, lifecycle="terminal", terminal_outcome="unknown", provider_position=f"retained-task:{evidence[2]}"))
        confirmed = store.confirm_retained_task_result(job.job_id, continuation.executor_token, continuation.frontier, continuation.candidate_output, tuple(terminals))
        if confirmed:
            get_trace_collector().record_event(
                EventKind.STAGE_RESULT, stage_id="llm.muse-retained-task-reconciliation", origin="local-backend", label="Retained Muse predecessor reconciliation", outcome=Outcome.COMPLETED, facts={"task_count": len(terminals), "job_id": job.job_id, "source": "settled retained session journal"}
            )
        return confirmed
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False
