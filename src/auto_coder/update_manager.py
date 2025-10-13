"""Utility helpers for keeping pipx installations up to date."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import click

from .logger_config import get_logger

logger = get_logger(__name__)

_DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60  # every 6 hours
_STATE_FILENAME = "update_state.json"
_PACKAGE_NAME = "auto-coder"
_CAPTURE_RESTART_ENV = "AUTO_CODER_TEST_CAPTURE_RESTART"

_STARTUP_ARGS: Optional[tuple[str, ...]] = None
_STARTUP_ENV: Optional[Dict[str, str]] = None


@dataclass
class AutoUpdateResult:
    """Structured result describing a pipx auto-update attempt."""

    attempted: bool
    updated: bool
    reason: str = ""
    stdout: str = ""
    stderr: str = ""


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
    base_dir = (
        Path(override_dir).expanduser()
        if override_dir
        else Path.home() / ".cache" / "auto-coder"
    )
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
        logger.warning(
            "Corrupted auto-update state file detected at %s; ignoring", path
        )
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
        "Auto-Coder auto-update could not be completed ("
        f"{reason}"
        "). "
        "Please run 'pipx upgrade auto-coder' manually."
    )
    click.secho(message, fg="yellow", err=True)
    logger.warning("Auto-update unavailable: %s", reason)


def record_startup_options(
    argv: Sequence[str], env: Optional[Mapping[str, str]] = None
) -> None:
    """Persist the original CLI invocation so restarts can replay it."""

    global _STARTUP_ARGS, _STARTUP_ENV

    try:
        _STARTUP_ARGS = tuple(argv)
    except Exception:
        logger.debug(
            "Failed to capture startup arguments; restart may be unavailable",
            exc_info=True,
        )
        _STARTUP_ARGS = None

    if env is not None:
        try:
            _STARTUP_ENV = dict(env)
        except Exception:
            logger.debug(
                "Failed to capture startup environment snapshot", exc_info=True
            )
            _STARTUP_ENV = None


def _resolve_startup_command(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[tuple[str, ...], Dict[str, str]]:
    """Compute the command/environment that should be used for restart."""

    if argv is None:
        argv = _STARTUP_ARGS
    if not argv:
        return tuple(), {}

    env_snapshot: Optional[Dict[str, str]]
    if env is not None:
        env_snapshot = dict(env)
    elif _STARTUP_ENV is not None:
        env_snapshot = dict(_STARTUP_ENV)
    else:
        env_snapshot = os.environ.copy()

    return tuple(argv), env_snapshot


def _capture_restart_event(argv: Sequence[str], env: Mapping[str, str]) -> None:
    """Record restart intent for tests when capture env var is set."""

    capture_path = os.environ.get(_CAPTURE_RESTART_ENV)
    if not capture_path:
        return

    try:
        path = Path(capture_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "argv": list(argv),
            "env": dict(env),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        logger.info("Auto-update restart captured for tests at %s", path)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to capture restart intent at %s: %s", capture_path, exc)
    raise SystemExit(0)


def restart_with_startup_options(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Restart the current process using the recorded startup arguments."""

    command, restart_env = _resolve_startup_command(argv, env)
    if not command:
        logger.warning(
            "Auto-update requested restart but no startup command is recorded"
        )
        return

    if os.environ.get(
        _CAPTURE_RESTART_ENV
    ):  # pragma: no cover - path exercised via SystemExit in tests
        _capture_restart_event(command, restart_env)
        return

    logger.info("Restarting process after auto-update: %s", " ".join(command))
    os.execvpe(command[0], list(command), restart_env)


def _pipx_upgrade_indicated_change(stdout: str, stderr: str) -> bool:
    """Best-effort heuristic to detect if pipx reported an actual upgrade."""

    combined = f"{stdout}\n{stderr}".lower()
    negative_markers = [
        "already up to date",
        "nothing to upgrade",
        "no action taken",
        "not installed",
    ]
    if any(marker in combined for marker in negative_markers):
        return False

    positive_markers = [
        "upgraded",
        "updated",
        "installing",
        "installed",
        "downloading",
        "downloaded",
    ]
    return any(marker in combined for marker in positive_markers)


def maybe_run_auto_update() -> AutoUpdateResult:
    """Attempt to upgrade pipx installations automatically."""
    if _auto_update_disabled():
        logger.debug("Auto-update disabled via AUTO_CODER_DISABLE_AUTO_UPDATE")
        return AutoUpdateResult(False, False, reason="disabled")

    if not _running_inside_pipx_env():
        logger.debug("Not running inside pipx environment; skipping auto-update")
        return AutoUpdateResult(False, False, reason="outside-pipx")

    state_file = _state_path()
    state = _load_state(state_file)

    last_check = float(state.get("last_check", 0.0)) if "last_check" in state else 0.0
    interval = _get_interval_seconds()
    now = time.time()
    if interval > 0 and (now - last_check) < interval:
        logger.debug(
            "Last auto-update check %.1f seconds ago; skipping", now - last_check
        )
        return AutoUpdateResult(False, False, reason="interval")

    pipx_executable = shutil.which("pipx")
    if not pipx_executable:
        _notify_manual_update("pipx executable not found in PATH")
        return AutoUpdateResult(False, False, reason="pipx-missing")

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
        return AutoUpdateResult(True, False, reason=str(exc), stderr=str(exc))

    if result.returncode == 0:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        updated = _pipx_upgrade_indicated_change(stdout, stderr)
        state["last_result"] = "success"
        state["last_error"] = ""
        if stdout:
            state["last_stdout"] = stdout.strip()
        state["last_returncode"] = 0
        _save_state(state_file, state)
        if updated:
            logger.info("Auto-update completed via pipx")
        else:
            logger.debug("pipx upgrade reported success with no changes")
        return AutoUpdateResult(True, updated, stdout=stdout, stderr=stderr)

    reason = result.stderr.strip() or "pipx upgrade returned non-zero exit status"
    _notify_manual_update(reason)
    state["last_result"] = "failure"
    state["last_error"] = reason
    if result.stdout:
        state["last_stdout"] = result.stdout.strip()
    state["last_returncode"] = result.returncode
    _save_state(state_file, state)
    return AutoUpdateResult(
        True,
        False,
        reason=reason,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def check_for_updates_and_restart() -> None:
    """Check for CLI updates and restart the process when an upgrade is applied."""

    result = maybe_run_auto_update()
    if not result.updated:
        return

    restart_with_startup_options()
