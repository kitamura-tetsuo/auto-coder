"""Real pytest-cov provenance and fail-closed artifact regression tests."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml
from coverage import CoverageData
from scripts.summarize_test_impact import normalize, write_immutable

from auto_coder.automation_config import AutomationConfig
from auto_coder.ci_observation import CheckObservation, ObservationAvailability, WorkflowObservation
from auto_coder.github_ci_observer import observe_ci
from auto_coder.util.github_action import _check_github_actions_status, filter_actionable_github_checks
from tests.ci_impact_plugin import inventory_digest

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "owner/repo"
EVENT_SHA = "f" * 40
PROFILE_SELECTOR = "not browser and not opencode_live and not muse_live and not supervisor_integration"
pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip()


def _checkout(tmp_path: Path, body: str, sources: dict[str, str] | None = None) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for name in ("tests/ci_impact_plugin.py", "scripts/summarize_test_impact.py", "scripts/run_test_impact_observation.py"):
        destination = checkout / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    (checkout / "tests/__init__.py").write_text("", encoding="utf-8")
    (checkout / "src").mkdir()
    (checkout / "src/__init__.py").write_text("", encoding="utf-8")
    (checkout / "tests/test_observation.py").write_text(body, encoding="utf-8")
    (checkout / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    (checkout / "uv.lock").write_text("controlled lock fingerprint\n", encoding="utf-8")
    for name, content in (sources or {}).items():
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(checkout, "init", "-q")
    _git(checkout, "config", "user.name", "Test")
    _git(checkout, "config", "user.email", "test@example.invalid")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "Controlled source")
    return checkout


def _env(checkout: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(checkout),
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_SHA": EVENT_SHA,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ID": "517",
        "GITHUB_RUN_ATTEMPT": "3",
    }


def _run(checkout: Path, directory: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(checkout / "scripts/run_test_impact_observation.py"), "--directory", str(directory), "--repository", REPOSITORY, "--source-ref", "main"],
        cwd=checkout,
        capture_output=True,
        text=True,
        env=_env(checkout),
        timeout=timeout,
    )


def _case_dir(tmp_path: Path) -> Path:
    return tmp_path / "raw" / str(uuid4())


def test_real_parametrization_fixture_collection_provenance_and_actual_checkout(tmp_path: Path) -> None:
    checkout = _checkout(
        tmp_path,
        "import pytest\n"
        "from src import collection_only\n"
        "from src.alpha import alpha\n"
        "@pytest.fixture\n"
        "def owned():\n    return alpha(1)\n"
        "@pytest.mark.parametrize('case', ['one|two', 'three::four'])\n"
        "def test_alpha(owned, case):\n    assert alpha(owned) == 3\n"
        "def test_beta():\n    from src.beta import beta\n    assert beta() == 7\n",
        {
            "src/collection_only.py": "INITIALIZED = 100\n",
            "src/alpha.py": "def alpha(value):\n    return value + 1\n",
            "src/beta.py": "def beta():\n    return 7\n",
        },
    )
    directory = _case_dir(tmp_path)
    result = _run(checkout, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    report = normalize(directory, checkout)
    assert report["status"] == "available", report["errors"]
    native_collection = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", PROFILE_SELECTOR],
        cwd=checkout,
        capture_output=True,
        text=True,
        env=_env(checkout),
        timeout=30,
    )
    assert native_collection.returncode == 0, native_collection.stdout + native_collection.stderr
    assert [line for line in native_collection.stdout.splitlines() if line.startswith("tests/") and "::" in line] == [node["nodeid"] for node in report["nodes"]]
    assert report["identity"]["checkout_sha"] == _git(checkout, "rev-parse", "HEAD")
    assert report["identity"]["checkout_tree"] == _git(checkout, "rev-parse", "HEAD^{tree}")
    assert report["identity"]["event_sha"] == EVENT_SHA != report["identity"]["checkout_sha"]
    assert report["identity"]["run_attempt"] == "3"
    nodes = report["nodes"]
    assert len(nodes) == 3
    assert report["profile"]["node_inventory_digest"] == inventory_digest([node["nodeid"] for node in nodes])
    parameter_nodes = [node for node in nodes if "test_alpha[" in node["nodeid"]]
    assert len(parameter_nodes) == 2
    assert any("one|two" in node["nodeid"] for node in parameter_nodes)
    assert any("three::four" in node["nodeid"] for node in parameter_nodes)
    assert all("src/alpha.py" in node["phases"]["setup"]["coverage"]["files"] for node in parameter_nodes)
    assert all("src/alpha.py" in node["phases"]["call"]["coverage"]["files"] for node in parameter_nodes)
    beta = next(node for node in nodes if node["nodeid"].endswith("::test_beta"))
    assert "src/beta.py" in beta["phases"]["call"]["coverage"]["files"]
    assert "src/beta.py" not in parameter_nodes[0]["observed_files"]
    assert "src/collection_only.py" in report["unattributed_execution"]["files"]
    assert all("src/collection_only.py" not in node["observed_files"] for node in nodes)
    assert all(node["phases"]["call"]["coverage"]["state"] == "measured" for node in nodes)
    assert report["native_session"] == {"exit_code": 0, "pytest_finalization_exit": 0, "started": True, "finalized": True}
    output = directory / "observation.json"
    write_immutable(output, report)
    assert json.loads(output.read_text(encoding="utf-8"))["profile"] == report["profile"]
    with pytest.raises(FileExistsError):
        write_immutable(output, {"status": "forged"})
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "available"


def test_native_failure_skip_xfail_xpass_teardown_error_and_unreached_call(tmp_path: Path) -> None:
    checkout = _checkout(
        tmp_path,
        "import pytest\n"
        "@pytest.fixture\ndef broken_teardown():\n    yield\n    raise RuntimeError('teardown sentinel')\n"
        "@pytest.fixture\ndef broken_setup():\n    raise RuntimeError('setup sentinel')\n"
        "def test_fail():\n    assert False, 'native failure'\n"
        "def test_teardown(broken_teardown):\n    assert True\n"
        "def test_setup_error(broken_setup):\n    assert True\n"
        "@pytest.mark.skip(reason='intentional')\ndef test_skip():\n    assert False\n"
        "@pytest.mark.xfail(reason='expected')\ndef test_xfail():\n    assert False\n"
        "@pytest.mark.xfail(reason='non-strict')\ndef test_xpass():\n    assert True\n",
    )
    directory = _case_dir(tmp_path)
    result = _run(checkout, directory)
    assert result.returncode == 1, result.stdout + result.stderr
    native = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", PROFILE_SELECTOR],
        cwd=checkout,
        capture_output=True,
        text=True,
        env=_env(checkout),
        timeout=30,
    )
    assert native.returncode == result.returncode
    assert all(sentinel in result.stdout for sentinel in ("native failure", "teardown sentinel", "setup sentinel"))
    report = normalize(directory, checkout)
    assert report["native_session"]["exit_code"] == 1
    nodes = {node["nodeid"].split("::")[-1]: node for node in report["nodes"]}
    assert nodes["test_fail"]["phases"]["call"]["outcome"] == "failed"
    assert nodes["test_teardown"]["phases"]["teardown"]["outcome"] == "failed"
    assert nodes["test_setup_error"]["phases"]["setup"]["outcome"] == "failed"
    assert nodes["test_setup_error"]["phases"]["call"]["state"] == "unreached"
    assert nodes["test_skip"]["phases"]["setup"]["outcome"] == "skipped"
    assert nodes["test_skip"]["phases"]["call"]["state"] == "unreached"
    assert nodes["test_xfail"]["phases"]["call"]["wasxfail"] == "expected"
    assert nodes["test_xpass"]["phases"]["call"]["wasxfail"] == "non-strict"
    assert report["status"] == "available", report["errors"]
    assert nodes["test_skip"]["phases"]["setup"]["coverage"]["state"] == "no_evidence"
    assert all(node["unmeasured_domains"] for node in nodes.values())


def test_real_child_process_is_explicitly_unmeasured(tmp_path: Path) -> None:
    checkout = _checkout(
        tmp_path,
        "import subprocess,sys\n" "def test_child():\n" "    subprocess.run([sys.executable, '-c', 'from src.child_only import child; child()'], check=True)\n",
        {"src/child_only.py": "def child():\n    return 99\n"},
    )
    directory = _case_dir(tmp_path)
    result = _run(checkout, directory)
    assert result.returncode == 0, result.stdout + result.stderr
    report = normalize(directory, checkout)
    assert report["status"] == "available", report["errors"]
    assert "src/child_only.py" not in report["nodes"][0]["observed_files"]
    assert all("src/child_only.py" not in node["observed_files"] for node in report["nodes"])
    assert "subprocess" in report["unmeasured_domains"]


def test_interrupted_real_call_keeps_partial_evidence_without_success(tmp_path: Path) -> None:
    checkout = _checkout(
        tmp_path,
        "import time\n" "def test_never_finishes():\n" "    while True:\n        time.sleep(0.1)\n",
    )
    directory = _case_dir(tmp_path)
    process = subprocess.Popen(
        [sys.executable, str(checkout / "scripts/run_test_impact_observation.py"), "--directory", str(directory), "--repository", REPOSITORY, "--source-ref", "main"],
        cwd=checkout,
        env=_env(checkout),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    observed = False
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            files = list(directory.glob("pytest-events-*.jsonl"))
            if files and '"kind": "phase_start"' in files[0].read_text(encoding="utf-8") and '"phase": "call"' in files[0].read_text(encoding="utf-8"):
                observed = True
                break
            time.sleep(0.05)
        assert observed, "real pytest call never began"
        os.killpg(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode not in (None, 0), stdout + stderr
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
    report = normalize(directory, checkout)
    assert report["status"] != "available"
    assert report["errors"]
    assert any("call" in str(node["phases"]) for node in report["nodes"])


def test_corrupt_context_conflicting_events_and_diagnostic_sink_do_not_forge_availability(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "def test_ok():\n    assert True\n")
    directory = _case_dir(tmp_path)
    assert _run(checkout, directory).returncode == 0
    event_file = next(directory.glob("pytest-events-*.jsonl"))
    data = event_file.read_text(encoding="utf-8")
    event_file.write_text(data + data.splitlines()[0] + "\n", encoding="utf-8")
    assert normalize(directory, checkout)["status"] == "incomplete"
    event_file.write_text(data, encoding="utf-8")
    for kind in ("collection", "session_finish"):
        event_file.write_text("\n".join(line for line in data.splitlines() if json.loads(line)["kind"] != kind) + "\n", encoding="utf-8")
        assert normalize(directory, checkout)["status"] == "incomplete"
    event_file.write_text(data, encoding="utf-8")
    coverage_data = CoverageData(basename=str(directory / "coverage.db"))
    coverage_data.read()
    coverage_data.set_context("foreign-node|run")
    coverage_data.add_lines({str(checkout / "tests/test_observation.py"): {999}})
    coverage_data.write()
    unknown = normalize(directory, checkout)
    assert unknown["status"] == "incomplete"
    assert any("unknown coverage context" in error for error in unknown["errors"])
    (directory / "coverage.db").write_bytes(b"not a coverage database")
    corrupted = normalize(directory, checkout)
    assert corrupted["status"] == "incomplete"
    assert any("corrupt" in error for error in corrupted["errors"])

    blocked = tmp_path / "occupied"
    blocked.write_text("a file, not a diagnostic directory", encoding="utf-8")
    without_diagnostic = _run(checkout, blocked)
    assert without_diagnostic.returncode == 0, without_diagnostic.stdout + without_diagnostic.stderr
    assert "diagnostic" in without_diagnostic.stdout.lower() + without_diagnostic.stderr.lower()
    assert normalize(blocked, checkout)["status"] == "unavailable"


def test_replaced_checkout_and_changed_tracked_source_cannot_reuse_observation(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "def test_ok():\n    assert True\n")
    directory = _case_dir(tmp_path)
    assert _run(checkout, directory).returncode == 0
    assert normalize(directory, checkout)["status"] == "available"
    (checkout / "tests/test_observation.py").write_text("def test_ok():\n    assert False\n", encoding="utf-8")
    dirty = normalize(directory, checkout)
    assert dirty["status"] == "incomplete"
    assert any("tracked Python source changed" in error for error in dirty["errors"])
    _git(checkout, "add", "tests/test_observation.py")
    _git(checkout, "commit", "-qm", "Post-observation revision")
    replaced = normalize(directory, checkout)
    assert replaced["status"] == "incomplete"
    assert any("checkout commit/tree differs" in error for error in replaced["errors"])


def test_workflow_normalizer_cli_from_clean_committed_checkout(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "def test_ok():\n    assert True\n")
    directory = _case_dir(tmp_path)
    native = _run(checkout, directory)
    assert native.returncode == 0, native.stdout + native.stderr

    workflow = yaml.safe_load((ROOT / ".github/workflows/test-impact-observation.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["observe"]["steps"]
    normalizer_step = next(step for step in steps if step["name"] == "Normalize independent diagnostic result")
    assert "uv run python -m scripts.summarize_test_impact" in normalizer_step["run"]

    # Match the workflow's Python module entrypoint, without inherited root PYTHONPATH.
    command = [
        sys.executable,
        "-m",
        "scripts.summarize_test_impact",
        "--directory",
        str(directory),
        "--checkout",
        str(checkout),
        "--output",
        str(directory / "observation.json"),
    ]
    clean_env = _env(checkout)
    clean_env.pop("PYTHONPATH", None)
    finalized = subprocess.run(command, cwd=checkout, env=clean_env, capture_output=True, text=True, timeout=30)
    assert finalized.returncode == 0, finalized.stdout + finalized.stderr
    output = directory / "observation.json"
    available = json.loads(output.read_text(encoding="utf-8"))
    assert available["status"] == "available"
    assert available["identity"]["checkout_sha"] == _git(checkout, "rev-parse", "HEAD")
    assert available["native_session"]["exit_code"] == 0

    event_file = next(directory.glob("pytest-events-*.jsonl"))
    event_file.write_text("not JSON\n", encoding="utf-8")
    broken_output = directory / "observation-corrupt.json"
    broken = subprocess.run(
        [*command[:-1], str(broken_output)],
        cwd=checkout,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert broken.returncode == 2, broken.stdout + broken.stderr
    incomplete = json.loads(broken_output.read_text(encoding="utf-8"))
    assert incomplete["status"] == "incomplete"
    assert incomplete["native_session"]["exit_code"] == 0
    assert any("pytest-events-" in error for error in incomplete["errors"])
    assert json.loads(output.read_text(encoding="utf-8")) == available


def test_workflow_is_manual_only_complete_and_not_a_required_check() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test-impact-observation.yml").read_text(encoding="utf-8"))
    assert list(workflow["on"]) == ["workflow_dispatch"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["source_ref"]["required"] is True
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    steps = workflow["jobs"]["observe"]["steps"]
    assert next(step for step in steps if step["name"] == "Checkout explicitly requested source")["with"]["ref"] == "${{ inputs.source_ref }}"
    assert any("--locked --extra test" in step.get("run", "") for step in steps)
    assert any("run_test_impact_observation.py" in step.get("run", "") for step in steps)
    assert any("uv run python -m scripts.summarize_test_impact" in step.get("run", "") for step in steps)
    assert "--cov-context=test" in (ROOT / "scripts/run_test_impact_observation.py").read_text(encoding="utf-8")
    assert PROFILE_SELECTOR in (ROOT / "scripts/run_test_impact_observation.py").read_text(encoding="utf-8")
    assert "--cov-context" not in (ROOT / ".github/workflows/pr-tests.yml").read_text(encoding="utf-8")
    assert "ci_impact_plugin" not in (ROOT / "tests/conftest.py").read_text(encoding="utf-8")


def _workflow(run_id: int, path: str, head: str = "a" * 40, status: str = "completed", conclusion: str = "failure") -> dict[str, object]:
    return {"id": run_id, "workflow_id": 22, "run_attempt": 1, "head_sha": head, "path": path, "name": "identical display name", "status": status, "conclusion": conclusion, "repository": {"full_name": REPOSITORY}}


def _check(check_id: int, run_id: int | None, head: str = "a" * 40, repository: str = REPOSITORY) -> dict[str, object]:
    return {"id": check_id, "app": {"id": 5}, "head_sha": head, "name": "identical display name", "status": "completed", "conclusion": "failure", "details_url": f"https://github.com/{repository}/actions/runs/{run_id}/job/{check_id}" if run_id else ""}


@pytest.mark.parametrize("state,conclusion", [("completed", "failure"), ("queued", ""), ("in_progress", "")])
def test_real_observe_ci_and_aggregation_ignore_only_verified_diagnostic_run_and_job(state: str, conclusion: str) -> None:
    ordinary = _workflow(20, ".github/workflows/pr-tests.yml", conclusion="success")
    diagnostic = _workflow(30, ".github/workflows/test-impact-observation.yml@refs/heads/main", status=state, conclusion=conclusion)
    api = MagicMock()
    api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": [ordinary, diagnostic]}
    api.checks.list_for_ref.return_value = {"check_runs": [_check(31, 30), _check(21, 20)]}
    api.actions.get_workflow_run.side_effect = lambda owner, repo, run_id: diagnostic if run_id == 30 else ordinary

    snapshot = observe_ci(api, "token", REPOSITORY, 17, "a" * 40)
    assert snapshot.availability is ObservationAvailability.KNOWN
    assert [fact.execution.run_id for fact in snapshot.facts if isinstance(fact, WorkflowObservation)] == ["20"]
    assert [fact.execution.check_id for fact in snapshot.facts if isinstance(fact, CheckObservation)] == ["21"]

    with patch("auto_coder.util.github_action.GitHubClient") as client, patch("auto_coder.util.github_action.get_ghapi_client", return_value=api):
        client.get_instance.return_value.token = "token"
        result = _check_github_actions_status(REPOSITORY, {"number": 17, "head": {"sha": "a" * 40}}, AutomationConfig())
        assert result.ids == [20]
        assert result.success is False  # The ordinary, identically named failed check still blocks.
        remaining = filter_actionable_github_checks(REPOSITORY, [_check(31, 30), _check(21, 20), _check(32, 30, repository="foreign/repo"), _check(33, None)])
        assert [check["id"] for check in remaining] == [21, 32, 33]
        spoofed_direct = {**_check(34, None), "path": ".github/workflows/test-impact-observation.yml"}
        assert filter_actionable_github_checks(REPOSITORY, [spoofed_direct]) == [spoofed_direct]


def test_foreign_and_unknown_check_provenance_stays_fail_closed() -> None:
    diagnostic = _workflow(30, ".github/workflows/test-impact-observation.yml")
    api = MagicMock()
    api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": [diagnostic]}
    api.checks.list_for_ref.return_value = {"check_runs": [_check(1, 30, repository="foreign/repo"), _check(2, None)]}
    snapshot = observe_ci(api, "token", REPOSITORY, 18, "a" * 40)
    assert snapshot.availability is ObservationAvailability.KNOWN
    assert len(snapshot.facts) == 2
    assert all(isinstance(fact, CheckObservation) for fact in snapshot.facts)
