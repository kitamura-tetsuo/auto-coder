"""Observe real pinned-uv sync work without changing subprocess behavior."""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_SUMMARY = re.compile(r"^(Downloaded|Built|Prepared)\s+(\d+)\s+packages?", re.IGNORECASE)
_PACKAGE = re.compile(r"^\s*(Downloaded|Built)\s+([^\s]+)(?:\s+v?[^\s]+)?\s*$", re.IGNORECASE)
_MAX_STREAM = 256_000


def _measurement(command: object, result: subprocess.CompletedProcess[object], condition: str) -> dict[str, object]:
    counts = {"downloaded": 0, "built": 0, "prepared": 0}
    packages: dict[str, list[str]] = {"downloaded": [], "built": []}
    recognized = False
    streams = [value for value in (result.stdout, result.stderr) if isinstance(value, (str, bytes))]
    text = "\n".join(value.decode(errors="replace") if isinstance(value, bytes) else value for value in streams)[:_MAX_STREAM]
    for line in text.splitlines():
        summary = _SUMMARY.match(line.strip())
        if summary:
            counts[summary.group(1).lower()] = int(summary.group(2))
            recognized = True
            continue
        package = _PACKAGE.match(line)
        if package and len(packages[package.group(1).lower()]) < 200:
            packages[package.group(1).lower()].append(package.group(2))
            recognized = True
    return {
        "schema_version": 1,
        "cache_condition": condition,
        "command": list(command) if isinstance(command, (list, tuple)) else [],
        "returncode": result.returncode,
        "parser_status": "recognized" if recognized else "unavailable",
        "reusable_work": counts,
        "packages": packages,
    }


@contextmanager
def observe_uv_sync(uv_executable: Path, observation_path: Path) -> Iterator[None]:
    """Delegate subprocess.run exactly and retain bounded uv work summaries."""

    saved_run = subprocess.run
    configured_condition = os.environ.get("AUTO_CODER_DEPENDENCY_CACHE_CONDITION", "uncached")
    cache = Path(os.environ["UV_CACHE_DIR"]) if "UV_CACHE_DIR" in os.environ else None
    population_marker = cache / ".auto-coder-completed-preparation" if cache else None
    condition = "warm" if population_marker and population_marker.exists() else configured_condition

    def observed_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        exact_sync = isinstance(command, (list, tuple)) and len(command) > 1 and str(command[0]) == str(uv_executable) and command[1] == "sync"
        try:
            result = saved_run(*args, **kwargs)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
            if exact_sync:
                _write(observation_path, {"schema_version": 1, "cache_condition": condition, "outcome": type(error).__name__, "parser_status": "unavailable"})
            raise
        if exact_sync and isinstance(result, subprocess.CompletedProcess):
            record = _measurement(command, result, condition)
            record["outcome"] = "completed"
            _write(observation_path, record)
        return result

    subprocess.run = observed_run  # type: ignore[assignment]
    try:
        yield
    finally:
        subprocess.run = saved_run  # type: ignore[assignment]


def mark_cache_population(source_sha: str, lock_sha256: str) -> None:
    """Identify the completed preparation which made later calls warm."""

    configured = os.environ.get("UV_CACHE_DIR")
    if not configured:
        return
    _write(
        Path(configured) / ".auto-coder-completed-preparation",
        {"schema_version": 1, "source_sha": source_sha, "lock_sha256": lock_sha256, "outcome": "completed"},
    )


def _write(path: Path, record: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        # Diagnostics are never installation authority.
        return
