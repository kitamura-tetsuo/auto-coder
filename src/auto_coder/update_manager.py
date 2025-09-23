"""Utility helpers for keeping pipx installations up to date."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

import click

from .logger_config import get_logger

logger = get_logger(__name__)

_DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60  # every 6 hours
_STATE_FILENAME = "update_state.json"
_PACKAGE_NAME = "auto-coder"


def _auto_update_disabled() -> bool:
    """Return True when auto-update checks are disabled via environment."""
    flag = os.environ.get("AUTO_CODER_DISABLE_AUTO_UPDATE")
    if not flag:
        return False
    return flag.strip().lower() in {"1", "true", "yes", "on"}


def _running_inside_pipx_env() -> bool:
    """Best-effort detection for pipx-managed execution environments."""
    prefix_path = Path(sys.prefix)
    if any(part.lower() == "pipx" for part in prefix_path.parts):
        return True
    for env_name in ("PIPX_HOME", "PIPX_BIN_DIR"):
        if os.environ.get(env_name):
            return True
    return False


def _get_interval_seconds() -> int:
    """Fetch the auto-update interval from environment or use default."""
    value = os.environ.get("AUTO_CODER_UPDATE_INTERVAL_SECONDS")
    if value is None:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        seconds = int(value)
        if seconds < 0:
            raise ValueError
        return seconds
    except ValueError:
        logger.warning(
            "Invalid AUTO_CODER_UPDATE_INTERVAL_SECONDS=%s; using default %s seconds",
            value,
            _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS


def _state_path() -> Path:
    """Compute the path that stores auto-update state information."""
    override_dir = os.environ.get("AUTO_CODER_UPDATE_STATE_DIR")
    base_dir = Path(override_dir).expanduser() if override_dir else Path.home() / ".cache" / "auto-coder"
    return base_dir / _STATE_FILENAME


def _load_state(path: Path) -> Dict[str, Any]:
    """Load persisted auto-update state from disk."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        logger.warning("Corrupted auto-update state file detected at %s; ignoring", path)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to load auto-update state at %s: %s", path, exc)
    return {}


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    """Persist auto-update state to disk."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True))
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Unable to persist auto-update state at %s: %s", path, exc)


def _notify_manual_update(reason: str) -> None:
    """Display a manual update instruction to the user."""
    message = (
        "Auto-Coder auto-update could not be completed (" f"{reason}" "). "
        "Please run 'pipx upgrade auto-coder' manually."
    )
    click.secho(message, fg="yellow", err=True)
    logger.warning("Auto-update unavailable: %s", reason)


def maybe_run_auto_update() -> None:
    """Attempt to upgrade pipx installations automatically."""
    if _auto_update_disabled():
        logger.debug("Auto-update disabled via AUTO_CODER_DISABLE_AUTO_UPDATE")
        return

    if not _running_inside_pipx_env():
        logger.debug("Not running inside pipx environment; skipping auto-update")
        return

    state_file = _state_path()
    state = _load_state(state_file)

    last_check = float(state.get("last_check", 0.0)) if "last_check" in state else 0.0
    interval = _get_interval_seconds()
    now = time.time()
    if interval > 0 and (now - last_check) < interval:
        logger.debug("Last auto-update check %.1f seconds ago; skipping", now - last_check)
        return

    pipx_executable = shutil.which("pipx")
    if not pipx_executable:
        _notify_manual_update("pipx executable not found in PATH")
        return

    state["last_check"] = now
    _save_state(state_file, state)

    try:
        result = subprocess.run(
            [pipx_executable, "upgrade", _PACKAGE_NAME],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except Exception as exc:
        _notify_manual_update(f"pipx upgrade execution failed: {exc}")
        state["last_result"] = "error"
        state["last_error"] = str(exc)
        _save_state(state_file, state)
        return

    if result.returncode == 0:
        logger.info("Auto-update completed via pipx")
        state["last_result"] = "success"
        state["last_error"] = ""
        if result.stdout:
            logger.debug("pipx upgrade output: %s", result.stdout.strip())
        _save_state(state_file, state)
        return

    reason = result.stderr.strip() or "pipx upgrade returned non-zero exit status"
    _notify_manual_update(reason)
    state["last_result"] = "failure"
    state["last_error"] = reason
    if result.stdout:
        state["last_stdout"] = result.stdout.strip()
    _save_state(state_file, state)
