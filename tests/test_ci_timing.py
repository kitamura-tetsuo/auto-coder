from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.summarize_ci_timing import summarize

from tests.fixtures.ci_timing import TimingRecorder


def test_nested_and_repeated_spans_keep_distinct_identity(tmp_path: Path) -> None:
    timing = TimingRecorder(tmp_path)
    with timing.span("install", target_role="source-a", target_source_sha="aaa") as first:
        with timing.span("installer", target_role="source-a") as child:
            pass
    with timing.span("install", target_role="source-a-alternate", target_source_sha="aaa") as alternate:
        pass
    report = summarize(tmp_path)
    spans = report["nested_harness_spans"]
    assert isinstance(spans, list)
    assert {event["span_id"] for event in spans} == {first, child, alternate}
    assert next(event for event in spans if event["span_id"] == child)["parent_span_id"] == first
    assert report["incomplete_spans"] == []


def test_unmatched_start_is_incomplete_not_zero_or_success(tmp_path: Path) -> None:
    timing = TimingRecorder(tmp_path)
    assert timing.emit("span_start", span_id="unfinished", parent_span_id=None, phase_label="blocked")
    report = summarize(tmp_path)
    assert [event["span_id"] for event in report["incomplete_spans"]] == ["unfinished"]
    assert "elapsed_ms" not in report["incomplete_spans"][0]
    assert "outcome" not in report["incomplete_spans"][0]


def test_unavailable_destination_does_not_replace_operation_result(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    timing = TimingRecorder(blocked)
    with timing.span("original-operation"):
        result = "original-result"
    assert result == "original-result"
    assert timing.last_error is not None


def _pytest_subprocess(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(body, encoding="utf-8")
    output = tmp_path / "timing"
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "AUTO_CODER_CI_TIMING_DIR": str(output),
        "AUTO_CODER_CI_TIMING_INVOCATION": "subprocess-case",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.ci_timing_plugin", str(test_file)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    return result, summarize(output)


def test_real_pytest_entry_records_native_phases_and_preserves_failure(tmp_path: Path) -> None:
    result, report = _pytest_subprocess(tmp_path, "def test_original_failure():\n    assert False, 'original sentinel'\n")
    assert result.returncode == 1
    assert "original sentinel" in result.stdout
    phases = report["native_pytest_phases"]
    assert isinstance(phases, list)
    assert [(phase["pytest_phase"], phase["outcome"]) for phase in phases] == [
        ("setup", "passed"),
        ("call", "failed"),
        ("teardown", "passed"),
    ]
    assert all(phase["nodeid"].endswith("test_sample.py::test_original_failure") for phase in phases)
    assert all(isinstance(phase["native_duration_ms"], float) for phase in phases)
    assert report["incomplete_pytest_phases"] == []


def test_disabled_pytest_timing_preserves_success_without_records(tmp_path: Path) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_ok():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if key != "AUTO_CODER_CI_TIMING_DIR"}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.ci_timing_plugin", str(test_file)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.glob("timing-*.jsonl"))
