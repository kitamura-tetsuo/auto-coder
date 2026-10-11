"""Shared final-response extraction for local CLI event streams."""

from __future__ import annotations

import json
from typing import Optional


def extract_codex_jsonl_message(response: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Extract the final assistant message from a Codex ``--json`` event stream.

    Returns ``(detected, message, error)``. Once a Codex JSONL envelope is
    detected, malformed lines and failed events are reported instead of being
    ignored so stderr contamination cannot turn an invalid response into PASS.
    """
    lines = [line for line in response.splitlines() if line.strip()]
    if not lines:
        return False, None, None

    try:
        first_event = json.loads(lines[0])
    except json.JSONDecodeError:
        return False, None, None

    if not isinstance(first_event, dict):
        return False, None, None
    first_type = first_event.get("type")
    if not isinstance(first_type, str) or not first_type.startswith(("thread.", "turn.", "item.")):
        return False, None, None

    messages: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            reason = f"non-JSON content in Codex event stream at line {line_number}: {exc.msg}"
            return True, None, reason
        if not isinstance(event, dict):
            return True, None, f"Codex event at line {line_number} is not an object"

        event_type = event.get("type")
        if event_type in ("turn.failed", "error"):
            return True, None, f"Codex emitted failure event {event_type} at line {line_number}"

        item = event.get("item")
        if event_type == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text)

    if not messages:
        return True, None, "Codex event stream contained no completed agent message"
    return True, messages[-1], None


def extract_claude_jsonl_result(response: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Extract Claude CLI's final result from a ``stream-json`` event stream.

    Claude streams one JSON object per line, beginning with ``system/init`` and
    ending with a ``result`` event.  Once that envelope is detected, every line
    and the terminal result are validated so truncated or failed streams cannot
    accidentally authorize a merge.
    """
    lines = [line for line in response.splitlines() if line.strip()]
    if not lines:
        return False, None, None

    try:
        first_event = json.loads(lines[0])
    except json.JSONDecodeError:
        return False, None, None

    if not isinstance(first_event, dict) or first_event.get("type") != "system" or first_event.get("subtype") != "init":
        return False, None, None

    terminal_results: list[tuple[int, dict[str, object]]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return True, None, f"non-JSON content in Claude event stream at line {line_number}: {exc.msg}"
        if not isinstance(event, dict):
            return True, None, f"Claude event at line {line_number} is not an object"
        if event.get("type") == "result":
            terminal_results.append((line_number, event))

    if not terminal_results:
        return True, None, "Claude event stream contained no terminal result"
    if len(terminal_results) != 1:
        return True, None, f"Claude event stream contained {len(terminal_results)} terminal results"

    line_number, terminal = terminal_results[0]
    if terminal.get("subtype") != "success" or terminal.get("is_error") is True:
        subtype = terminal.get("subtype", "unknown")
        return True, None, f"Claude emitted failed terminal result {subtype} at line {line_number}"
    result = terminal.get("result")
    if not isinstance(result, str) or not result.strip():
        return True, None, f"Claude terminal result at line {line_number} contained no response text"
    return True, result, None


def extract_muse_jsonl_result(response: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Extract final assistant output from a Muse ``--json`` event stream.

    Returns ``(detected, message, error)``.
    Once a Muse JSONL envelope is detected, malformed lines or failure events
    are reported instead of being ignored.
    """
    lines = [line for line in response.splitlines() if line.strip()]
    if not lines:
        return False, None, None

    # Skip any non-JSON preamble lines from muse (e.g. 'muse: ...')
    first_index = -1
    for i, line in enumerate(lines):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "record_type" in parsed and "payload_type" in parsed:
                first_index = i
                break
        except (json.JSONDecodeError, ValueError):
            continue

    if first_index == -1:
        return False, None, None

    terminal_text: Optional[str] = None
    assistant_text: Optional[str] = None
    terminal_error: Optional[str] = None
    delta_texts: list[str] = []

    for line_number, line in enumerate(lines[first_index:], start=first_index + 1):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            return True, None, f"non-JSON content in Muse event stream at line {line_number}: {exc}"
        if not isinstance(event, dict):
            return True, None, f"Muse event at line {line_number} is not an object"

        payload_type = event.get("payload_type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue

        if payload_type == "run.terminal.completed":
            text = payload.get("text")
            if isinstance(text, str):
                terminal_text = text
        elif payload_type == "run.terminal.failed":
            reason = payload.get("reason")
            terminal_error = str(reason) if reason else "Muse run terminal failed"
        elif payload_type == "task.lifecycle.failed":
            event_obj = payload.get("event")
            if isinstance(event_obj, dict) and event_obj.get("reason"):
                terminal_error = str(event_obj["reason"])
            elif not terminal_error:
                terminal_error = "Muse task lifecycle failed"
        elif payload_type == "run.output.delta":
            delta = payload.get("text")
            if isinstance(delta, str):
                delta_texts.append(delta)
        elif payload_type == "runtime.session":
            event_obj = payload.get("event")
            if isinstance(event_obj, dict):
                event_kind = event_obj.get("kind")
                payload_kind = payload.get("kind")
                if event_kind == "assistant_message_committed":
                    text = event_obj.get("text")
                    if isinstance(text, str):
                        assistant_text = text
                elif event_kind == "terminal":
                    if event_obj.get("terminal") == "failed":
                        terminal_error = str(event_obj.get("reason") or "Muse run terminal failed")
                    elif event_obj.get("terminal") == "completed" and isinstance(event_obj.get("text"), str):
                        terminal_text = event_obj.get("text")
                elif payload_kind == "run" and event_kind == "failed":
                    terminal_error = str(event_obj.get("reason") or "Muse run failed")

    if terminal_text is not None:
        return True, terminal_text, None

    if assistant_text is not None:
        return True, assistant_text, None

    if terminal_error:
        return True, None, f"Muse emitted failure event: {terminal_error}"

    if delta_texts:
        return True, "".join(delta_texts), None

    return True, None, "Muse event stream contained no terminal completed text"


def extract_cli_response(response: str) -> str:
    """Unwrap known envelopes, refusing malformed or unsuccessful streams."""
    for extract in (extract_codex_jsonl_message, extract_claude_jsonl_result, extract_muse_jsonl_result):
        detected, message, error = extract(response)
        if detected:
            if error or message is None:
                raise ValueError(error or "CLI event stream contained no final response")
            return message
    return response
