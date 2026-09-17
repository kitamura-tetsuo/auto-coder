import json
import threading
from pathlib import Path
from typing import Optional

from .cloud_provider_instructions import CloudProviderInstructionError, PreparedCloudPrompt, restore_prepared_cloud_task

_lock = threading.Lock()


def _get_path(repo_name: str) -> Path:
    safe_repo = repo_name.replace("/", "_") if repo_name else "global"
    return Path.home() / ".auto-coder" / safe_repo / "managed_prompts.json"


def save_managed_prompt(repo_name: str, task_id: str, prompt: PreparedCloudPrompt) -> None:
    path = _get_path(repo_name)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        data[task_id] = prompt.to_json()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)


def get_managed_prompt(repo_name: str, task_id: str) -> Optional[PreparedCloudPrompt]:
    path = _get_path(repo_name)
    with _lock:
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        payload = data.get(task_id)
        if not payload:
            return None
        try:
            return restore_prepared_cloud_task(payload)
        except CloudProviderInstructionError:
            return None


def recover_original_task(task_text: str, repo_name: str, task_id: str) -> str:
    """Recover the original task from a prompt text and its managed metadata."""
    path = _get_path(repo_name)
    payload = None
    with _lock:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    payload = data.get(task_id)
            except Exception:
                pass

    if payload:
        try:
            prepared = restore_prepared_cloud_task(payload)
            return prepared.original_task
        except CloudProviderInstructionError as e:
            raise RuntimeError(f"Managed prompt metadata is malformed: {e}")
    else:
        if "===== AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS" in task_text:
            raise RuntimeError("Missing managed metadata for a managed prompt")
        return task_text
