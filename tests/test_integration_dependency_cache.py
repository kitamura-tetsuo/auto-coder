from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.fixtures.integration_dependency_observation import observe_uv_sync
from tests.utils.integration_dependency_cache import CONTAINER_CACHE_DIRECTORY, dependency_cache_arguments


def test_cache_arguments_distinguish_cold_warm_and_uncached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_CODER_INTEGRATION_UV_CACHE_DIR", raising=False)
    assert dependency_cache_arguments().condition == "uncached"

    cache = tmp_path / "uv-cache"
    monkeypatch.setenv("AUTO_CODER_INTEGRATION_UV_CACHE_DIR", str(cache))
    cold = dependency_cache_arguments()
    assert cold.condition == "cold"
    assert f"UV_CACHE_DIR={CONTAINER_CACHE_DIRECTORY}" in cold.arguments
    assert str(cache.resolve()) in cold.arguments[1]

    (cache / "artifact").write_text("downloaded", encoding="utf-8")
    assert dependency_cache_arguments().condition == "warm"


def test_unusable_cache_falls_back_without_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_file = tmp_path / "not-a-directory"
    cache_file.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AUTO_CODER_INTEGRATION_UV_CACHE_DIR", str(cache_file))
    result = dependency_cache_arguments()
    assert result.condition == "fallback"
    assert result.arguments == ()


def test_observer_delegates_success_and_failure_without_changing_results(tmp_path: Path) -> None:
    executable = tmp_path / "fixture-uv"
    executable.write_text("#!/bin/sh\nshift\nprintf 'Downloaded 4 packages\\n' >&2\nexit \"${UV_FIXTURE_EXIT:-0}\"\n", encoding="utf-8")
    executable.chmod(0o755)
    output = tmp_path / "observations.jsonl"
    success_command = [str(executable), "sync"]
    with observe_uv_sync(executable, output):
        completed = subprocess.run(success_command, capture_output=True, text=True, check=True)
    assert completed.returncode == 0
    record = json.loads(output.read_text(encoding="utf-8").splitlines()[-1])
    assert record["reusable_work"]["downloaded"] == 4
    assert record["parser_status"] == "recognized"

    with pytest.raises(subprocess.CalledProcessError) as raised:
        with observe_uv_sync(executable, output):
            subprocess.run([str(executable), "sync"], capture_output=True, text=True, check=True, env={"UV_FIXTURE_EXIT": "7"})
    assert raised.value.returncode == 7
    assert json.loads(output.read_text(encoding="utf-8").splitlines()[-1])["outcome"] == "CalledProcessError"
