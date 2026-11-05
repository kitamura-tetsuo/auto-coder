"""Optional uv artifact-cache wiring for disposable preparation containers."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from tests.fixtures.ci_timing import recorder

CACHE_ENVIRONMENT_VARIABLE = "AUTO_CODER_INTEGRATION_UV_CACHE_DIR"
CONTAINER_CACHE_DIRECTORY = "/dependency-cache/uv"


@dataclass(frozen=True)
class DependencyCacheArguments:
    arguments: tuple[str, ...]
    condition: str
    host_directory: Path | None


def dependency_cache_arguments(*, namespace: str | None = None, reset: bool = False) -> DependencyCacheArguments:
    """Prepare a configured host cache, or explicitly retain uncached behavior."""

    configured = os.environ.get(CACHE_ENVIRONMENT_VARIABLE)
    if not configured:
        result = DependencyCacheArguments((), "uncached", None)
        _record(result, None)
        return result
    unresolved = Path(configured)
    try:
        unresolved = unresolved.expanduser()
        directory = unresolved.resolve()
        if namespace:
            directory /= namespace
        if reset and directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir() or not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
            raise OSError("cache path is not a writable directory")
        empty = next(directory.iterdir(), None) is None
    except (OSError, RuntimeError) as error:
        result = DependencyCacheArguments((), "fallback", unresolved)
        _record(result, f"{type(error).__name__}: {error}")
        return result
    condition = "cold" if empty else "warm"
    result = DependencyCacheArguments(
        (
            "--mount",
            f"type=bind,src={directory},dst={CONTAINER_CACHE_DIRECTORY}",
            "-e",
            f"UV_CACHE_DIR={CONTAINER_CACHE_DIRECTORY}",
            "-e",
            f"AUTO_CODER_DEPENDENCY_CACHE_CONDITION={condition}",
            "-e",
            f"AUTO_CODER_DEPENDENCY_CACHE_NAMESPACE={namespace or 'shared'}",
        ),
        condition,
        directory,
    )
    _record(result, None)
    return result


def _record(cache: DependencyCacheArguments, error: str | None) -> None:
    recorder().emit(
        "dependency_cache_setup",
        cache_condition=cache.condition,
        cache_directory=str(cache.host_directory) if cache.host_directory else None,
        configured=cache.host_directory is not None,
        fallback_error=error,
    )
