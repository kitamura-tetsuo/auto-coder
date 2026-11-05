from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures import ci_timing
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


def test_symlink_loop_falls_back_and_preserves_caller_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.symlink_to(second)
    second.symlink_to(first)
    monkeypatch.setenv("AUTO_CODER_INTEGRATION_UV_CACHE_DIR", str(first))
    result = dependency_cache_arguments()
    calls = []
    if not result.arguments:
        calls.append("ordinary-preparation")
    assert result.condition == "fallback"
    assert result.arguments == ()
    assert calls == ["ordinary-preparation"]


def test_observer_delegates_success_and_failure_without_changing_results(tmp_path: Path) -> None:
    executable = tmp_path / "fixture-uv"
    executable.write_text(
        "#!/bin/sh\nshift\nprintf ' Downloading anyio\\n      Built auto-coder @ file:///source\\nPrepared 2 packages in 1ms\\n' >&2\nexit \"${UV_FIXTURE_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    output = tmp_path / "observations.jsonl"
    success_command = [str(executable), "sync"]
    observer = {
        "target_role": "cold",
        "target_source_sha": "a" * 40,
        "attempt_id": "attempt",
        "python_executable": Path(sys.executable),
        "uv_version": "0.7.22",
    }
    with observe_uv_sync(executable, output, **observer):
        completed = subprocess.run(success_command, capture_output=True, text=True, check=True)
    assert completed.returncode == 0
    record = json.loads(output.read_text(encoding="utf-8").splitlines()[-1])
    assert record["reusable_work"]["prepared"] == 2
    assert record["packages"] == {"downloaded": ["anyio"], "built": ["auto-coder"]}
    assert record["parser_status"] == "recognized"

    with pytest.raises(subprocess.CalledProcessError) as raised:
        with observe_uv_sync(executable, output, **observer):
            subprocess.run([str(executable), "sync"], capture_output=True, text=True, check=True, env={"UV_FIXTURE_EXIT": "7"})
    assert raised.value.returncode == 7
    assert json.loads(output.read_text(encoding="utf-8").splitlines()[-1])["outcome"] == "CalledProcessError"


def test_retained_records_establish_attributable_same_input_reduction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_CODER_CI_TIMING_DIR", str(tmp_path))
    monkeypatch.setattr(ci_timing, "_RECORDER", None)
    common = {
        "schema_version": 1,
        "parser_status": "recognized",
        "target_source_sha": "a" * 40,
        "lock_sha256": "b" * 64,
        "python_executable": "/usr/bin/python3.12",
        "uv_version": "0.7.22",
        "cache_namespace": "comparison",
    }
    cold = {
        **common,
        "target_role": "cold",
        "cache_condition": "cold",
        "environment_path": "/runtime/cold",
        "packages": {"downloaded": ["anyio", "httpx"], "built": ["auto-coder"]},
    }
    warm = {
        **common,
        "target_role": "warm",
        "cache_condition": "warm",
        "environment_path": "/runtime/warm",
        "previous_population": {"target_role": "cold", "outcome": "completed"},
        "packages": {"downloaded": [], "built": ["auto-coder"]},
    }
    (tmp_path / "dependency-work-cold.jsonl").write_text(json.dumps(cold) + "\n", encoding="utf-8")
    (tmp_path / "dependency-work-warm.jsonl").write_text(json.dumps(warm) + "\n", encoding="utf-8")
    comparison = ci_timing.observe_dependency_cache_comparison("cold", "warm")
    assert comparison.complete is True
    assert comparison.reusable_cold_work == 2
    assert comparison.reusable_warm_work == 0
    retained = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("timing-*.jsonl"))
    assert '"event_kind": "dependency_cache_comparison"' in retained
