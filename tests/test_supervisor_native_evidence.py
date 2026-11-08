"""Adversarial native-evidence and exact-profile Supervisor shard regressions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.plan_ci_shards import _collect  # noqa: E402
from supervisor_aggregate_evidence import REQUIRED_GROUPS, evaluate  # noqa: E402
from supervisor_ci_profiles import CONTAINER_REQUIRED, FRESH_REQUIRED, HARNESS, OPERATOR_REQUIRED, PROFILES, REPOSITORY, SEMANTIC_ENV, check_plan
from supervisor_ci_profiles import context as profile_context  # noqa: E402
from supervisor_ci_profiles import profile_args
from supervisor_native_evidence import GitDataResponse, NativeEvidenceError, _inventory_ok, capture, native_job_name, original_producer  # noqa: E402

from auto_coder.ci_shard_plan import ShardContext, VerifiedPlan, _canonical, build_plan, read_plan  # noqa: E402
from auto_coder.util.gh_cache import GitHubGitDataClient  # noqa: E402
from tests.test_ci_shard_plan import _history  # noqa: E402

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


def _plan(profile: str, base: Path, nodes_override: tuple[str, ...] | None = None) -> VerifiedPlan:
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
    if nodes_override is not None:
        nodes = nodes_override
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
    directory.mkdir(parents=True, exist_ok=True)
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
    name = "worker-bridge" if profile == "worker" else "operator-upgrade" if profile == "operator-upgrade" else f"{profile}-shard-{shard}"
    folder = base / f"supervisor-integration-{name}-{RUN}-{attempt}"
    folder.mkdir()
    if profile == "worker":
        reports = {"worker-bridge.json": _inventory(profile, None, ("worker::test_bridge",), None, attempt, cast(int, job["runner_id"])), "worker-daemon.json": _inventory(profile, None, ("worker::test_daemon",), None, attempt, cast(int, job["runner_id"]))}
    elif profile == "operator-upgrade":
        assert plan is None and shard is None
        reports = {"operator-upgrade.json": _inventory(profile, None, OPERATOR_REQUIRED, None, attempt, cast(int, job["runner_id"]))}
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
    operator = _job(native_job_name("operator-upgrade", None), 98, 2, start="2026-11-08T11:00:00Z", runner=98)
    current.append(operator)
    _artifact(artifacts_dir, "operator-upgrade", None, None, operator, 2)
    return FakeGitHub({1: [previous], 2: current}), plans_dir, artifacts_dir


def _evaluate(valid_native_evidence):
    client, plans, artifacts = valid_native_evidence
    return evaluate(cast(GitHubGitDataClient, client), {name: {"result": "success"} for name in REQUIRED_GROUPS}, plans, artifacts, SHA, SHA, RUN, 2)


@pytest.mark.parametrize("case", ("allowed", "wrong-reason", "sibling", "prefix"))
def test_parameterized_management_skips_cross_receipt_and_aggregate(valid_native_evidence, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    client, plans, artifacts = valid_native_evidence
    profile = "management-self-update"
    base = "tests/test_management_fresh_host_integration.py::" "test_real_prior_supervisor_binds_instance_and_published_port"
    variants = (f"{base}[False]", f"{base}[True]")
    unexpected = {
        "sibling": f"{base}_sibling[False]",
        "prefix": base.removesuffix("_port"),
    }.get(case)
    nodes = (
        "tests/test_management_self_update_integration.py::test_case",
        *variants,
        *((unexpected,) if unexpected else ()),
    )
    plan = _plan(profile, plans, nodes_override=nodes)
    artifact = artifacts / f"supervisor-integration-management-self-update-shard-1-{RUN}-1"
    inventory_file = artifact / f"{profile}-1.json"
    report = _inventory(profile, 1, plan.inventory, plan, 1, 11)
    skipped = (*variants, *((unexpected,) if unexpected else ()))
    for node in skipped:
        report["outcomes"][node] = "skipped"
        report["skip_reasons"][node] = "Skipped: an unrelated reason" if case == "wrong-reason" and node == variants[0] else "Skipped: production-profile image is not available locally"
    inventory_file.write_text(json.dumps(report), encoding="utf-8")
    receipt_file = artifact / "receipt.json"
    receipt_file.unlink()

    monkeypatch.setattr(
        "supervisor_native_evidence.subprocess.check_output",
        lambda *_args, **_kwargs: SHA,
    )
    env = {
        "GITHUB_RUN_ID": str(RUN),
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": SHA,
        "GITHUB_JOB": profile,
        "RUNNER_NAME": "GitHub Actions 11",
    }

    if case != "allowed":
        with pytest.raises(NativeEvidenceError, match="unauthorized or unproven native management skip"):
            capture(
                cast(GitHubGitDataClient, client),
                profile=profile,
                shard=1,
                plan_file=plans / profile / "plan.json",
                inventories=(inventory_file,),
                destination=receipt_file,
                environ=env,
            )
        assert not receipt_file.exists()
        return

    capture(
        cast(GitHubGitDataClient, client),
        profile=profile,
        shard=1,
        plan_file=plans / profile / "plan.json",
        inventories=(inventory_file,),
        destination=receipt_file,
        environ=env,
    )
    selected, _metrics = _evaluate(valid_native_evidence)
    management = next(item for item in selected if item.profile == profile)
    assert all(management.inventories[0]["outcomes"][node] == "skipped" for node in variants)

    # Even a valid receipt digest cannot authorize a changed skip reason.
    report["skip_reasons"][variants[0]] = "Skipped: another unapproved reason"
    inventory_file.write_text(json.dumps(report), encoding="utf-8")
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    receipt["inventories"][inventory_file.name] = "sha256:" + hashlib.sha256(inventory_file.read_bytes()).hexdigest()
    receipt_file.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(NativeEvidenceError, match="unauthorized or unproven native management skip"):
        _evaluate(valid_native_evidence)


@pytest.mark.parametrize(("profile", "runner"), (("fresh-host", 20), ("isolated-containers", 21)))
def test_privileged_profiles_reject_management_skip_reason(valid_native_evidence, monkeypatch: pytest.MonkeyPatch, profile: str, runner: int) -> None:
    client, plans, artifacts = valid_native_evidence
    plan_file = plans / profile / "plan.json"
    plan, _raw = read_plan(plan_file)
    artifact = artifacts / f"supervisor-integration-{profile}-shard-1-{RUN}-2"
    inventory_file = artifact / f"{profile}-1.json"
    report = _inventory(profile, 1, plan.inventory, plan, 2, runner)
    node = "tests/test_management_fresh_host_integration.py::" "test_real_prior_supervisor_binds_instance_and_published_port" if profile == "fresh-host" else plan.inventory[0]
    assert node in plan.inventory
    report["outcomes"][node] = "skipped"
    report["skip_reasons"][node] = "Skipped: production-profile image is not available locally"
    inventory_file.write_text(json.dumps(report), encoding="utf-8")
    receipt_file = artifact / "receipt.json"
    receipt_file.unlink()
    monkeypatch.setattr(
        "supervisor_native_evidence.subprocess.check_output",
        lambda *_args, **_kwargs: SHA,
    )

    with pytest.raises(NativeEvidenceError, match="mandatory privileged/container test was skipped"):
        capture(
            cast(GitHubGitDataClient, client),
            profile=profile,
            shard=1,
            plan_file=plan_file,
            inventories=(inventory_file,),
            destination=receipt_file,
            environ={
                "GITHUB_RUN_ID": str(RUN),
                "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_SHA": SHA,
                "GITHUB_JOB": profile,
                "RUNNER_NAME": f"GitHub Actions {runner}",
            },
        )
    assert not receipt_file.exists()


def test_operator_upgrade_missing_native_receipt_refuses_aggregate(valid_native_evidence) -> None:
    _client, _plans, artifacts = valid_native_evidence
    (artifacts / f"supervisor-integration-operator-upgrade-{RUN}-2" / "receipt.json").unlink()
    with pytest.raises(NativeEvidenceError, match="missing original producing-job receipt"):
        _evaluate(valid_native_evidence)


@pytest.mark.parametrize("fault", ("missing-case", "skipped-case"))
def test_operator_upgrade_original_inventory_never_accepts_missing_or_skipped_case(fault: str) -> None:
    report = _inventory("operator-upgrade", None, OPERATOR_REQUIRED, None, 1, 1)
    if fault == "missing-case":
        report["collected"].remove(OPERATOR_REQUIRED[0])
        report["outcomes"].pop(OPERATOR_REQUIRED[0])
    else:
        report["outcomes"][OPERATOR_REQUIRED[0]] = "skipped"
        report["skip_reasons"][OPERATOR_REQUIRED[0]] = "Skipped: fabricated success"
    with pytest.raises(NativeEvidenceError):
        _inventory_ok(report, None, "operator-upgrade", None, SHA, RUN, 1)


def test_native_rerun_alias_uses_original_successful_job_receipt(valid_native_evidence) -> None:
    selections, metrics = _evaluate(valid_native_evidence)
    management = next(item for item in selections if item.profile == "management-self-update")
    assert management.job["id"] == 101
    assert management.producing_job["id"] == 11
    assert management.producing_attempt == 1
    assert sum(item.profile == "worker" for item in selections) == 1
    assert sum(item.profile == "operator-upgrade" for item in selections) == 1
    assert metrics["operator-upgrade"]["shards"] == 1
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


def test_real_planned_pytest_shards_produce_complete_native_aggregate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """Run actual selected pytest shards through the receipt and aggregate boundary."""
    project = tmp_path / "checkout"
    project.mkdir()
    # Reproduce the workflow's exact fresh-host selectors and required parameter IDs
    # with disposable, side-effect-free cases; the production plugins remain unmodified.
    cases = {
        "test_management_fresh_host_integration.py": (
            "test_real_docker_published_listener_is_owned_by_replacement",
            "test_real_docker_listener_rejects_mismatched_container_address",
            "test_real_docker_listener_rejects_changed_container_inspection",
            "test_real_listener_accepts_only_mount_order_changes",
            "test_real_prior_supervisor_binds_instance_and_published_port",
        ),
        "test_management_fresh_end_to_end.py": ("test_initial_handoff",),
        "test_management_retained_startup_container.py": ("test_retained_startup",),
        "test_management_proxy_watcher_process_integration.py": ("test_proxy_watcher",),
    }
    for filename, names in cases.items():
        path = project / "tests" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"def {name}(): assert True" for name in names) + "\n", encoding="utf-8")
    for filename, test_name, variants in (
        ("test_management_archived_cutover_container.py", "test_real_second_cutover_preserves_all_owner_and_key_exclusions", ("complete", "host-interrupted", "omitted-ancestor", "stopped-prior")),
        ("test_management_installed_fresh_container.py", "test_installed_initial_abc_real_handoff", ("complete", "before-stop", "before-forward-start", "ready-corrupt", "host-interrupted", "source-unavailable")),
    ):
        (project / "tests" / filename).write_text(
            "import pytest\n" f"@pytest.mark.parametrize('variant', {list(variants)!r}, ids={list(variants)!r})\n" f"def {test_name}(variant): assert variant\n",
            encoding="utf-8",
        )
    (project / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    for name in HARNESS:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith("tests/"):
            target.write_text("", encoding="utf-8")
        else:
            shutil.copyfile(ROOT / name, target)
    for command in (("init", "-q"), ("config", "user.name", "CI Test"), ("config", "user.email", "ci@example.invalid"), ("add", "-A"), ("commit", "-qm", "isolated source")):
        subprocess.run(["git", *command], cwd=project, check=True, capture_output=True)
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()

    monkeypatch.chdir(project)
    monkeypatch.setattr(sys.modules[__name__], "SHA", checkout)
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(project), str(project / "scripts"), str(ROOT / "src"))),
        "PYTEST_ADDOPTS": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_PLUGINS": "",
        "GITHUB_WORKFLOW": "Supervisor Integration Tests",
        "GITHUB_RUN_ID": str(RUN),
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": checkout,
        "GITHUB_JOB": "fresh-host",
        "SUPERVISOR_REQUIRED_NODEIDS": json.dumps(FRESH_REQUIRED, separators=(",", ":")),
        "SUPERVISOR_ALLOWED_SKIPS": "{}",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    plan_dir = tmp_path / "plans"
    fresh_dir = plan_dir / "fresh-host"
    fresh_dir.mkdir(parents=True)
    context = profile_context("fresh-host", environ=environment, checkout=checkout)
    context_path = fresh_dir / "context.json"
    context_path.write_text(json.dumps(context.json()), encoding="utf-8")
    environment["SUPERVISOR_CI_RESULT_MODE"] = "collect"
    monkeypatch.setenv("SUPERVISOR_CI_RESULT_MODE", "collect")
    profile, collected = _collect(context_path, context)
    assert set(FRESH_REQUIRED) <= set(collected)
    assert len(collected) > len(FRESH_REQUIRED)
    history = tmp_path / "history.json"
    _history(history, profile, collected, seconds=90.0)
    plan_file = fresh_dir / "plan.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "plan_ci_shards.py"), "--context", str(context_path), "--duration-profile", str(history), "--output", str(plan_file), "--log-dir", str(tmp_path / "planner-logs"), "--", *profile_args("fresh-host")],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    planned, _raw = read_plan(plan_file)
    assert len(planned.shards) >= 2
    assert tuple(node for shard in planned.shards for node in shard) == collected
    assert len({index for index, shard in enumerate(planned.shards) if set(shard) & set(FRESH_REQUIRED)}) >= 2

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    management = _plan("management-self-update", plan_dir)
    containers = _plan("isolated-containers", plan_dir)
    jobs = [
        _job(native_job_name("worker", None), 10, 1, start="2026-11-08T10:00:00Z", runner=10),
        _job(native_job_name("management-self-update", 1), 11, 1, start="2026-11-08T10:00:00Z", runner=11),
        _job(native_job_name("isolated-containers", 1), 12, 1, start="2026-11-08T10:00:00Z", runner=12),
    ]
    client = FakeGitHub({1: jobs})
    _artifact(artifacts, "worker", None, None, jobs[0], 1)
    _artifact(artifacts, "management-self-update", 1, management, jobs[1], 1)
    _artifact(artifacts, "isolated-containers", 1, containers, jobs[2], 1)
    native_files = []
    for shard, assigned in enumerate(planned.shards, 1):
        job = _job(native_job_name("fresh-host", shard), 100 + shard, 1, start="2026-11-08T10:00:00Z", runner=100 + shard)
        jobs.append(job)
        report = tmp_path / f"fresh-host-{shard}.json"
        run_env = {
            **environment,
            "SUPERVISOR_CI_RESULT_MODE": "",
            "SUPERVISOR_RESULTS_PATH": str(report),
            "SUPERVISOR_PLAN_PATH": str(plan_file),
            "SUPERVISOR_PROFILE": "fresh-host",
            "SUPERVISOR_SHARD_ID": str(shard),
            "RUNNER_NAME": str(job["runner_name"]),
        }
        execution = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "scripts.ci_shard_plugin", f"--ci-plan={plan_file}", f"--ci-shard={shard}", "-q", "-vv", *profile_args("fresh-host"), f"--junitxml={tmp_path / f'fresh-host-{shard}.xml'}"],
            cwd=project,
            env=run_env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        assert execution.returncode == 0, execution.stdout + execution.stderr
        produced = json.loads(report.read_text(encoding="utf-8"))
        assert produced["collected"] == list(assigned)
        assert produced["outcomes"] == dict.fromkeys(assigned, "passed")
        assert produced["missing_selection"] == produced["missing"] == produced["repeated"] == []
        assert produced["first_case_phase_epoch"] <= produced["last_case_phase_epoch"]
        assert set(produced["collected"]) & set(FRESH_REQUIRED) == set(assigned) & set(FRESH_REQUIRED)
        native_files.append(report)
        folder = artifacts / f"supervisor-integration-fresh-host-shard-{shard}-{RUN}-1"
        folder.mkdir()
        consumed = folder / report.name
        shutil.copyfile(report, consumed)
        receipt = capture(
            cast(GitHubGitDataClient, client),
            profile="fresh-host",
            shard=shard,
            plan_file=plan_file,
            inventories=(consumed,),
            destination=folder / "receipt.json",
            environ=run_env,
        )
        assert receipt["job_id"] == job["id"]

    operator_job = _job(native_job_name("operator-upgrade", None), 13, 1, start="2026-11-08T10:00:00Z", runner=13)
    jobs.append(operator_job)
    _artifact(artifacts, "operator-upgrade", None, None, operator_job, 1)

    selected, metrics = evaluate(
        cast(GitHubGitDataClient, client),
        {name: {"result": "success"} for name in REQUIRED_GROUPS},
        plan_dir,
        artifacts,
        checkout,
        checkout,
        RUN,
        1,
    )
    observed = [node for item in selected if item.profile == "fresh-host" for report in item.inventories for node in report["collected"]]
    assert tuple(observed) == collected
    assert metrics["fresh-host"]["shards"] == len(planned.shards)
    duplicate = "tests/test_management_fresh_host_integration.py::test_real_prior_supervisor_binds_instance_and_published_port"
    assert duplicate in observed and duplicate in management.inventory

    # The real producer file, when made incomplete, must fail before a receipt can
    # be issued, even if the simulated native job still reports success.
    damaged = tmp_path / "damaged.json"
    data = json.loads(native_files[0].read_text(encoding="utf-8"))
    data["outcomes"].pop(data["collected"][0])
    damaged.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(NativeEvidenceError, match="outcomes are missing"):
        capture(
            cast(GitHubGitDataClient, client),
            profile="fresh-host",
            shard=1,
            plan_file=plan_file,
            inventories=(damaged,),
            destination=tmp_path / "forbidden-receipt.json",
            environ={**environment, "RUNNER_NAME": str(jobs[3]["runner_name"])},
        )
    assert not (tmp_path / "forbidden-receipt.json").exists()
