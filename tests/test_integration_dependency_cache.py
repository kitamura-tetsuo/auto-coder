from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.fixtures import ci_timing
from tests.fixtures.integration_dependency_observation import _write, observe_uv_sync
from tests.utils.integration_dependency_cache import CONTAINER_CACHE_DIRECTORY, dependency_cache_arguments, isolated_cache_namespace


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


def test_unknown_user_cache_falls_back_before_container_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = "~auto_coder_missing_cache_owner_2733/uv-cache"
    monkeypatch.setenv("AUTO_CODER_INTEGRATION_UV_CACHE_DIR", configured)
    result = dependency_cache_arguments()
    assert result.condition == "fallback"
    assert result.arguments == ()
    assert result.host_directory == Path(configured)


def test_concurrent_comparisons_own_independent_cache_namespaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "runner-cache"
    monkeypatch.setenv("AUTO_CODER_INTEGRATION_UV_CACHE_DIR", str(shared))
    first_namespace = isolated_cache_namespace("same-source-cold-warm")
    first_started = threading.Event()
    allow_first_to_finish = threading.Event()
    first_result = []

    def active_preparation() -> None:
        first = dependency_cache_arguments(namespace=first_namespace)
        assert first.host_directory is not None
        active_artifact = first.host_directory / "active-preparation-artifact"
        active_artifact.write_text("in use", encoding="utf-8")
        first_result.append((first, active_artifact))
        first_started.set()
        assert allow_first_to_finish.wait(timeout=5)

    thread = threading.Thread(target=active_preparation)
    thread.start()
    assert first_started.wait(timeout=5)

    second_namespace = isolated_cache_namespace("same-source-cold-warm")
    second = dependency_cache_arguments(namespace=second_namespace)

    first, active_artifact = first_result[0]
    allow_first_to_finish.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first.condition == "cold"
    assert second.condition == "cold"
    assert second.host_directory is not None
    assert second_namespace != first_namespace
    assert second.host_directory != first.host_directory
    assert active_artifact.read_text(encoding="utf-8") == "in use"


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
        "execution_id": "execution",
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
        "timing_invocation_id": ci_timing.recorder().context.invocation_id,
        "outcome": "completed",
        "returncode": 0,
        "execution_id": "current-execution",
    }
    cold = {
        **common,
        "target_role": "cold",
        "cache_condition": "cold",
        "environment_path": "/runtime/cold",
        "observation_id": "cold-observation",
        "attempt_id": "cold-attempt",
        "packages": {"downloaded": ["anyio", "httpx"], "built": ["auto-coder"]},
    }
    warm = {
        **common,
        "target_role": "warm",
        "cache_condition": "warm",
        "environment_path": "/runtime/warm",
        "observation_id": "warm-observation",
        "attempt_id": "warm-attempt",
        "previous_population": {
            "population_id": "cold-population",
            "target_role": "cold",
            "attempt_id": "cold-attempt",
            "source_sha": "a" * 40,
            "lock_sha256": "b" * 64,
            "outcome": "completed",
        },
        "packages": {"downloaded": [], "built": ["auto-coder"]},
    }
    (tmp_path / "dependency-work-cold.jsonl").write_text(json.dumps(cold) + "\n", encoding="utf-8")
    (tmp_path / "dependency-work-warm.jsonl").write_text(json.dumps(warm) + "\n", encoding="utf-8")
    for role in ("cold", "warm"):
        for label in ("worker.prepare", "worker.installer"):
            ci_timing.recorder().emit(
                "span_end",
                phase_label=label,
                target_role=role,
                target_source_sha="a" * 40,
                execution_id="current-execution",
                outcome="completed",
            )
    comparison = ci_timing.observe_dependency_cache_comparison("cold", "warm", cold_execution_id="current-execution", warm_execution_id="current-execution")
    assert comparison.complete is True
    assert comparison.reusable_cold_work == 2
    assert comparison.reusable_warm_work == 0
    retained = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("timing-*.jsonl"))
    assert '"event_kind": "dependency_cache_comparison"' in retained

    # Retain a fully consistent prior pair, then fail actual current appends.
    paths = [tmp_path / f"dependency-work-{role}.jsonl" for role in ("cold", "warm")]
    previous = [path.read_bytes() for path in paths]
    original_open = Path.open

    def deny_current_append(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path in paths and mode == "a":
            raise PermissionError("current measurement append denied")
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as denied:
        denied.setattr(Path, "open", deny_current_append)
        for path, record in zip(paths, (cold, warm)):
            _write(path, {**record, "execution_id": "new-execution"})
    assert [path.read_bytes() for path in paths] == previous
    missing = ci_timing.observe_dependency_cache_comparison("cold", "warm", cold_execution_id="new-execution", warm_execution_id="new-execution")
    assert missing.complete is False
    assert "missing retained cold or warm uv work record" in missing.errors
    assert missing.reusable_cold_work is None
    assert missing.reusable_warm_work is None


@pytest.mark.parametrize(
    "mutation, expected_error",
    [
        ("missing_spans", "missing completed prepare/installer spans"),
        ("stale_invocation", "different timing invocation"),
        ("missing_input", "exact-input/identity evidence is missing"),
        ("wrong_populator", "lineage does not identify"),
    ],
)
def test_comparison_rejects_incomplete_or_unattributable_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, expected_error: str) -> None:
    monkeypatch.setenv("AUTO_CODER_CI_TIMING_DIR", str(tmp_path))
    monkeypatch.setattr(ci_timing, "_RECORDER", None)
    invocation = ci_timing.recorder().context.invocation_id
    common = {
        "parser_status": "recognized",
        "target_source_sha": "a" * 40,
        "lock_sha256": "b" * 64,
        "python_executable": "/usr/bin/python3.12",
        "uv_version": "0.7.22",
        "cache_namespace": "comparison",
        "timing_invocation_id": invocation,
        "outcome": "completed",
        "returncode": 0,
        "execution_id": "current-execution",
        "packages": {"downloaded": ["anyio"], "built": ["auto-coder"]},
    }
    cold = {**common, "target_role": "cold", "cache_condition": "cold", "environment_path": "/cold", "observation_id": "cold", "attempt_id": "cold-attempt"}
    warm = {
        **common,
        "target_role": "warm",
        "cache_condition": "warm",
        "environment_path": "/warm",
        "observation_id": "warm",
        "attempt_id": "warm-attempt",
        "packages": {"downloaded": [], "built": ["auto-coder"]},
        "previous_population": {"population_id": "population", "target_role": "cold", "attempt_id": "cold-attempt", "source_sha": "a" * 40, "lock_sha256": "b" * 64, "outcome": "completed"},
    }
    if mutation == "stale_invocation":
        warm["timing_invocation_id"] = "stale"
    elif mutation == "missing_input":
        warm["lock_sha256"] = None
    elif mutation == "wrong_populator":
        warm["previous_population"] = {**warm["previous_population"], "attempt_id": "other-attempt"}
    for role in ("cold", "warm"):
        if mutation == "missing_spans" and role == "warm":
            continue
        for label in ("worker.prepare", "worker.installer"):
            ci_timing.recorder().emit(
                "span_end",
                phase_label=label,
                target_role=role,
                target_source_sha="a" * 40,
                execution_id="current-execution",
                outcome="completed",
            )
    (tmp_path / "dependency-work-cold.jsonl").write_text(json.dumps(cold) + "\n", encoding="utf-8")
    (tmp_path / "dependency-work-warm.jsonl").write_text(json.dumps(warm) + "\n", encoding="utf-8")
    comparison = ci_timing.observe_dependency_cache_comparison("cold", "warm", cold_execution_id="current-execution", warm_execution_id="current-execution")
    assert comparison.complete is False
    assert any(expected_error in error for error in comparison.errors)


