"""Adversarial native-evidence and exact-profile Supervisor shard regressions."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from supervisor_aggregate_evidence import REQUIRED_GROUPS, evaluate  # noqa: E402
from supervisor_ci_profiles import CONTAINER_REQUIRED, FRESH_REQUIRED, HARNESS, PROFILES, REPOSITORY, SEMANTIC_ENV, check_plan, profile_args  # noqa: E402
from supervisor_native_evidence import GitDataResponse, NativeEvidenceError, _inventory_ok, native_job_name, original_producer  # noqa: E402

from auto_coder.ci_shard_plan import ShardContext, VerifiedPlan, _canonical, build_plan  # noqa: E402
from auto_coder.util.gh_cache import GitHubGitDataClient  # noqa: E402

SHA = "a" * 40
HEAD_SHA = "b" * 40  # The PR API head is deliberately not the tested synthetic merge.
RUN = 501


def _job(name: str, job_id: int, attempt: int, *, start: str, runner: int) -> dict[str, object]:
    return {
        "id": job_id,
        "name": name,
        "run_id": RUN,
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": "success",
        "runner_id": runner,
        "runner_name": f"GitHub Actions {runner}",
        "created_at": start,
        "started_at": start,
        "completed_at": "2026-11-08T10:00:30Z" if start == "2026-11-08T10:00:00Z" else "2026-11-08T11:00:30Z",
        "steps": [{"number": 1, "name": "Native pytest", "status": "completed", "conclusion": "success", "started_at": start, "completed_at": "2026-11-08T10:00:30Z" if start == "2026-11-08T10:00:00Z" else "2026-11-08T11:00:30Z"}],
    }


class FakeGitHub:
    def __init__(self, attempts: dict[int, list[dict[str, object]]]):
        self.attempts = attempts

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        assert method == "GET" and payload is None
        if "/jobs?" in path:
            attempt = int(path.split("/attempts/", 1)[1].split("/", 1)[0])
            page = int(path.split("&page=", 1)[1])
            jobs = self.attempts[attempt]
            return GitDataResponse(200, {"total_count": len(jobs), "jobs": jobs[(page - 1) * 100 : page * 100]})
        attempt = int(path.split("/attempts/", 1)[1])
        return GitDataResponse(200, {"id": RUN, "run_attempt": attempt, "path": ".github/workflows/supervisor-integration-tests.yml", "head_sha": HEAD_SHA})


def _plan(profile: str, base: Path) -> VerifiedPlan:
    nodes = (
        (*FRESH_REQUIRED, "tests/test_management_fresh_host_integration.py::test_real_prior_supervisor_binds_instance_and_published_port")
        if profile == "fresh-host"
        else (
            CONTAINER_REQUIRED
            if profile == "isolated-containers"
            else (
                "tests/test_management_self_update_integration.py::test_case",
                "tests/test_management_fresh_host_integration.py::test_real_prior_supervisor_binds_instance_and_published_port",
            )
        )
    )
    context = ShardContext.parse(
        {
            "schema_version": "ci-shard-context-v1",
            "repository": REPOSITORY,
            "profile_id": profile,
            "checkout_sha": SHA,
            "pytest_args": list(profile_args(profile)),
            "harness_files": list(HARNESS),
            "semantic_environment": list(SEMANTIC_ENV),
            "origin": {"repository": REPOSITORY, "workflow": "Supervisor Integration Tests", "run_id": str(RUN), "run_attempt": "1", "planning_attempt": "initial", "event_sha": SHA},
        }
    )
    profile_identity = {"repository": REPOSITORY, "profile_id": profile, "configuration_digest": "sha256:" + "1" * 64, "python_major_minor": "3.12"}
    result = build_plan(context, profile_identity, nodes, None)
    directory = base / profile
    directory.mkdir(parents=True)
    (directory / "context.json").write_text(json.dumps(context.json()))
    (directory / "plan.json").write_bytes(_canonical(result))
    return VerifiedPlan(cast(str, result["digest_sha256"]), context, profile_identity, nodes, (nodes,))


def _inventory(profile: str, shard: int | None, nodes: tuple[str, ...], plan: VerifiedPlan | None, attempt: int, runner: int) -> dict[str, object]:
    return {
        "schema_version": "supervisor-native-inventory-v2",
        "checkout_sha": SHA,
        "event_sha": SHA,
        "run_id": str(RUN),
        "run_attempt": str(attempt),
        "job_key": "worker-bridge" if profile == "worker" else profile,
        "runner_name": f"GitHub Actions {runner}",
        "profile": profile,
        "plan_digest": plan.digest if plan is not None else None,
        "plan_origin": plan.context.origin if plan is not None else None,
        "shard": shard,
        "collected": list(nodes),
        "outcomes": dict.fromkeys(nodes, "passed"),
        "skip_reasons": {},
        "missing": [],
        "missing_selection": [],
        "unexpected": [],
        "repeated": [],
        "first_case_phase_epoch": 1794132010.0 if attempt == 1 else 1794135610.0,
        "last_case_phase_epoch": 1794132020.0 if attempt == 1 else 1794135620.0,
    }


def _artifact(base: Path, profile: str, shard: int | None, plan: VerifiedPlan | None, job: dict[str, object], attempt: int) -> Path:
    name = "worker-bridge" if profile == "worker" else f"{profile}-shard-{shard}"
    folder = base / f"supervisor-integration-{name}-{RUN}-{attempt}"
    folder.mkdir()
    if profile == "worker":
        reports = {"worker-bridge.json": _inventory(profile, None, ("worker::test_bridge",), None, attempt, cast(int, job["runner_id"])), "worker-daemon.json": _inventory(profile, None, ("worker::test_daemon",), None, attempt, cast(int, job["runner_id"]))}
    else:
        assert plan is not None
        reports = {f"{profile}-{shard}.json": _inventory(profile, shard, plan.inventory, plan, attempt, cast(int, job["runner_id"]))}
    hashes = {}
    for filename, report in reports.items():
        file = folder / filename
        file.write_text(json.dumps(report))
        hashes[filename] = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
    receipt = {
        "schema_version": "supervisor-native-job-receipt-v1",
        "repository": REPOSITORY,
        "run_id": RUN,
        "run_attempt": attempt,
        "checkout_sha": SHA,
        "event_sha": SHA,
        "profile": profile,
        "shard": shard,
        "plan_digest": plan.digest if plan is not None else None,
        "plan_origin_attempt": "1" if plan is not None else None,
        "job_id": job["id"],
        "job_name": job["name"],
        "job_key": "worker-bridge" if profile == "worker" else profile,
        "runner_name": job["runner_name"],
        "runner_id": job["runner_id"],
        "job_started_at": job["started_at"],
        "inventories": hashes,
    }
    (folder / "receipt.json").write_text(json.dumps(receipt))
    return folder


@pytest.fixture
def valid_native_evidence(tmp_path: Path):
    plans_dir = tmp_path / "plans"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    plans = {profile: _plan(profile, plans_dir) for profile in PROFILES}
    previous = _job(native_job_name("management-self-update", 1), 11, 1, start="2026-11-08T10:00:00Z", runner=11)
    alias = {**previous, "id": 101, "run_attempt": 2}
    current = [alias]
    _artifact(artifacts_dir, "management-self-update", 1, plans["management-self-update"], previous, 1)
    for index, profile in enumerate(("fresh-host", "isolated-containers"), 20):
        job = _job(native_job_name(profile, 1), index, 2, start="2026-11-08T11:00:00Z", runner=index)
        current.append(job)
        _artifact(artifacts_dir, profile, 1, plans[profile], job, 2)
    worker = _job(native_job_name("worker", None), 99, 2, start="2026-11-08T11:00:00Z", runner=99)
    current.append(worker)
    _artifact(artifacts_dir, "worker", None, None, worker, 2)
    return FakeGitHub({1: [previous], 2: current}), plans_dir, artifacts_dir


def _evaluate(valid_native_evidence):
    client, plans, artifacts = valid_native_evidence
    return evaluate(cast(GitHubGitDataClient, client), {name: {"result": "success"} for name in REQUIRED_GROUPS}, plans, artifacts, SHA, SHA, RUN, 2)


def test_native_rerun_alias_uses_original_successful_job_receipt(valid_native_evidence) -> None:
    selections, metrics = _evaluate(valid_native_evidence)
    management = next(item for item in selections if item.profile == "management-self-update")
    assert management.job["id"] == 101
    assert management.producing_job["id"] == 11
    assert management.producing_attempt == 1
    assert sum(item.profile == "worker" for item in selections) == 1
    assert all(metrics[profile]["peak_overlapping_test_phases"] >= 1 for profile in PROFILES)
    duplicate = "tests/test_management_fresh_host_integration.py::test_real_prior_supervisor_binds_instance_and_published_port"
    assert duplicate in next(item for item in selections if item.profile == "fresh-host").inventories[0]["collected"]
    assert duplicate in management.inventories[0]["collected"]


def test_ambiguous_original_producing_jobs_are_rejected(valid_native_evidence) -> None:
    client, _plans, _artifacts = valid_native_evidence
    client.attempts[1].append({**client.attempts[1][0], "id": 12})
    with pytest.raises(NativeEvidenceError, match="ambiguous original"):
        _evaluate(valid_native_evidence)


def test_native_alias_without_matching_original_receipt_is_rejected(valid_native_evidence) -> None:
    _client, _plans, artifacts = valid_native_evidence
    folder = artifacts / f"supervisor-integration-management-self-update-shard-1-{RUN}-1"
    (folder / "receipt.json").unlink()
    with pytest.raises(NativeEvidenceError, match="missing original producing-job receipt"):
        _evaluate(valid_native_evidence)


def test_changed_runner_identity_cannot_relabel_retained_pass(valid_native_evidence) -> None:
    client, _plans, _artifacts = valid_native_evidence
    client.attempts[2][0] = {**client.attempts[2][0], "runner_name": "Unrelated runner"}
    with pytest.raises(NativeEvidenceError, match="missing original producing-job receipt"):
        _evaluate(valid_native_evidence)


def test_foreign_original_receipt_plan_is_rejected(valid_native_evidence) -> None:
    _client, _plans, artifacts = valid_native_evidence
    receipt = artifacts / f"supervisor-integration-management-self-update-shard-1-{RUN}-1" / "receipt.json"
    data = json.loads(receipt.read_text())
    data["plan_digest"] = "sha256:" + "c" * 64
    receipt.write_text(json.dumps(data))
    with pytest.raises(NativeEvidenceError, match="mixed original plan"):
        _evaluate(valid_native_evidence)


def test_missing_full_profile_required_marker_refuses_plan_before_execution(tmp_path: Path) -> None:
    plan = _plan("fresh-host", tmp_path)
    incomplete = build_plan(plan.context, plan.profile, tuple(plan.inventory[1:]), None)
    path = tmp_path / "fresh-host" / "plan.json"
    path.write_bytes(_canonical(incomplete))
    with pytest.raises(ValueError, match="required anchor"):
        check_plan("fresh-host", path)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "failed", "cancelled", "forbidden-skip", "extra", "wrong-source", "bad-plan"])
def test_complete_native_inventory_refuses_all_false_success_paths(fault: str) -> None:
    context = ShardContext.parse(
        {
            "schema_version": "ci-shard-context-v1",
            "repository": REPOSITORY,
            "profile_id": "fresh-host",
            "checkout_sha": SHA,
            "pytest_args": list(profile_args("fresh-host")),
            "harness_files": [],
            "semantic_environment": [],
            "origin": {"repository": REPOSITORY},
        }
    )
    plan = VerifiedPlan("sha256:" + "2" * 64, context, {}, ("test::one", "test::two"), (("test::one", "test::two"),))
    report = _inventory("fresh-host", 1, plan.inventory, plan, 1, 1)
    if fault == "missing":
        cast(dict[str, str], report["outcomes"]).pop("test::two")
    elif fault == "duplicate":
        cast(list[str], report["collected"]).append("test::one")
    elif fault in ("failed", "cancelled", "forbidden-skip"):
        cast(dict[str, str], report["outcomes"])["test::one"] = fault
    elif fault == "extra":
        cast(dict[str, str], report["outcomes"])["test::extra"] = "passed"
    elif fault == "wrong-source":
        report["checkout_sha"] = HEAD_SHA
    else:
        report["plan_digest"] = "sha256:" + "3" * 64
    with pytest.raises(NativeEvidenceError):
        _inventory_ok(report, plan, "fresh-host", 1, SHA, RUN, 1)


def test_actual_collection_control_mode_never_creates_pass_receipt(tmp_path: Path, _use_real_commands) -> None:
    file = tmp_path / "test_profile.py"
    file.write_text("import pytest\n@pytest.mark.parametrize('item', [1, 2])\ndef test_case(item): assert item > 0\n")
    report = tmp_path / "receipt.json"
    import os
    import subprocess

    env = {**os.environ, "SUPERVISOR_CI_RESULT_MODE": "collect", "SUPERVISOR_RESULTS_PATH": str(report), "PYTHONPATH": str(ROOT / "scripts")}
    result = subprocess.run(["uv", "run", "pytest", "-p", "supervisor_ci_results", "--collect-only", "-q", str(file)], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not report.exists()
