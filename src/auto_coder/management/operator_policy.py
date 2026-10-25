"""Strict, non-caching operator policy loading."""

import json
from dataclasses import dataclass
from pathlib import Path


class PolicyUnavailable(Exception):
    """The policy is absent or cannot be used safely."""


@dataclass(frozen=True)
class OperatorPolicy:
    project_id: str
    allowed_uids: frozenset[str]


def read_operator_policy(path: Path, expected_project_id: str) -> OperatorPolicy:
    """Read and validate *path* on every call; no last-known-good value exists."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyUnavailable("operator policy is unavailable") from exc

    if not isinstance(value, dict) or set(value) != {"project_id", "allowed_uids"}:
        raise PolicyUnavailable("operator policy schema is invalid")
    project_id = value["project_id"]
    allowed_uids = value["allowed_uids"]
    if project_id != expected_project_id or not isinstance(project_id, str):
        raise PolicyUnavailable("operator policy project does not match")
    if not isinstance(allowed_uids, list) or any(not isinstance(uid, str) or not uid for uid in allowed_uids):
        raise PolicyUnavailable("operator policy schema is invalid")
    return OperatorPolicy(project_id=project_id, allowed_uids=frozenset(allowed_uids))