def test_comparison_rejects_old_same_label_spans_from_retained_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prior execution cannot fill a current execution's missing timing evidence."""

    monkeypatch.setenv("AUTO_CODER_CI_TIMING_DIR", str(tmp_path))
    monkeypatch.setattr(ci_timing, "_RECORDER", None)
    timing = ci_timing.recorder()
    common = {
        "parser_status": "recognized",
        "target_source_sha": "a" * 40,
        "lock_sha256": "b" * 64,
        "python_executable": "/usr/bin/python3.12",
        "uv_version": "0.7.22",
        "cache_namespace": "comparison",
        "timing_invocation_id": timing.context.invocation_id,
        "execution_id": "current-execution",
        "outcome": "completed",
        "returncode": 0,
    }
    cold = {
        **common,
        "target_role": "cold",
        "cache_condition": "cold",
        "environment_path": "/cold",
        "observation_id": "cold",
        "attempt_id": "cold-attempt",
        "packages": {"downloaded": ["anyio"], "built": ["auto-coder"]},
    }
    warm = {
        **common,
        "target_role": "warm",
        "cache_condition": "warm",
        "environment_path": "/warm",
        "observation_id": "warm",
        "attempt_id": "warm-attempt",
        "packages": {"downloaded": [], "built": ["auto-coder"]},
        "previous_population": {
            "population_id": "population",
            "target_role": "cold",
            "attempt_id": "cold-attempt",
            "source_sha": "a" * 40,
            "lock_sha256": "b" * 64,
            "outcome": "completed",
        },
    }
    for role in ("cold", "warm"):
        for label in ("worker.prepare", "worker.installer"):
            timing.emit(
                "span_end",
                phase_label=label,
                target_role=role,
                target_source_sha="c" * 40,
                execution_id="prior-execution",
                outcome="completed",
            )
    (tmp_path / "dependency-work-cold.jsonl").write_text(json.dumps(cold) + "\n", encoding="utf-8")
    (tmp_path / "dependency-work-warm.jsonl").write_text(json.dumps(warm) + "\n", encoding="utf-8")

    comparison = ci_timing.observe_dependency_cache_comparison("cold", "warm", cold_execution_id="current-execution", warm_execution_id="current-execution")

    assert comparison.complete is False
    assert comparison.errors.count("missing completed prepare/installer spans for cold") == 1
    assert comparison.errors.count("missing completed prepare/installer spans for warm") == 1
