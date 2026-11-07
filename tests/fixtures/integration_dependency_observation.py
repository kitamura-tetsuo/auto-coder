"""Observe real pinned-uv sync work without changing subprocess behavior."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_SUMMARY = re.compile(r"^(Downloaded|Built|Prepared)\s+(\d+)\s+packages?", re.IGNORECASE)
_PACKAGE = re.compile(r"^\s+(Downloading|Built)\s+([^\s]+)(?:\s+.*)?$", re.IGNORECASE)
_MAX_STREAM = 256_000


def _measurement(command: object, result: subprocess.CompletedProcess[object], condition: str, identity: dict[str, object]) -> dict[str, object]:
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
        category = "downloaded" if package and package.group(1).lower() == "downloading" else "built"
        if package and len(packages[category]) < 200:
            packages[category].append(package.group(2))
            recognized = True
    return {
        "schema_version": 1,
        "cache_condition": condition,
        "command": list(command) if isinstance(command, (list, tuple)) else [],
        "returncode": result.returncode,
        "parser_status": "recognized" if recognized else "unavailable",
        "reusable_work": counts,
        "packages": packages,
        **identity,
    }


@contextmanager
def observe_uv_sync(
    uv_executable: Path,
    observation_path: Path,
    *,
    target_role: str,
    target_source_sha: str,
    attempt_id: str,
    execution_id: str,
    python_executable: Path,
    uv_version: str,
) -> Iterator[None]:
    """Delegate subprocess.run exactly and retain bounded uv work summaries."""

    saved_run = subprocess.run
    configured_condition = os.environ.get("AUTO_CODER_DEPENDENCY_CACHE_CONDITION", "uncached")
    cache = Path(os.environ["UV_CACHE_DIR"]) if "AUTO_CODER_DEPENDENCY_CACHE_CONDITION" in os.environ and "UV_CACHE_DIR" in os.environ else None
    population_marker = cache / ".auto-coder-completed-preparation" if cache else None
    previous_population = _read_marker(population_marker) if population_marker else None
    condition = "warm" if previous_population else configured_condition
    observation_id = str(uuid.uuid4())

    def observed_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        exact_sync = isinstance(command, (list, tuple)) and len(command) > 1 and str(command[0]) == str(uv_executable) and command[1] == "sync"
        identity: dict[str, object] = {
            "observation_id": observation_id,
            "timing_invocation_id": os.environ.get("AUTO_CODER_CI_TIMING_INVOCATION"),
            "target_role": target_role,
            "target_source_sha": target_source_sha,
            "attempt_id": attempt_id,
            "execution_id": execution_id,
            "python_executable": str(python_executable),
            "uv_version": uv_version,
            "cache_namespace": os.environ.get("AUTO_CODER_DEPENDENCY_CACHE_NAMESPACE", "uncached"),
            "previous_population": previous_population,
        }
        if exact_sync and isinstance(command, (list, tuple)) and "--project" in command:
            checkout = Path(str(command[command.index("--project") + 1]))
            identity["checkout_path"] = str(checkout)
            try:
                identity["lock_sha256"] = __import__("hashlib").sha256((checkout / "uv.lock").read_bytes()).hexdigest()
            except OSError:
                identity["lock_sha256"] = None
        environment = kwargs.get("env")
        if isinstance(environment, dict):
            identity["environment_path"] = environment.get("VIRTUAL_ENV")
        try:
            result = saved_run(*args, **kwargs)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
            if exact_sync:
                _write(observation_path, {"schema_version": 1, "cache_condition": condition, "outcome": type(error).__name__, "parser_status": "unavailable", **identity})
            raise
        if exact_sync and isinstance(result, subprocess.CompletedProcess):
            record = _measurement(command, result, condition, identity)
            record["outcome"] = "completed"
            _write(observation_path, record)
        return result

    subprocess.run = observed_run  # type: ignore[assignment]
    try:
        yield
    finally:
        subprocess.run = saved_run  # type: ignore[assignment]


def mark_cache_population(source_sha: str, lock_sha256: str, *, target_role: str, attempt_id: str) -> None:
    """Identify the completed preparation which made later calls warm."""

    configured = os.environ.get("UV_CACHE_DIR") if "AUTO_CODER_DEPENDENCY_CACHE_CONDITION" in os.environ else None
    if not configured:
        return
    marker = Path(configured) / ".auto-coder-completed-preparation"
    previous = _read_marker(marker)
    record = {
        "schema_version": 1,
        "population_id": str(uuid.uuid4()),
        "source_sha": source_sha,
        "lock_sha256": lock_sha256,
        "target_role": target_role,
        "attempt_id": attempt_id,
        "previous_population_id": previous.get("population_id") if previous else None,
        "outcome": "completed",
    }
    try:
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(marker)
    except OSError:
        return


def _read_marker(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("outcome") == "completed" else None


def _write(path: Path, record: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        # Diagnostics are never installation authority.
        return
