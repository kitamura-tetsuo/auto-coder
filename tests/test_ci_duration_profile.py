"""End-to-end native pytest evidence and immutable duration profile regressions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auto_coder.ci_duration_profile import ProfileContext, ProfileIdentity, ProfileInputError, build_profile, write_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {
    "repository": "kitamura-tetsuo/auto-coder",
    "profile_id": "pr-tests-native",
    "configuration_digest": "sha256:" + "b" * 64,
    "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
}


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_pytest(tmp_path: Path, source: str, *, repeat_node: bool = False) -> tuple[subprocess.CompletedProcess[str], list[Path]]:
    test_file = tmp_path / "test_observed.py"
    test_file.write_text(source, encoding="utf-8")
    output = tmp_path / "timing"
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AUTO_CODER_CI_TIMING_DIR": str(output),
        "AUTO_CODER_CI_TIMING_INVOCATION": "duration-real-test",
    }
    for name in ("GITHUB_SHA", "GITHUB_WORKFLOW", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"):
        environment.pop(name, None)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "tests.ci_timing_plugin", str(test_file), *([str(test_file), "--keep-duplicates"] if repeat_node else [])], cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30)
    return result, sorted(output.glob("timing-*.jsonl"))


def _records(files: list[Path]) -> list[dict[str, object]]:
    return [json.loads(line) for file in files for line in file.read_text(encoding="utf-8").splitlines()]


def _context(tmp_path: Path, when: datetime | None = None, profile: dict[str, object] | None = None) -> Path:
    path = tmp_path / "context.json"
    _write_json(path, {"schema_version": "ci-duration-context-v1", "profile": profile or PROFILE, "evaluation_time_utc": (when or datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()})
    return path


def _batch(tmp_path: Path, files: list[Path], *, name: str = "batch", profile: dict[str, object] | None = None, kind: str = "local") -> Path:
    native_context = _records(files)[0]["context"]
    origin = {
        "kind": kind,
        **{key: native_context[key] for key in ("checkout_sha", "event_sha", "workflow", "run_id", "run_attempt", "job_key", "invocation_id")},
    }
    path = tmp_path / f"{name}.json"
    _write_json(path, {"schema_version": "ci-duration-batch-v1", "profile": profile or PROFILE, "origin": origin, "files": [os.path.relpath(file, tmp_path) for file in files]})
    return path


def _cli(context: Path, output: Path, *, batches: list[Path] | None = None, previous: list[Path] | None = None) -> subprocess.CompletedProcess[str]:
    arguments = [sys.executable, str(ROOT / "scripts" / "update_ci_duration_profile.py"), "--context", str(context), "--output", str(output), "--log-dir", str(context.parent / "diagnostics")]
    for manifest in batches or []:
        arguments.extend(["--batch", str(manifest)])
    for snapshot in previous or []:
        arguments.extend(["--previous", str(snapshot)])
    return subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True, timeout=40)


def _run_ok(context: Path, output: Path, *, batches: list[Path] | None = None, previous: list[Path] | None = None) -> dict[str, object]:
    execution = _cli(context, output, batches=batches, previous=previous)
    assert execution.returncode == 0, execution.stdout + execution.stderr
    return _read_json(output)


def test_real_pytest_phases_to_cli_preserve_parameterization_outcomes_and_nested_spans(tmp_path: Path) -> None:
    result, files = _run_pytest(
        tmp_path,
        "import pytest\n"
        "from tests.fixtures.ci_timing import recorder\n"
        "@pytest.mark.parametrize('number',[3,5])\n"
        "def test_parameterized(number):\n"
        "    with recorder().span('inside-native-call'): assert number > 0\n"
        "def test_failed(): assert False, 'original assertion sentinel'\n"
        "@pytest.mark.xfail(reason='expected sentinel')\n"
        "def test_xfail(): assert False\n"
        "@pytest.mark.skip(reason='before call')\n"
        "def test_skipped(): assert False\n"
        "@pytest.fixture\n"
        "def setup_failure(): raise RuntimeError('setup sentinel')\n"
        "def test_setup_failure(setup_failure): assert False\n"
        "@pytest.fixture\n"
        "def teardown_failure():\n"
        "    yield\n"
        "    raise RuntimeError('teardown sentinel')\n"
        "def test_teardown_failure(teardown_failure): assert True\n",
    )
    assert result.returncode == 1
    assert "original assertion sentinel" in result.stdout
    assert "setup sentinel" in result.stdout and "teardown sentinel" in result.stdout
    assert len(files) == 1
    native = _records(files)
    assert any(record.get("event_kind") == "span_end" and record.get("phase_label") == "inside-native-call" for record in native)
    context = _context(tmp_path)
    batch = _batch(tmp_path, files)
    snapshot = _run_ok(context, tmp_path / "profile.json", batches=[batch])
    assert snapshot["schema_version"] == "ci-duration-profile-v1"
    assert snapshot["status"] == "partial"  # A skipped-before-call and a failed setup are incomplete.
    assert len(snapshot["nodes"]) == 5
    samples = [entry["samples"][0] for entry in snapshot["nodes"].values()]
    assert {sample["nodeid"].split("::")[-1] for sample in samples} == {"test_parameterized[3]", "test_parameterized[5]", "test_failed", "test_xfail", "test_teardown_failure"}
    assert any(sample["phase_outcomes"]["call"]["outcome"] == "failed" for sample in samples)
    assert any(sample["phase_outcomes"]["call"]["wasxfail"] == "expected sentinel" for sample in samples)
    assert any(sample["phase_outcomes"]["teardown"]["outcome"] == "failed" for sample in samples)
    for sample in samples:
        reports = {record["pytest_phase"]: record["native_duration_ms"] for record in native if record.get("event_kind") == "pytest_phase_report" and record.get("nodeid") == sample["nodeid"]}
        assert set(reports) == {"setup", "call", "teardown"}
        assert sample["raw_duration_seconds"] == math.fsum(reports[phase] / 1000 for phase in ("setup", "call", "teardown"))
        assert sample["duration_seconds"] >= 0.001
        assert all(sample["phase_span_ids"].values())
    assert (tmp_path / "diagnostics" / "ci-duration-profile.log").read_text(encoding="utf-8").find("partial") >= 0
    inherited = _run_ok(context, tmp_path / "inherited.json", previous=[tmp_path / "profile.json"])
    assert inherited["status"] == "partial"
    assert inherited["nodes"] == snapshot["nodes"]
    assert "source snapshot has partial diagnostics" in " ".join(inherited["reasons"])


def test_repeated_native_execution_never_splices_incomplete_phase_boundaries(tmp_path: Path) -> None:
    result, files = _run_pytest(tmp_path, "def test_repeated(): assert True\n", repeat_node=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(files) == 1
    recorded = _records(files)
    starts = {phase: [event for event in recorded if event.get("event_kind") == "pytest_phase_start" and event.get("pytest_phase") == phase] for phase in ("setup", "call", "teardown")}
    assert all(len(events) == 2 for events in starts.values())
    first_ids = {phase: starts[phase][0]["span_id"] for phase in starts}
    second_ids = {phase: starts[phase][1]["span_id"] for phase in starts}
    assert len(set(first_ids.values()) | set(second_ids.values())) == 6
    context = _context(tmp_path)
    original_batch = _batch(tmp_path, files)
    full = _run_ok(context, tmp_path / "all-executions.json", batches=[original_batch])
    nodeid = starts["setup"][0]["nodeid"]
    assert full["status"] == "available"
    assert full["nodes"][nodeid]["sample_count"] == 2

    # Both executions are incomplete: setup1/call1-start and
    # setup2-start/call2/teardown2 cannot form a single measurement.
    include = {
        first_ids["setup"]: True,
        first_ids["call"]: False,
        second_ids["setup"]: False,
        second_ids["call"]: True,
        second_ids["teardown"]: True,
    }
    truncated = [event for event in recorded if event.get("span_id") in include and (event.get("event_kind") == "pytest_phase_start" or (event.get("event_kind") == "pytest_phase_report" and include[event["span_id"]]))]
    damaged_file = tmp_path / "partial-repetitions.jsonl"
    damaged_file.write_text("\n".join(json.dumps(event, sort_keys=True) for event in truncated) + "\n", encoding="utf-8")
    damaged = _run_ok(context, tmp_path / "partial-repetitions-profile.json", batches=[_batch(tmp_path, [damaged_file], name="partial-repetitions")])
    assert damaged["status"] == "unavailable"
    assert damaged["nodes"] == {}
    assert "incomplete" in " ".join(damaged["reasons"])

    # A later independently complete execution is retained despite a
    # conflicting setup report in the first execution.
    first_setup_report = next(event for event in recorded if event.get("event_kind") == "pytest_phase_report" and event.get("span_id") == first_ids["setup"])
    conflict = json.loads(json.dumps(first_setup_report))
    conflict["native_duration_ms"] += 1
    conflicting_file = tmp_path / "conflicting-repetitions.jsonl"
    conflicting_file.write_text("\n".join(json.dumps(event, sort_keys=True) for event in [*recorded, conflict]) + "\n", encoding="utf-8")
    retained = _run_ok(context, tmp_path / "conflicting-repetitions-profile.json", batches=[_batch(tmp_path, [conflicting_file], name="conflicting-repetitions")])
    assert retained["status"] == "partial"
    assert retained["nodes"][nodeid]["sample_count"] == 1
    assert retained["nodes"][nodeid]["samples"][0]["phase_span_ids"] == second_ids
    assert "conflicting native phase" in " ".join(retained["reasons"])

    second_only = [event for event in recorded if event.get("span_id") in set(second_ids.values())]
    complete_file = tmp_path / "complete-second.jsonl"
    complete_file.write_text("\n".join(json.dumps(event, sort_keys=True) for event in second_only) + "\n", encoding="utf-8")
    independently_complete = _run_ok(context, tmp_path / "complete-second-profile.json", batches=[_batch(tmp_path, [complete_file], name="complete-second")])
    assert independently_complete["status"] == "available"
    assert independently_complete["nodes"][nodeid]["sample_count"] == 1
    assert independently_complete["nodes"][nodeid]["samples"][0]["phase_span_ids"] == second_ids


def test_duplicate_delivery_conflicting_phase_and_invalid_new_batch_preserve_history(tmp_path: Path) -> None:
    result, files = _run_pytest(tmp_path, "def test_original(): assert True\n")
    assert result.returncode == 0
    context = _context(tmp_path)
    batch = _batch(tmp_path, files)
    first = _run_ok(context, tmp_path / "first.json", batches=[batch, batch])
    assert first["status"] == "available"
    node = next(iter(first["nodes"]))
    assert first["nodes"][node]["sample_count"] == 1
    second = _run_ok(context, tmp_path / "second.json", batches=[batch, batch])
    assert (tmp_path / "first.json").read_bytes() == (tmp_path / "second.json").read_bytes()

    conflicting = tmp_path / "conflicting.jsonl"
    raw_lines = files[0].read_text(encoding="utf-8").splitlines()
    report = next(json.loads(line) for line in raw_lines if json.loads(line).get("event_kind") == "pytest_phase_report" and json.loads(line).get("pytest_phase") == "call")
    report["native_duration_ms"] += 100
    conflicting.write_text("\n".join([*raw_lines, json.dumps(report, sort_keys=True)]) + "\n", encoding="utf-8")
    conflict_batch = _batch(tmp_path, [conflicting], name="conflict")
    merged = _run_ok(context, tmp_path / "merged.json", batches=[conflict_batch], previous=[tmp_path / "first.json"])
    assert merged["status"] == "partial"
    assert merged["nodes"][node] == first["nodes"][node]
    assert "conflicting native phase" in " ".join(merged["reasons"])
    assert _read_json(tmp_path / "first.json") == first

    # Two individually well-formed but contradictory deliveries are not a
    # trustworthy new observation if there is no validated history.
    changed = tmp_path / "changed.jsonl"
    changed.write_text("\n".join(json.dumps(report if json.loads(line).get("event_kind") == "pytest_phase_report" and json.loads(line).get("pytest_phase") == "call" else json.loads(line), sort_keys=True) for line in raw_lines) + "\n", encoding="utf-8")
    changed_batch = _batch(tmp_path, [changed], name="changed")
    contradictory = _run_ok(context, tmp_path / "contradictory.json", batches=[batch, changed_batch])
    assert contradictory["status"] == "unavailable" and contradictory["nodes"] == {}
    assert "conflicting batch deliveries" in " ".join(contradictory["reasons"])


def test_native_origin_and_profile_mismatch_are_rejected_without_relabeling(tmp_path: Path) -> None:
    result, files = _run_pytest(tmp_path, "def test_original(): assert True\n")
    assert result.returncode == 0
    context = _context(tmp_path)
    batch = _batch(tmp_path, files)
    baseline = _run_ok(context, tmp_path / "baseline.json", batches=[batch])
    for key, invalid in (("checkout_sha", "a" * 40), ("invocation_id", "foreign")):
        content = _read_json(batch)
        content["origin"][key] = invalid
        foreign = tmp_path / f"{key}.json"
        _write_json(foreign, content)
        snapshot = _run_ok(context, tmp_path / f"{key}-profile.json", batches=[foreign], previous=[tmp_path / "baseline.json"])
        assert snapshot["status"] == "partial"
        assert snapshot["nodes"] == baseline["nodes"]
        assert key in " ".join(snapshot["reasons"])
    foreign_profile = _read_json(batch)
    foreign_profile["profile"]["profile_id"] = "another-profile"
    _write_json(tmp_path / "foreign-profile.json", foreign_profile)
    snapshot = _run_ok(context, tmp_path / "foreign-output.json", batches=[tmp_path / "foreign-profile.json"])
    assert snapshot["status"] == "unavailable" and snapshot["nodes"] == {}
    assert "foreign profile identity" in " ".join(snapshot["reasons"])
    invalid_context = _read_json(context)
    invalid_context["profile"]["python_major_minor"] = "not-python"
    _write_json(tmp_path / "invalid-context.json", invalid_context)
    result = _cli(tmp_path / "invalid-context.json", tmp_path / "must-not-exist.json", batches=[batch])
    assert result.returncode != 0 and not (tmp_path / "must-not-exist.json").exists()


def test_actions_native_origin_uses_real_checkout_not_event_sha_or_other_attempt(tmp_path: Path) -> None:
    result, files = _run_pytest(tmp_path, "def test_original(): assert True\n")
    assert result.returncode == 0
    observed = _records(files)
    checkout_sha = observed[0]["context"]["checkout_sha"]
    event_sha = "e" * 40 if checkout_sha != "e" * 40 else "f" * 40
    for record in observed:
        record["context"].update({"event_sha": event_sha, "workflow": "PR Tests", "run_id": "12345", "run_attempt": "2", "job_key": "test-shard"})
    native = tmp_path / "actions.jsonl"
    native.write_text("\n".join(json.dumps(record, sort_keys=True) for record in observed) + "\n", encoding="utf-8")
    context = _context(tmp_path)
    batch = _batch(tmp_path, [native], kind="github-actions")
    original = _run_ok(context, tmp_path / "actions-profile.json", batches=[batch])
    assert original["status"] == "available"
    sample = next(iter(original["nodes"].values()))["samples"][0]
    assert sample["origin"]["checkout_sha"] == checkout_sha
    assert sample["origin"]["event_sha"] == event_sha
    assert sample["origin"]["run_attempt"] == "2"

    wrong = _read_json(batch)
    wrong["origin"]["run_attempt"] = "3"
    mismatch = tmp_path / "wrong-attempt.json"
    _write_json(mismatch, wrong)
    rejected = _run_ok(context, tmp_path / "actions-merged.json", batches=[mismatch], previous=[tmp_path / "actions-profile.json"])
    assert rejected["status"] == "partial"
    assert rejected["nodes"] == original["nodes"]
    assert "run_attempt" in " ".join(rejected["reasons"])

    other_checkout = "c" * 40 if checkout_sha != "c" * 40 else "d" * 40
    for record in observed:
        record["context"]["checkout_sha"] = other_checkout
        record["context"]["invocation_id"] = "same-profile-older-source"
    historical_file = tmp_path / "other-checkout.jsonl"
    historical_file.write_text("\n".join(json.dumps(record, sort_keys=True) for record in observed) + "\n", encoding="utf-8")
    historical_batch = _batch(tmp_path, [historical_file], name="other-checkout", kind="github-actions")
    compatible = _run_ok(context, tmp_path / "compatible.json", batches=[historical_batch], previous=[tmp_path / "actions-profile.json"])
    entry = next(iter(compatible["nodes"].values()))
    assert compatible["status"] == "available"
    assert entry["sample_count"] == 2
    assert {sample["origin"]["checkout_sha"] for sample in entry["samples"]} == {checkout_sha, other_checkout}


def test_zero_native_sum_has_positive_floor_and_fourteen_day_boundary(tmp_path: Path) -> None:
    result, files = _run_pytest(tmp_path, "def test_original(): assert True\n")
    assert result.returncode == 0
    now = datetime.now(timezone.utc).replace(microsecond=0)
    observed = _records(files)
    for record in observed:
        if record["event_kind"] == "pytest_phase_report":
            record["native_duration_ms"] = 0.0
        if record["event_kind"] == "pytest_phase_start":
            ordinal = ("setup", "call", "teardown").index(record["pytest_phase"])
            record["started_at_utc"] = (now - timedelta(days=14) + timedelta(microseconds=ordinal)).isoformat()
    native = tmp_path / "zero.jsonl"
    native.write_text("\n".join(json.dumps(record, sort_keys=True) for record in observed) + "\n", encoding="utf-8")
    context = _context(tmp_path, now)
    snapshot = _run_ok(context, tmp_path / "zero-profile.json", batches=[_batch(tmp_path, [native])])
    sample = next(iter(snapshot["nodes"].values()))["samples"][0]
    assert sample["raw_duration_seconds"] == 0.0
    assert sample["duration_seconds"] == 0.001
    assert next(iter(snapshot["nodes"].values()))["p95_seconds"] == 0.001
    assert sample["phase_started_at"]["setup"] == sample["observed_at"]


def test_corrupt_history_and_atomic_output_failure_preserve_original_files(tmp_path: Path, monkeypatch) -> None:
    result, files = _run_pytest(tmp_path, "def test_original(): assert True\n")
    assert result.returncode == 0
    context = _context(tmp_path)
    batch = _batch(tmp_path, files)
    original = tmp_path / "prior.json"
    baseline = _run_ok(context, original, batches=[batch])
    before = original.read_bytes()
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text('{"schema_version": "ci-duration-profile-v1", "nodes": ', encoding="utf-8")
    snapshot = _run_ok(context, tmp_path / "new.json", previous=[original, corrupt])
    assert snapshot["status"] == "partial"
    assert snapshot["nodes"] == baseline["nodes"]
    assert original.read_bytes() == before
    assert "invalid JSON" in " ".join(snapshot["reasons"])

    oversized = _records(files)
    for record in oversized:
        if record["event_kind"] == "pytest_phase_report" and record["pytest_phase"] == "call":
            record["native_duration_ms"] = 10**400
    invalid_file = tmp_path / "oversized.jsonl"
    invalid_file.write_text("\n".join(json.dumps(record, sort_keys=True) for record in oversized) + "\n", encoding="utf-8")
    oversized_profile = _run_ok(context, tmp_path / "oversized-profile.json", batches=[_batch(tmp_path, [invalid_file], name="oversized")], previous=[original])
    assert oversized_profile["status"] == "partial"
    assert oversized_profile["nodes"] == baseline["nodes"]
    assert "finite nonnegative" in " ".join(oversized_profile["reasons"])

    occupied = tmp_path / "occupied"
    occupied.write_text("a file, not a directory", encoding="utf-8")
    failing = _cli(context, occupied / "profile.json", previous=[original])
    assert failing.returncode != 0 and "failed" in failing.stdout
    assert not (occupied / "profile.json").exists()
    assert original.read_bytes() == before
    assert _cli(context, original, batches=[batch]).returncode != 0
    assert original.read_bytes() == before

    context_model = ProfileContext.read(context)
    merged = build_profile(context_model, [batch], [])

    def link_unavailable(_source, _target):
        raise OSError("sentinel atomic link failure")

    monkeypatch.setattr("auto_coder.ci_duration_profile.os.link", link_unavailable)
    with pytest.raises(ProfileInputError, match="atomic output failed"):
        write_profile(tmp_path / "atomic.json", merged, [context, batch, original])
    assert not (tmp_path / "atomic.json").exists()
    assert list(tmp_path.glob(".atomic.json.*.tmp")) == []


def test_two_concurrent_immutable_publishers_have_one_winner(tmp_path: Path, monkeypatch) -> None:
    result, files = _run_pytest(tmp_path, "def test_observed(): assert True\n")
    assert result.returncode == 0
    context = _context(tmp_path)
    batch = _batch(tmp_path, files)
    prior = tmp_path / "immutable-prior.json"
    _run_ok(context, prior, batches=[batch])
    prior_bytes = prior.read_bytes()
    profile_context = ProfileContext.read(context)
    measured = build_profile(profile_context, [batch], [])
    empty = build_profile(profile_context, [], [])
    assert measured.payload["digest_sha256"] != empty.payload["digest_sha256"]

    actual_link = os.link
    barrier = threading.Barrier(2)

    def simultaneous_publish(source: str, destination: str) -> None:
        barrier.wait(timeout=10)
        actual_link(source, destination)

    monkeypatch.setattr("auto_coder.ci_duration_profile.os.link", simultaneous_publish)
    output = tmp_path / "contended.json"

    def publish(profile) -> str:
        try:
            write_profile(output, profile, [context, batch, prior])
            return "success"
        except ProfileInputError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (measured, empty)))
    assert results.count("success") == 1, results
    assert sum("atomic output failed" in outcome and "FileExistsError" in outcome for outcome in results) == 1, results
    winner_bytes = output.read_bytes()
    winner = json.loads(winner_bytes)
    assert winner in (measured.payload, empty.payload)
    assert winner_bytes == (json.dumps(winner, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    assert list(tmp_path.glob(".contended.json.*.tmp")) == []
    assert prior.read_bytes() == prior_bytes
    assert _cli(context, output, batches=[batch], previous=[prior]).returncode != 0
    assert output.read_bytes() == winner_bytes
    assert prior.read_bytes() == prior_bytes


def test_real_interrupt_and_skipped_call_remain_unavailable_not_measured_zero(tmp_path: Path) -> None:
    result, files = _run_pytest(tmp_path, "import pytest\n@pytest.mark.skip\ndef test_not_reached(): assert False\ndef test_interrupt(): raise KeyboardInterrupt('interrupt sentinel')\n")
    assert result.returncode not in (0, None)
    assert "KeyboardInterrupt" in result.stdout + result.stderr
    assert files
    snapshot = _run_ok(_context(tmp_path), tmp_path / "output.json", batches=[_batch(tmp_path, files)])
    assert snapshot["status"] == "unavailable"
    assert snapshot["nodes"] == {}
    assert any("incomplete" in reason or "unreached" in reason for reason in snapshot["reasons"])


def test_long_native_tail_retention_nearest_rank_p95_and_sample_identity(tmp_path: Path) -> None:
    result, files = _run_pytest(tmp_path, "def test_observed(): assert True\n")
    assert result.returncode == 0
    original = _records(files)
    evaluation = datetime.now(timezone.utc).replace(microsecond=0)
    context = _context(tmp_path, evaluation)
    manifests = []
    for index in range(24):
        # Derived from real pytest phases, with distinct native invocation/span IDs.
        timestamp = evaluation - timedelta(hours=index * 8)
        records = []
        for record in original:
            candidate = json.loads(json.dumps(record))
            candidate["context"]["invocation_id"] = f"historical-{index:02d}"
            candidate["context"]["process_id"] = f"process-{index:02d}"
            if candidate.get("span_id"):
                candidate["span_id"] = f"{candidate['span_id']}-{index}"
            if candidate["event_kind"] == "pytest_phase_start":
                phase_offset = ("setup", "call", "teardown").index(candidate["pytest_phase"])
                candidate["started_at_utc"] = (timestamp + timedelta(microseconds=phase_offset)).isoformat()
            if candidate["event_kind"] == "pytest_phase_report":
                candidate["native_duration_ms"] = 820_000.0 if index in (0, 1) and candidate["pytest_phase"] == "call" else 1_000.0 if candidate["pytest_phase"] == "call" else 0.0
            records.append(candidate)
        native_file = tmp_path / f"historical-{index}.jsonl"
        native_file.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")
        manifests.append(_batch(tmp_path, [native_file], name=f"batch-{index}"))
    snapshot = _run_ok(context, tmp_path / "duration-profile.json", batches=manifests)
    assert snapshot["status"] == "available"
    node = next(iter(snapshot["nodes"].values()))
    assert node["sample_count"] == 20
    assert node["p95_seconds"] == 820.0
    assert len({sample["sample_id"] for sample in node["samples"]}) == 20
    assert [sample["duration_seconds"] for sample in node["samples"]][-2:] == [820.0, 820.0]
    assert node["samples"][-1]["raw_duration_seconds"] == 820.0
    assert all(sample["origin"]["checkout_sha"] == original[0]["context"]["checkout_sha"] for sample in node["samples"])
    assert hashlib.sha256((json.dumps({key: value for key, value in snapshot.items() if key != "digest_sha256"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()).hexdigest() == snapshot["digest_sha256"]

    older = _context(tmp_path, evaluation + timedelta(days=15))
    stale = _run_ok(older, tmp_path / "stale.json", previous=[tmp_path / "duration-profile.json"])
    assert stale["status"] == "unavailable" and stale["nodes"] == {}
