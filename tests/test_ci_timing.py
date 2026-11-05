from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.summarize_ci_timing import summarize

import tests.fixtures.ci_timing as ci_timing
from tests.fixtures.ci_timing import TimingRecorder, installer_span


@pytest.mark.parametrize("diagnostics", ["disabled", "enabled", "unavailable"])
@pytest.mark.parametrize("outcome", ["return", "predicate-error", "timeout", "interrupt"])
def test_management_operation_transition_observes_original_wait_without_changing_outcome(tmp_path: Path, monkeypatch, diagnostics: str, outcome: str) -> None:
    from tests import test_management_self_update_integration as harness

    output = tmp_path / "timing"
    monkeypatch.delenv("AUTO_CODER_CI_TIMING_DIR", raising=False)
    if diagnostics == "unavailable":
        output.write_text("occupied", encoding="utf-8")
    timing = TimingRecorder(output if diagnostics != "disabled" else None)
    monkeypatch.setattr(harness, "recorder", lambda: timing)
    returned = object()
    original_error = RuntimeError("original predicate failure") if outcome == "predicate-error" else KeyboardInterrupt("original interrupt")

    def execute(wait, observed: bool):
        now = 0.0
        calls = 0
        sleeps = []
        cleanup = []

        def sleep(interval):
            nonlocal now
            sleeps.append(interval)
            now += interval

        def predicate():
            nonlocal calls
            calls += 1
            if calls == 2:
                if outcome == "return":
                    return returned
                if outcome != "timeout":
                    raise original_error
            return None

        monkeypatch.setattr(harness, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep))
        result = error = None
        try:
            arguments = {"timeout": 0.5, "interval": 0.25, "description": "original transition"}
            if observed:
                arguments["operation_id"] = "operation-fixture"
            result = wait(predicate, **arguments)
        except BaseException as caught:
            error = caught
        finally:
            cleanup.append("original cleanup")
        return result, error, calls, sleeps, cleanup

    original = execute(harness._wait, False)
    observed = execute(harness._wait_operation_transition, True)
    assert original[0] is observed[0]
    assert original[2:] == observed[2:] == (2, [0.25, 0.25] if outcome == "timeout" else [0.25], ["original cleanup"])
    if outcome == "return":
        assert observed[0] is returned and original[1] is observed[1] is None
    elif outcome == "timeout":
        assert type(original[1]) is type(observed[1]) is AssertionError
        assert str(original[1]) == str(observed[1]) == "timed out waiting for original transition"
    else:
        assert original[1] is observed[1] is original_error

    if diagnostics == "enabled":
        spans = summarize(output)["nested_harness_spans"]
        assert len(spans) == 1
        assert spans[0]["phase_label"] == "management.operation_wait"
        assert spans[0]["wait_boundary"] == "original transition"
        assert spans[0]["operation_id"] == "operation-fixture"
        assert spans[0]["outcome"] == ("completed" if outcome == "return" else "interrupted" if outcome == "interrupt" else "exception")
        assert spans[0]["elapsed_ms"] >= 0
    elif diagnostics == "unavailable":
        assert timing.last_error is not None
        assert output.read_text(encoding="utf-8") == "occupied"
    else:
        assert timing.last_error is None and not output.exists()


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
    report = summarize(blocked)
    assert report["diagnostic_status"] == "unavailable"
    assert report["diagnostic_errors"]


