"""Real Git histories, native F observer artifacts and production shadow CLI."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from scripts.report_test_impact import (
    EvidenceError,
    _validate_downloaded_artifact,
    _verify_remote_attempt,
    compare,
    parse_name_status,
)
from scripts.summarize_test_impact import normalize

from tests.test_ci_impact_evidence import ROOT, _checkout, _env, _git

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")

MODULE_A = "src/auto_coder/alpha.py"
MODULE_B = "src/auto_coder/beta.py"
TESTS = "def test_a():\n" "    from src.auto_coder.alpha import touch\n" "    assert touch() == 5\n" "\n" "def test_b():\n" "    from src.auto_coder.beta import beta\n" "    assert beta() == 2\n"
SOURCES = {
    MODULE_A: "FLAG = 0\ndef touch():\n    return 5\n",
    MODULE_B: "def beta():\n    from src.auto_coder import alpha\n    return alpha.touch() + 1 if alpha.FLAG else 2\n",
}


def _fixture(tmp_path: Path) -> Path:
    return _checkout(tmp_path, TESTS, SOURCES)


def _commit(checkout: Path, path: str, content: str | None, message: str = "Change measured history") -> None:
    target = checkout / path
    if content is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(checkout, "add", "-A", "--", path)
    _git(checkout, "commit", "-qm", message)


def _observe(checkout: Path, tmp_path: Path, run_id: int, *, expected_exit: int = 0) -> dict[str, object]:
    directory = tmp_path / "raw" / str(uuid4())
    environment = _env(checkout)
    environment["GITHUB_RUN_ID"] = str(run_id)
    result = subprocess.run(
        [
            sys.executable,
            str(checkout / "scripts/run_test_impact_observation.py"),
            "--directory",
            str(directory),
            "--repository",
            "owner/repo",
            "--source-ref",
            _git(checkout, "rev-parse", "HEAD"),
        ],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        timeout=50,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    report = normalize(directory, checkout)
    assert report["status"] == "available", report["errors"]
    return report


def _report(baseline: dict[str, object], current: dict[str, object], checkout: Path) -> dict[str, object]:
    return compare(baseline, current, checkout, "owner/repo", 511, 3)


def test_production_cli_freezes_only_old_edges_and_finds_real_omitted_failure(tmp_path: Path) -> None:
    checkout = _fixture(tmp_path)
    baseline = _observe(checkout, tmp_path, 511)
    alpha = next(node for node in baseline["nodes"] if node["nodeid"].endswith("test_a"))
    beta = next(node for node in baseline["nodes"] if node["nodeid"].endswith("test_b"))
    assert MODULE_A in alpha["observed_files"]
    assert MODULE_A not in beta["observed_files"]
    assert MODULE_A not in baseline["unattributed_execution"]["files"]

    _commit(checkout, MODULE_A, "FLAG = 1\ndef touch():\n    return 5\n# changed revision\n")
    current = _observe(checkout, tmp_path, 512, expected_exit=1)
    # The current run now observes B executing A, but that edge cannot repair
    # an omission already frozen from the historical baseline.
    assert MODULE_A in next(node for node in current["nodes"] if node["nodeid"].endswith("test_b"))["observed_files"]
    report = _report(baseline, current, checkout)
    assert report["comparison_status"] == "candidate", report["reasons"]
    assert report["selection_mode"] == "BASELINE_CANDIDATE"
    assert report["evidence"]["baseline_status"] == report["evidence"]["current_status"] == "available"
    assert report["evidence"]["current_native_session"]["exit_code"] == 1
    assert report["candidate_nodes"] == [alpha["nodeid"]]
    assert report["omitted_nodes"] == [beta["nodeid"]]
    assert report["validation_status"] == "missed_failure"
    assert report["missed_failures"] == [{"nodeid": beta["nodeid"], "failed_phases": ["call"], "classification": "MISSED_FAILURE"}]
    assert report["changed_paths"] == [{"status": "M", "path": MODULE_A, "old_path": None}]
    assert report["phase_time_estimate"]["full_ms"] >= report["phase_time_estimate"]["selected_ms"]

    artifacts = tmp_path / "comparison"
    artifacts.mkdir()
    baseline_path = artifacts / "baseline.json"
    current_path = artifacts / "current.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    current_path.write_text(json.dumps(current), encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "scripts.report_test_impact",
        "--baseline",
        str(baseline_path),
        "--current",
        str(current_path),
        "--baseline-run-id",
        "511",
        "--baseline-run-attempt",
        "3",
        "--repository",
        "owner/repo",
        "--repository-dir",
        str(checkout),
        "--output",
        str(artifacts / "report.json"),
        "--summary",
        str(artifacts / "summary.md"),
    ]
    cli = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert json.loads((artifacts / "report.json").read_text(encoding="utf-8")) == report
    assert "MISSED_FAILURE" in (artifacts / "summary.md").read_text(encoding="utf-8")
    assert (artifacts / "report.log").exists()
    bad_command = cmd.copy()
    bad_command[bad_command.index("511")] = "not-an-id"
    bad_command[-3] = str(artifacts / "bad-report.json")
    bad_command[-1] = str(artifacts / "bad-summary.md")
    bad = subprocess.run(bad_command, cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert bad.returncode == 0, bad.stdout + bad.stderr
    assert json.loads((artifacts / "bad-report.json").read_text(encoding="utf-8"))["comparison_status"] == "unavailable"


def test_empty_verified_diff_is_distinct_from_missing_evidence(tmp_path: Path) -> None:
    checkout = _fixture(tmp_path)
    baseline = _observe(checkout, tmp_path, 511)
    current = _observe(checkout, tmp_path, 512)
    report = _report(baseline, current, checkout)
    assert report["comparison_status"] == "candidate"
    assert report["changed_paths"] == []
    assert report["candidate_nodes"] == []
    assert len(report["omitted_nodes"]) == 2
    assert report["validation_status"] == "no_observed_miss"
    assert report["empirical_not_soundness_guarantee"] is True
    assert current["native_session"]["exit_code"] == baseline["native_session"]["exit_code"] == 0


def test_changed_and_new_parametrized_test_ids_and_removed_history(tmp_path: Path) -> None:
    checkout = _checkout(
        tmp_path,
        "import pytest\n" "@pytest.mark.parametrize('value', [1,2])\n" "def test_params(value):\n    assert value > 0\n" "def test_stable():\n    assert True\n",
    )
    baseline = _observe(checkout, tmp_path, 511)
    _commit(
        checkout,
        "tests/test_observation.py",
        "import pytest\n" "@pytest.mark.parametrize('value', [2,3])\n" "def test_params(value):\n    assert value > 0\n" "def test_stable():\n    assert True\n",
    )
    _commit(checkout, "tests/test_new.py", "def test_added():\n    assert True\n")
    current = _observe(checkout, tmp_path, 512)
    report = _report(baseline, current, checkout)
    assert report["comparison_status"] == "candidate"
    assert len(report["candidate_nodes"]) == 4
    assert len(report["new_nodes"]) == 2
    assert report["removed_nodes"] == ["tests/test_observation.py::test_params[1]"]
    assert any("test_params[3]" in name for name in report["candidate_nodes"])
    assert report["omitted_nodes"] == []


@pytest.mark.parametrize(
    ("kind", "path", "content", "reason"),
    [
        ("add", "src/auto_coder/unmeasured.py", "VALUE = 1\n", "source_path_status"),
        ("delete", MODULE_A, None, "source_path_status"),
        ("conftest", "tests/conftest.py", "import pytest\n", "unmeasured_or_global_change"),
        ("resource", "data/settings.json", '{"value":1}\n', "unmeasured_or_global_change"),
        ("lock", "uv.lock", "different fingerprint\n", "measurement configuration differs"),
        ("config", "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-q"\n', "measurement configuration differs"),
    ],
)
def test_unmeasured_and_global_changes_fallback_to_full(tmp_path: Path, kind: str, path: str, content: str | None, reason: str) -> None:
    checkout = _fixture(tmp_path)
    baseline = _observe(checkout, tmp_path, 511)
    _commit(checkout, path, content)
    current = _observe(checkout, tmp_path, 512, expected_exit=1 if kind == "delete" else 0)
    report = _report(baseline, current, checkout)
    assert report["selection_mode"] == "FULL"
    assert report["comparison_status"] in {"full", "unavailable"}
    assert set(report["candidate_nodes"]) == {node["nodeid"] for node in current["nodes"]}
    assert report["omitted_nodes"] == []
    assert report["validation_status"] == "not_validated"
    assert any(reason in item for item in report["reasons"])


def test_renamed_source_records_both_old_and_new_paths_and_forces_full(tmp_path: Path) -> None:
    checkout = _fixture(tmp_path)
    baseline = _observe(checkout, tmp_path, 511)
    _git(checkout, "mv", MODULE_A, "src/auto_coder/renamed.py")
    _git(checkout, "commit", "-qm", "Rename source")
    current = _observe(checkout, tmp_path, 512, expected_exit=1)
    report = _report(baseline, current, checkout)
    assert report["comparison_status"] == "full"
    assert report["changed_paths"] == [{"status": "R100", "path": "src/auto_coder/renamed.py", "old_path": MODULE_A}]
    assert any("source_path_status" in reason for reason in report["reasons"])


def test_unmeasured_source_and_unattributed_mapping_force_full(tmp_path: Path) -> None:
    checkout = _checkout(
        tmp_path,
        "from src.auto_coder.alpha import FLAG\n" "def test_only():\n    assert FLAG == 0\n",
        {MODULE_A: "FLAG = 0\n", MODULE_B: "def beta():\n    return 7\n"},
    )
    baseline = _observe(checkout, tmp_path, 511)
    assert MODULE_A in baseline["unattributed_execution"]["files"]
    _commit(checkout, MODULE_A, "FLAG = 0\n# touched\n")
    current = _observe(checkout, tmp_path, 512)
    report = _report(baseline, current, checkout)
    assert report["comparison_status"] == "full"
    assert any("source_has_unattributed_execution" in reason for reason in report["reasons"])
    _commit(checkout, MODULE_B, "def beta():\n    return 8\n")
    third = _observe(checkout, tmp_path, 513)
    unmapped_report = compare(current, third, checkout, "owner/repo", 512, 3)
    assert unmapped_report["comparison_status"] == "full"
    assert any("source_unmapped" in reason for reason in unmapped_report["reasons"])


def test_native_skip_and_xfail_are_retained_without_invented_passes(tmp_path: Path) -> None:
    checkout = _checkout(
        tmp_path,
        "import pytest\n" "def test_measured():\n    from src.auto_coder.alpha import touch\n    assert touch() == 5\n" "@pytest.mark.xfail(reason='expected')\n" "def test_xfail():\n    assert False\n" "@pytest.mark.skip(reason='skip')\n" "def test_skip():\n    assert False\n",
        {MODULE_A: SOURCES[MODULE_A]},
    )
    baseline = _observe(checkout, tmp_path, 511)
    _commit(checkout, MODULE_A, SOURCES[MODULE_A] + "# changed Python code\n")
    current = _observe(checkout, tmp_path, 512)
    report = _report(baseline, current, checkout)
    assert report["comparison_status"] == "candidate"
    assert report["validation_status"] == "no_observed_miss"
    assert any(node.endswith("test_skip") for node in report["candidate_nodes"])
    assert len(report["omitted_native_outcomes"]) == 1
    xfail = next(record for record in report["omitted_native_outcomes"] if record["nodeid"].endswith("test_xfail"))
    skipped = next(node for node in current["nodes"] if node["nodeid"].endswith("test_skip"))
    assert any(phase["wasxfail"] == "expected" for phase in xfail["phases"])
    assert skipped["phases"]["setup"]["outcome"] == "skipped"
    assert skipped["phases"]["call"]["state"] == "unreached"


def test_downloaded_raw_artifact_must_remain_consistent(tmp_path: Path) -> None:
    checkout = _fixture(tmp_path)
    baseline = _observe(checkout, tmp_path, 511)
    directory = next((tmp_path / "raw").iterdir())
    output = directory / "observation.json"
    output.write_text(json.dumps(baseline), encoding="utf-8")
    _validate_downloaded_artifact(output, baseline)
    event_file = next(directory.glob("pytest-events-*.jsonl"))
    original = event_file.read_bytes()
    event_file.write_bytes(b"broken JSON\n")
    with pytest.raises(EvidenceError, match="corrupt"):
        _validate_downloaded_artifact(output, baseline)
    event_file.write_bytes(original)
    (directory / "coverage.db").write_bytes(b"corrupt coverage database")
    with pytest.raises(EvidenceError, match="corrupt"):
        _validate_downloaded_artifact(output, baseline)


def test_mixed_attempt_and_corrupt_or_incomplete_evidence_are_unavailable(tmp_path: Path) -> None:
    checkout = _fixture(tmp_path)
    baseline = _observe(checkout, tmp_path, 511)
    current = _observe(checkout, tmp_path, 512)
    cases = [
        compare(baseline, current, checkout, "owner/repo", 511, 2),
        compare({**baseline, "status": "incomplete"}, current, checkout),
        compare({**baseline, "tooling": {"versions": {}}}, current, checkout),
        compare(None, current, checkout, external_errors=["expired artifact"]),
        compare(baseline, {**current, "status": "incomplete"}, checkout),
        compare({**baseline, "identity": {**baseline["identity"], "repository": "foreign/repo"}}, current, checkout),
    ]
    assert all(item["comparison_status"] == "unavailable" for item in cases)
    assert all(item["selection_mode"] == "FULL" for item in cases)
    assert all(len(item["candidate_nodes"]) == 2 for item in cases)
    assert all(item["validation_status"] == "not_validated" for item in cases)
    invalid_current = compare(baseline, {**current, "profile": None}, checkout)
    assert invalid_current["selection_mode"] == "UNKNOWN"
    assert invalid_current["candidate_nodes"] == []
    assert invalid_current["complete_profile_required"] is True


def test_baseline_nonancestor_is_not_treated_as_a_partial_diff(tmp_path: Path) -> None:
    checkout = _fixture(tmp_path)
    common = _git(checkout, "rev-parse", "HEAD")
    _commit(checkout, MODULE_A, "FLAG = 0\ndef touch():\n    return 5\n# baseline\n")
    baseline = _observe(checkout, tmp_path, 511)
    _git(checkout, "checkout", "-qb", "diverged", common)
    _commit(checkout, MODULE_B, SOURCES[MODULE_B] + "# current\n")
    current = _observe(checkout, tmp_path, 512)
    report = _report(baseline, current, checkout)
    assert report["comparison_status"] == "unavailable"
    assert any("not a proven ancestor" in reason for reason in report["reasons"])
    assert report["changed_paths"] is None


def test_real_interruption_cannot_produce_zero_misses(tmp_path: Path) -> None:
    checkout = _fixture(tmp_path)
    baseline = _observe(checkout, tmp_path, 511)
    _commit(checkout, "tests/test_observation.py", "import time\ndef test_hangs():\n    while True:\n        time.sleep(0.1)\n")
    directory = tmp_path / "raw" / str(uuid4())
    command = [
        sys.executable,
        str(checkout / "scripts/run_test_impact_observation.py"),
        "--directory",
        str(directory),
        "--repository",
        "owner/repo",
        "--source-ref",
        "main",
    ]
    process = subprocess.Popen(
        command,
        cwd=checkout,
        env=_env(checkout),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            events = list(directory.glob("pytest-events-*.jsonl"))
            if events and '"kind": "phase_start"' in events[0].read_text(encoding="utf-8") and '"phase": "call"' in events[0].read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        else:
            pytest.fail("native call did not begin")
        os.killpg(process.pid, signal.SIGINT)
        process.communicate(timeout=12)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=12)
    current = normalize(directory, checkout)
    assert current["status"] == "incomplete"
    report = _report(baseline, current, checkout)
    assert report["comparison_status"] == "unavailable"
    assert report["validation_status"] == "not_validated"
    assert report["missed_failures"] == []


def test_git_parser_rejects_truncation_and_unknown_status_and_preserves_unicode() -> None:
    assert parse_name_status(b"") == []
    result = parse_name_status("R100\0old name.py\0new name.py\0M\0tests/test_é.py\0".encode("utf-8"))
    assert result[0].old_path == "old name.py"
    assert result[1].path == "tests/test_é.py"
    for broken in (b"M\0src/x.py", b"R100\0old\0", b"?\0bad\0", b"M\0\0", b"M\0\xff\0"):
        with pytest.raises(EvidenceError):
            parse_name_status(broken)


def test_exact_attempt_artifact_discovery_refuses_ambiguity(tmp_path: Path) -> None:
    from scripts.report_test_impact import _baseline_path

    with pytest.raises(EvidenceError, match="found 0"):
        _baseline_path(tmp_path)
    for name in ("one", "two"):
        subdir = tmp_path / name
        subdir.mkdir()
        (subdir / "observation.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceError, match="found 2"):
        _baseline_path(tmp_path)
    for child in tmp_path.iterdir():
        (child / "observation.json").unlink()
    flat = tmp_path / "observation.json"
    flat.write_text("{}", encoding="utf-8")
    assert _baseline_path(tmp_path) == flat
    (tmp_path / "one" / "observation.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceError, match="found 2"):
        _baseline_path(tmp_path)


def test_remote_attempt_validation_requires_exact_successful_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    import auto_coder.util.gh_cache as gh_cache

    payload = {
        "id": 511,
        "run_attempt": 3,
        "repository": {"full_name": "owner/repo"},
        "path": ".github/workflows/test-impact-observation.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
    }

    class Client:
        def __init__(self, token: str, repository: str) -> None:
            assert token == "a-token"
            assert repository == "owner/repo"

        def request(self, method: str, path: str) -> object:
            assert method == "GET"
            assert path == "actions/runs/511/attempts/3"
            from auto_coder.util.gh_cache import GitDataResponse

            return GitDataResponse(200, payload)

    monkeypatch.setattr(gh_cache, "GitHubGitDataClient", Client)
    _verify_remote_attempt("owner/repo", 511, 3, "a-token")
    for field, value in (("run_attempt", 4), ("path", ".github/workflows/pr-tests.yml"), ("conclusion", "failure"), ("repository", {"full_name": "foreign/repo"})):
        original = payload[field]
        payload[field] = value
        with pytest.raises(EvidenceError):
            _verify_remote_attempt("owner/repo", 511, 3, "a-token")
        payload[field] = original


def test_workflow_shadow_dispatch_preserves_full_native_and_ci_authority() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test-impact-observation.yml").read_text(encoding="utf-8"))
    assert list(workflow["on"]) == ["workflow_dispatch"]
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["source_ref"]["required"] is True
    assert inputs["baseline_run_id"]["required"] is False
    assert inputs["baseline_run_attempt"]["required"] is False
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    steps = workflow["jobs"]["observe"]["steps"]
    native_index = next(i for i, step in enumerate(steps) if step.get("id") == "native")
    compare_index = next(i for i, step in enumerate(steps) if step["name"] == "Compare baseline candidates after the full native run")
    assert native_index < compare_index
    assert "--cov-context=test" in (ROOT / "scripts/run_test_impact_observation.py").read_text(encoding="utf-8")
    assert "scripts.report_test_impact" in steps[compare_index]["run"]
    download = next(step for step in steps if step["name"] == "Download exact baseline run and attempt evidence")
    preflight_index = next(i for i, step in enumerate(steps) if step.get("id") == "baseline_provenance")
    assert native_index < preflight_index < steps.index(download) < compare_index
    assert "steps.baseline_provenance.outcome == 'success'" in download["if"]
    assert download["with"]["run-id"] == "${{ inputs.baseline_run_id }}"
    assert "${{ inputs.baseline_run_attempt }}" in download["with"]["pattern"]
    assert download["continue-on-error"] is True
    assert steps[-1]["uses"].startswith("actions/upload-artifact@")
    for path in ("pr-tests.yml", "browser-tests.yml", "opencode-live-tests.yml"):
        assert "report_test_impact" not in (ROOT / ".github/workflows" / path).read_text(encoding="utf-8")
    script = (ROOT / "scripts/report_test_impact.py").read_text(encoding="utf-8")
    assert 'subprocess.run(["gh"' not in script
    assert "pytest -k" not in script
