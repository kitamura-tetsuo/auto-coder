"""Durable recovery of the original task behind a composed cloud prompt.

`cloud_provider_instructions.prepare_cloud_task` never marks its output: the
composed bytes sent to a provider carry no in-band signal that would let a
later process reliably split them back into "original task" and "managed
component". That split is exactly what a failed-session replacement or a
recurrent-task restart needs after the current process (and any in-memory
cache) is gone.

This module is the side channel that makes that split possible: every
successful `prepare_cloud_task` call at a client boundary durably records its
full `PreparedCloudPrompt` here, keyed by the provider's own task/session id.
Recovery then prefers that durable record; only a prompt with no record *and*
no managed-instruction marker is treated as a genuine legacy/undecorated
task. A prompt that carries the marker without a matching record cannot be
split safely, so recovery refuses rather than guessing.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from .cloud_provider_instructions import CloudProviderInstructionError, PreparedCloudPrompt, restore_prepared_cloud_task

# Shared prefix of `cloud_provider_instructions._COMPONENT_HEADING` across all
# recipients; recognizing it does not require importing the recipient-parameterized
# constant itself.
_COMPONENT_MARKER_PREFIX = "===== AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS ("

_lock = threading.Lock()


class ManagedPromptRecoveryError(RuntimeError):
    """A saved prompt carries a managed-instruction marker but no durable record."""


def _store_path(repo_name: Optional[str]) -> Path:
    safe_repo = (repo_name or "unknown").replace("/", "_")
    return Path.home() / ".auto-coder" / safe_repo / "managed_prompts.json"


def save_managed_prompt(repo_name: Optional[str], task_id: str, prepared: PreparedCloudPrompt) -> None:
    """Durably associate a prepared prompt record with the task/session id it produced.

    A no-op when `task_id` is falsy (nothing to key the record by).
    """
    if not task_id:
        return
    path = _store_path(repo_name)
    with _lock:
        data: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        data[task_id] = prepared.to_json()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)


def get_managed_prompt(repo_name: Optional[str], task_id: str) -> Optional[PreparedCloudPrompt]:
    """Return the durably retained prepared-prompt record for a task, or None.

    Returns None (never raises) for a missing file, a missing key, or a
    corrupted/unsupported record; callers that need to distinguish "no
    record" from "corrupt record" should use `recover_original_task` instead.
    """
    if not task_id:
        return None
    path = _store_path(repo_name)
    if not path.exists():
        return None
    with _lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    payload = data.get(task_id)
    if not payload:
        return None
    try:
        return restore_prepared_cloud_task(payload)
    except CloudProviderInstructionError:
        return None


def recover_original_task(task_text: str, repo_name: Optional[str], task_id: str) -> str:
    """Recover the exact original (undecorated) task behind a saved/remote prompt.

    A durably retained record (when present and valid) is authoritative and
    is used regardless of `task_text`. Absent a usable record, a prompt that
    contains no managed-instruction marker is a genuine legacy/undecorated
    task and is returned unchanged. A prompt that *does* contain the marker
    but has no matching record cannot be split back into original task and
    managed component without guessing, so this raises
    `ManagedPromptRecoveryError` instead of stripping guessed text or
    resending the opaque payload unchanged.
    """
    record = get_managed_prompt(repo_name, task_id)
    if record is not None:
        return record.original_task
    if _COMPONENT_MARKER_PREFIX in task_text:
        raise ManagedPromptRecoveryError(f"Managed prompt metadata for task '{task_id}' is missing or unreadable, but its saved prompt carries a managed-instruction marker; refusing to guess the original task.")
    return task_text