@pytest.mark.parametrize("enabled", [False, True])
def test_installer_span_lock_read_failure_preserves_installer_result(tmp_path: Path, monkeypatch, capfd, enabled: bool) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    lock = checkout / "uv.lock"
    lock.write_text("lock", encoding="utf-8")
    monkeypatch.delenv("AUTO_CODER_CI_TIMING_DIR", raising=False)
    timing = TimingRecorder(tmp_path / "timing" if enabled else None)
    original_read_bytes = Path.read_bytes

    def unavailable(path: Path) -> bytes:
        if path == lock:
            raise OSError("diagnostic lock read failed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    calls: list[tuple[Path, Path]] = []

    def installer(source: Path, environment: Path) -> str:
        calls.append((source, environment))
        return "installed"

    environment = tmp_path / "environment"
    with installer_span(timing, checkout, "worker.installer", target_role="candidate", target_source_sha="a" * 40, uv_version="0.7.22"):
        result = installer(checkout, environment)

    assert result == "installed"
    assert calls == [(checkout, environment)]
    if enabled:
        assert timing.last_error is not None and "diagnostic lock read failed" in timing.last_error
        assert "CI timing diagnostic unavailable" in capfd.readouterr().err
    else:
        assert timing.last_error is None


def test_installer_span_lock_read_failure_preserves_installer_exception(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    lock = checkout / "uv.lock"
    lock.write_text("lock", encoding="utf-8")
    timing = TimingRecorder(tmp_path / "timing")
    original_read_bytes = Path.read_bytes

    def unavailable(path: Path) -> bytes:
        if path == lock:
            raise OSError("diagnostic lock read failed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    original = RuntimeError("original installer failure")

    with pytest.raises(RuntimeError) as raised:
        with installer_span(timing, checkout, "management.installer", target_role="current", target_source_sha="b" * 40, uv_version="0.7.22"):
            raise original

    assert raised.value is original


def test_unavailable_diagnostic_logger_does_not_replace_operation_result(tmp_path: Path, monkeypatch) -> None:
    timing = TimingRecorder(tmp_path)
    timing.last_error = "original diagnostic failure"

    def unavailable_sink(*_args, **_kwargs):
        raise ValueError("logger sink unavailable")

    monkeypatch.setattr(ci_timing.logger, "add", unavailable_sink)
    timing._report_error()
    assert timing.last_error == "original diagnostic failure"


def test_context_uses_executed_checkout_not_pull_request_event_sha(tmp_path: Path, monkeypatch) -> None:
    event_sha = "f" * 40
    monkeypatch.setenv("GITHUB_SHA", event_sha)
    timing = TimingRecorder(tmp_path)
    actual = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    assert timing.context.checkout_sha == actual
    assert timing.context.event_sha == event_sha


def test_docker_propagation_is_opt_in_and_carries_verified_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AUTO_CODER_CI_TIMING_DIR", raising=False)
    monkeypatch.setattr(ci_timing, "_RECORDER", None)
    assert ci_timing.docker_timing_arguments() == []

    monkeypatch.setenv("AUTO_CODER_CI_TIMING_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_CI_TIMING_INVOCATION", "outer-invocation")
    monkeypatch.setattr(ci_timing, "_RECORDER", None)
    arguments = ci_timing.docker_timing_arguments(target_role="nested-install")
    joined = " ".join(arguments)
    assert f"type=bind,src={tmp_path.resolve()},dst=/ci-timing" in joined
    assert "--mount" in arguments
    assert "AUTO_CODER_CI_TIMING_INVOCATION=outer-invocation" in joined
    assert "AUTO_CODER_CI_TIMING_CHECKOUT_SHA=" in joined
    assert "AUTO_CODER_CI_TIMING_TARGET_ROLE=nested-install" in joined
    monkeypatch.setattr(ci_timing, "_RECORDER", None)


def test_docker_timing_path_with_colon_uses_unambiguous_mount_syntax(tmp_path: Path, monkeypatch) -> None:
    timing_directory = tmp_path / "timing:diagnostics"
    monkeypatch.setenv("AUTO_CODER_CI_TIMING_DIR", str(timing_directory))
    monkeypatch.setattr(ci_timing, "_RECORDER", None)

    arguments = ci_timing.docker_timing_arguments(target_role="real-stage")

    assert arguments[:2] == ["--mount", f"type=bind,src={timing_directory.resolve()},dst=/ci-timing"]
    assert timing_directory.is_dir()
    monkeypatch.setattr(ci_timing, "_RECORDER", None)


def test_summary_marks_an_expected_missing_invocation_incomplete(tmp_path: Path) -> None:
    timing = TimingRecorder(tmp_path)
    timing.context = dataclasses.replace(timing.context, invocation_id="worker-bridge")
    with timing.span("worker.batch"):
        pass

    report = summarize(tmp_path, ("worker-bridge", "worker-daemon"))

    assert report["diagnostic_status"] == "incomplete"
    assert report["observed_invocations"] == ["worker-bridge"]
    assert report["missing_invocations"] == ["worker-daemon"]
    assert "worker-daemon" in " ".join(report["diagnostic_errors"])


def test_dedicated_nested_evidence_oracle_accepts_controlled_install_spans(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_CODER_CI_TIMING_DIR", str(tmp_path))
    monkeypatch.setattr(ci_timing, "_RECORDER", None)
    timing = ci_timing.recorder()
    for label in ("worker.prepare", "worker.installer"):
        with timing.span(label, target_role="controlled-install", target_source_sha="a" * 40):
            pass

    evidence = ci_timing.observe_nested_install_evidence("controlled-install", "a" * 40)
    assert evidence.complete is True
    assert set(evidence.labels) == {"worker.prepare", "worker.installer"}
    assert evidence.errors == ()
    monkeypatch.setattr(ci_timing, "_RECORDER", None)


def test_real_harness_boundary_keeps_result_and_cleanup_when_timing_is_unavailable_or_partial(tmp_path: Path, monkeypatch, capfd) -> None:
    outcomes = []
    for case in ("disabled", "unavailable", "partial"):
        root = tmp_path / case
        if case == "disabled":
            monkeypatch.delenv("AUTO_CODER_CI_TIMING_DIR", raising=False)
        elif case == "unavailable":
            root.write_text("occupied", encoding="utf-8")
            monkeypatch.setenv("AUTO_CODER_CI_TIMING_DIR", str(root))
        else:
            root.mkdir()
            (root / "timing-partial.jsonl").write_text('{"event_kind":"diagnostic"}\nnot-json\n', encoding="utf-8")
            monkeypatch.setenv("AUTO_CODER_CI_TIMING_DIR", str(root))
        monkeypatch.setattr(ci_timing, "_RECORDER", None)
        cleanup: list[str] = []
        try:
            docker_arguments = ci_timing.docker_timing_arguments(target_role="real-helper")
            # This models the existing Docker boundary after it has returned
            # the authoritative installed-source receipt.
            receipt = {"source_sha": "b" * 40, "installed_runtime_id": "valid-runtime"}
            evidence = ci_timing.observe_nested_install_evidence("real-helper", receipt["source_sha"])
            assert receipt["installed_runtime_id"] == "valid-runtime"
        finally:
            cleanup.append("original-cleanup")
        outcomes.append((case, receipt, cleanup, bool(docker_arguments), evidence.complete))

    assert [(case, receipt["installed_runtime_id"], cleanup) for case, receipt, cleanup, _mounted, _complete in outcomes] == [
        ("disabled", "valid-runtime", ["original-cleanup"]),
        ("unavailable", "valid-runtime", ["original-cleanup"]),
        ("partial", "valid-runtime", ["original-cleanup"]),
    ]
    assert [(mounted, complete) for _case, _receipt, _cleanup, mounted, complete in outcomes] == [
        (False, False),
        (False, False),
        (True, False),
    ]
    assert "CI timing diagnostic unavailable" in capfd.readouterr().err
    assert summarize(tmp_path / "partial")["diagnostic_status"] == "incomplete"
    monkeypatch.setattr(ci_timing, "_RECORDER", None)


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
    result, report = _pytest_subprocess(
        tmp_path,
        "from tests.fixtures.ci_timing import recorder\n" "def test_original_failure():\n" "    with recorder().span('inside-call'):\n" "        assert False, 'original sentinel'\n",
    )
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
    nested = next(span for span in report["nested_harness_spans"] if span["phase_label"] == "inside-call")
    call = next(phase for phase in phases if phase["pytest_phase"] == "call")
    assert nested["nodeid"] == call["nodeid"]
    assert nested["parent_span_id"] == call["span_id"]


def test_fixture_error_preserves_setup_error_and_marks_call_not_reached(tmp_path: Path) -> None:
    result, report = _pytest_subprocess(
        tmp_path,
        "import pytest\n" "@pytest.fixture\n" "def broken():\n" "    raise RuntimeError('fixture sentinel')\n" "def test_fixture_error(broken):\n" "    raise AssertionError('call must not run')\n",
    )
    assert result.returncode == 1
    assert "fixture sentinel" in result.stdout
    phases = report["native_pytest_phases"]
    assert [(phase["pytest_phase"], phase["outcome"]) for phase in phases] == [("setup", "failed"), ("teardown", "passed")]
    assert not any(event.get("pytest_phase") == "call" for event in report["incomplete_pytest_phases"])


def test_observed_interrupt_preserves_interrupt_cleanup_and_incomplete_call(tmp_path: Path) -> None:
    test_file = tmp_path / "test_interrupt.py"
    boundary = tmp_path / "entered"
    cleanup = tmp_path / "cleanup"
    test_file.write_text(
        "import pathlib,pytest,time\n"
        f"boundary=pathlib.Path({str(boundary)!r})\n"
        f"cleanup=pathlib.Path({str(cleanup)!r})\n"
        "@pytest.fixture\n"
        "def owned():\n"
        "    yield\n"
        "    cleanup.write_text('done')\n"
        "def test_blocking(owned):\n"
        "    boundary.write_text('entered')\n"
        "    while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    output = tmp_path / "timing"
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "AUTO_CODER_CI_TIMING_DIR": str(output)}
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.ci_timing_plugin", str(test_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    deadline = time.monotonic() + 15
    observed = False
    while time.monotonic() < deadline:
        report = summarize(output)
        observed = boundary.exists() and any(event.get("pytest_phase") == "call" for event in report["incomplete_pytest_phases"])
        if observed:
            break
        time.sleep(0.05)
    assert observed, "controller never observed the real blocking call boundary"
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode not in (0, None), stdout + stderr
    assert "KeyboardInterrupt" in stdout + stderr
    assert cleanup.read_text(encoding="utf-8") == "done"
    report = summarize(output)
    assert any(event.get("pytest_phase") == "call" for event in report["incomplete_pytest_phases"])


def test_unavailable_pytest_output_reports_loss_without_changing_success(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.write_text("not a directory", encoding="utf-8")
    test_file = tmp_path / "test_ok.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "AUTO_CODER_CI_TIMING_DIR": str(target)}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.ci_timing_plugin", str(test_file)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CI timing diagnostic unavailable" in result.stdout + result.stderr
    assert summarize(target)["diagnostic_status"] == "unavailable"


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


def test_enabled_and_disabled_preserve_same_failure_and_cleanup(tmp_path: Path) -> None:
    outcomes = []
    for enabled in (False, True):
        case = tmp_path / ("enabled" if enabled else "disabled")
        case.mkdir()
        cleanup = case / "cleanup"
        test_file = case / "test_failure.py"
        test_file.write_text(
            "import pathlib,pytest\n" f"cleanup=pathlib.Path({str(cleanup)!r})\n" "@pytest.fixture\n" "def owned():\n" "    yield\n" "    cleanup.write_text('done')\n" "def test_failure(owned):\n" "    assert False, 'same original failure'\n",
            encoding="utf-8",
        )
        environment = {key: value for key, value in os.environ.items() if key != "AUTO_CODER_CI_TIMING_DIR"}
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        if enabled:
            environment["AUTO_CODER_CI_TIMING_DIR"] = str(case / "timing")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "tests.ci_timing_plugin", str(test_file)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        outcomes.append((result.returncode, "same original failure" in result.stdout, cleanup.read_text(encoding="utf-8")))
    assert outcomes == [(1, True, "done"), (1, True, "done")]
