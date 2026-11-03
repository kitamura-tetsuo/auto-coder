"""Read-only correlation of the native WHAM follow-up request/response graph."""

from __future__ import annotations

import hashlib
from typing import Optional, Sequence

from .codex_wham_client import WhamTurn


def _parents(turns: Sequence[WhamTurn], by_id: dict[str, WhamTurn]) -> Optional[dict[str, Optional[str]]]:
    parents: dict[str, Optional[str]] = {}
    for turn in turns:
        raw = turn.raw_data
        if not isinstance(raw, dict):
            return None
        inner = raw.get("turn", raw)
        if not isinstance(inner, dict) or raw.get("id", turn.id) != turn.id or inner.get("id", turn.id) != turn.id:
            return None
        values = [container[key] for container, key in ((raw, "parent"), (inner, "previous_turn_id")) if key in container]
        if not values or any(value is not None and (not isinstance(value, str) or not value) for value in values) or len(set(values)) != 1:
            return None
        parent = values[0]
        if parent is not None and parent not in by_id:
            return None
        parents[turn.id] = parent
    visited: set[str] = set()
    for identity in parents:
        path: set[str] = set()
        cursor: Optional[str] = identity
        while cursor is not None and cursor not in visited:
            if cursor in path:
                return None
            path.add(cursor)
            cursor = parents[cursor]
        visited.update(path)
    return parents


def _input_text(turn: WhamTurn) -> Optional[str]:
    raw = turn.raw_data
    if not isinstance(raw, dict):
        return None
    inner = raw.get("turn", raw)
    items = inner.get("input_items") if isinstance(inner, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    message = items[0]
    content = message.get("content")
    if message.get("type") != "message" or message.get("role") != "user" or not isinstance(content, list) or not content:
        return None
    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("content_type") != "text" or not isinstance(item.get("text"), str):
            return None
        parts.append(item["text"])
    return "".join(parts)


def followup_completion(task_id: str, baseline_id: str, fingerprint: str, turns: Sequence[WhamTurn]) -> Optional[tuple[str, str]]:
    """Return native user/assistant identities only for one coherent exact chain.

    Provider mapping order is not causality. Missing nodes, sibling requests,
    duplicate identities/text and contradictory parent/child edges stay unknown.
    No source journal or delivery observation is changed here.
    """
    by_id = {turn.id: turn for turn in turns}
    if len(by_id) != len(turns) or any(not turn.id.startswith(task_id + "~") or turn.role.lower() not in {"user", "human", "assistant", "agent", "bot", "asst"} for turn in turns):
        return None
    parents = _parents(turns, by_id)
    if parents is None:
        return None
    baseline = by_id.get(baseline_id)
    if baseline is None or baseline.role.lower() not in {"assistant", "agent", "bot", "asst"}:
        return None
    users = [turn for turn in turns if turn.role.lower() in {"user", "human"} and parents[turn.id] == baseline_id]
    if len(users) != 1:
        return None
    user = users[0]
    text = _input_text(user)
    if text is None or hashlib.sha256(text.encode("utf-8")).hexdigest() != fingerprint:
        return None
    user_inputs = [_input_text(turn) for turn in turns if turn.role.lower() in {"user", "human"}]
    if any(value is None for value in user_inputs) or sum(hashlib.sha256(value.encode("utf-8")).hexdigest() == fingerprint for value in user_inputs if value is not None) != 1:
        return None
    responses = [turn for turn in turns if turn.role.lower() in {"assistant", "agent", "bot", "asst"} and parents[turn.id] == user.id]
    if len(responses) != 1 or responses[0].status.lower() not in {"completed", "failed", "error", "cancelled", "canceled"}:
        return None
    response = responses[0]
    for parent, child in ((baseline, user), (user, response)):
        if not isinstance(parent.raw_data, dict):
            return None
        children = parent.raw_data.get("children")
        if children is not None and (not isinstance(children, list) or any(not isinstance(identity, str) or identity not in by_id for identity in children) or len(set(children)) != len(children) or child.id not in children):
            return None
    return user.id, response.id
