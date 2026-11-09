"""Real pytest collection/application and adversarial immutable-plan regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
from scripts.plan_ci_shards import _collect

from auto_coder.ci_duration_profile import BatchOrigin, DurationSample, ExecutionOrigin, ProfileIdentity
from auto_coder.ci_duration_profile import _digest as native_digest
from auto_coder.ci_duration_profile import _utc
from auto_coder.ci_shard_plan import ShardContext, ShardPlanError, _digest, allocate, build_plan, duration_estimates, read_plan, verify_plan

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("_use_real_commands")
TEST_SOURCE = """
import os
from pathlib import Path
import pytest

@pytest.fixture(autouse=True)
def record_test(request):
    path = os.environ.get("CI_SHARD_TEST_RECORD")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(request.node.nodeid + "\\n")

@pytest.mark.parametrize("value", range(5), ids=["zero", "one", "two", "three", "four"])
def test_parameterized(value):
    if value == 2:
        assert False, "the actual selected test must fail"
    if value == 4:
        pytest.skip("native skip is retained")

@pytest.mark.slow
def test_excluded():
    raise AssertionError("marker selector must exclude this")
"""


def _run(directory: Path, *args: str, record: Path | None = None, env_patch: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": ""}
    if record is not None:
        env["CI_SHARD_TEST_RECORD"] = str(record)
    if env_patch:
        env.update(env_patch)
    return subprocess.run([sys.executable, *args], cwd=directory, env=env, text=True, capture_output=True, timeout=90, check=False)


def _project(tmp_path: Path, source: str = TEST_SOURCE, harness: tuple[str, ...] = ("tests/conftest.py",)) -> tuple[Path, Path, ShardContext]:
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests/test_sample.py").write_text(source, encoding="utf-8")
    (project / "tests/conftest.py").write_text("", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\nmarkers = ["slow: marker excluded by the CI profile"]\n', encoding="utf-8")
    for arguments in (("init", "-q"), ("config", "user.name", "CI Test"), ("config", "user.email", "ci@example.invalid"), ("add", "-A"), ("commit", "-qm", "test base")):
        subprocess.run(["git", *arguments], cwd=project, check=True, capture_output=True)
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    context = ShardContext(
        "kitamura-tetsuo/auto-coder",
        "required-source",
        checkout,
        ("-m", "not slow"),
        harness,
        ("CI_SHARD_PROFILE_ENV",),
        {"repository": "kitamura-tetsuo/auto-coder", "workflow": "Source Tests", "run_id": "7", "run_attempt": "1", "planning_attempt": "plan-a", "event_sha": "f" * 40},
    )
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context.json()), encoding="utf-8")
    return project, context_path, context


def _inventory(project: Path, context_path: Path, context: ShardContext, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, object], tuple[str, ...]]:
    monkeypatch.chdir(project)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_PLUGINS", "")
    return _collect(context_path, context)


def _history(path: Path, profile_data: dict[str, object], nodeids: tuple[str, ...], seconds: float = 220) -> None:
    profile = ProfileIdentity.parse(profile_data)
    now = datetime.now(timezone.utc)
    stamp = _utc(now)
    origin = ExecutionOrigin(BatchOrigin("local", "a" * 40, None, None, None, None, None, "duration-test"), "native-process", 1001, f"{profile.python_major_minor}.1")
    nodes: dict[str, object] = {}
    for position, node in enumerate(nodeids):
        spans = {phase: f"{position}-{phase}" for phase in ("setup", "call", "teardown")}
        sample_id = native_digest({"profile": profile.json(), "origin": origin.json(), "nodeid": node, "phase_span_ids": spans})
        sample = DurationSample(sample_id, node, now, origin, spans, {phase: stamp for phase in spans}, {"setup": 0.0, "call": seconds * 1000, "teardown": 0.0}, {phase: {"outcome": "passed", "wasxfail": None} for phase in spans})
        nodes[node] = {"p95_seconds": sample.duration_seconds, "sample_count": 1, "oldest_at": stamp, "newest_at": stamp, "samples": [sample.json()]}
    payload: dict[str, object] = {
        "schema_version": "ci-duration-profile-v1",
        "profile": profile.json(),
        "generated_at": stamp,
        "retention_days": 14,
        "max_samples_per_node": 20,
        "min_estimate_seconds": 0.001,
        "status": "available" if nodes else "unavailable",
        "reasons": [] if nodes else ["no complete samples"],
        "inputs": [],
        "nodes": nodes,
    }
    payload["digest_sha256"] = native_digest(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _plan_cli(project: Path, context_path: Path, output: Path, history: Path | None = None, *selectors: str) -> subprocess.CompletedProcess[str]:
    options = ["--context", str(context_path), "--output", str(output), "--log-dir", str(output.parent / "logs")]
    if history is not None:
        options.extend(("--duration-profile", str(history)))
    return _run(project, str(ROOT / "scripts/plan_ci_shards.py"), *options, "--", *(selectors or ("-m", "not slow")))


def _apply(project: Path, plan: Path, shard: int, *, record: Path | None = None, selectors: tuple[str, ...] = ("-m", "not slow"), env_patch: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _run(project, "-m", "pytest", "-p", "scripts.ci_shard_plugin", f"--ci-plan={plan}", f"--ci-shard={shard}", *selectors, record=record, env_patch=env_patch)


def test_cold_start_supervisor_estimate_splits_real_collection_without_history(tmp_path: Path) -> None:
    project, context_path, context = _project(tmp_path)
    plan_path = tmp_path / "cold-start-plan.json"
    command = (
        str(ROOT / "scripts/plan_ci_shards.py"),
        "--context",
        str(context_path),
        "--cold-start-estimate-seconds",
        "120",
        "--output",
        str(plan_path),
        "--log-dir",
        str(tmp_path / "logs"),
        "--",
        *context.pytest_args,
    )
    created = _run(project, *command)
    assert created.returncode == 0, created.stdout + created.stderr
    verified, _raw = read_plan(plan_path)
    semantic = json.loads(plan_path.read_text())["semantic"]
    assert len(verified.inventory) == 5
    assert len(verified.shards) == 2
    assert [len(shard) for shard in verified.shards] == [3, 2]
    assert Counter(node for shard in verified.shards for node in shard) == Counter(verified.inventory)
    assert {entry["provenance"] for entry in semantic["estimates"]} == {"fallback_cold_start"}
    assert {entry["estimated_ms"] for entry in semantic["estimates"]} == {120_000}
    assert semantic["duration_source"]["status"] == "unavailable"

    invalid = _run(
        project,
        str(ROOT / "scripts/plan_ci_shards.py"),
        "--context",
        str(context_path),
        "--cold-start-estimate-seconds",
        "0",
        "--output",
        str(tmp_path / "invalid-plan.json"),
        "--log-dir",
        str(tmp_path / "invalid-logs"),
        "--",
        *context.pytest_args,
    )
    assert invalid.returncode != 0
    assert not (tmp_path / "invalid-plan.json").exists()


def test_contiguous_minimal_budget_boundary_and_indivisible_long_test() -> None:
    inventory = tuple(f"test.py::test_{position}" for position in range(5))
    estimates = [{"nodeid": node, "estimated_ms": ms} for node, ms in zip(inventory, (210_000, 210_000, 210_000, 821_000, 210_000))]
    shards, capacity, limited = allocate(inventory, estimates)
    assert [shard["nodeids"] for shard in shards] == [list(inventory[:2]), [inventory[2]], [inventory[3]], [inventory[4]]]
    assert capacity == 420_000 and not limited
    assert shards[2]["over_budget_nodes"] == [inventory[3]]
    assert shards[0]["estimated_ms"] == 420_000


def test_more_than_twelve_groups_binary_searches_smallest_capacity() -> None:
    inventory = tuple(f"test.py::test_{position}" for position in range(36))
    estimates = [{"nodeid": node, "estimated_ms": 210_001} for node in inventory]
    shards, capacity, limited = allocate(inventory, estimates)
    assert limited and len(shards) == 12  # Three contiguous nodes fit the minimum feasible capacity.
    assert capacity == 630_003
    assert Counter(node for group in shards for node in group["nodeids"]) == Counter(inventory)


def test_external_control_paths_preserve_fresh_unsharded_config_and_selection(tmp_path: Path) -> None:
    project, context_path, original = _project(tmp_path, source="def test_selected(): assert True\n")
    (project / "extras").mkdir()
    (project / "extras/test_excluded.py").write_text("def test_outside_testpaths(): assert False\n")
    (project / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\nxfail_strict = true\n')
    (project / "tests/conftest.py").write_text(
        "import json, os\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def record(request):\n"
        '    output = os.environ.get("CI_SHARD_TEST_RECORD")\n'
        "    if output:\n"
        '        with open(output, "a", encoding="utf-8") as stream:\n'
        '            stream.write(request.node.nodeid + "\\n")\n'
        "def pytest_sessionstart(session):\n"
        '    output = os.environ.get("CI_SHARD_DISCOVERY_RECORD")\n'
        "    if output:\n"
        '        Path(output).write_text(json.dumps({"root": str(session.config.rootpath), '
        '"config": str(session.config.inipath), "xfail_strict": session.config.getini("xfail_strict")}))\n'
    )
    for args in (("add", "-A"), ("commit", "-qm", "test root/config oracle")):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    context = ShardContext(
        original.repository,
        original.profile_id,
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip(),
        (),
        original.harness_files,
        original.semantic_environment,
        original.origin,
    )
    context_path.write_text(json.dumps(context.json()))
    report = tmp_path / "discovery.json"
    baseline_record = tmp_path / "baseline.txt"
    baseline = _run(
        project,
        "-m",
        "pytest",
        "-q",
        record=baseline_record,
        env_patch={"CI_SHARD_DISCOVERY_RECORD": str(report)},
    )
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    metadata = json.loads(report.read_text())
    assert metadata == {"root": str(project), "config": str(project / "pyproject.toml"), "xfail_strict": True}
    plan_file = tmp_path / "plan.json"
    created = _run(
        project,
        str(ROOT / "scripts/plan_ci_shards.py"),
        "--context",
        str(context_path),
        "--output",
        str(plan_file),
        "--log-dir",
        str(tmp_path / "logs"),
        "--",
        env_patch={"CI_SHARD_DISCOVERY_RECORD": str(report)},
    )
    assert created.returncode == 0, created.stdout + created.stderr
    assert json.loads(report.read_text()) == metadata
    plan, _ = read_plan(plan_file)
    assert plan.inventory == ("tests/test_sample.py::test_selected",)
    selected_record = tmp_path / "selected.txt"
    applied = _apply(
        project,
        plan_file,
        1,
        selectors=(),
        record=selected_record,
        env_patch={"CI_SHARD_DISCOVERY_RECORD": str(report)},
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert json.loads(report.read_text()) == metadata
    assert Counter(selected_record.read_text().splitlines()) == Counter(baseline_record.read_text().splitlines())


def test_explicit_untracked_effective_ini_change_is_refused_before_test(tmp_path: Path) -> None:
    source = """
import os
from pathlib import Path
import pytest

@pytest.mark.xfail(reason="ini controls XPASS strictness")
def test_xpass():
    output = os.environ.get("CI_SHARD_TEST_RECORD")
    if output:
        Path(output).write_text("executed\\n")
"""
    project, context_path, original = _project(tmp_path, source=source)
    ini = project / "pytest.ini"
    ini.write_text("[pytest]\nxfail_strict = true\n")
    context = ShardContext(
        original.repository,
        original.profile_id,
        original.checkout_sha,
        ("-c", "pytest.ini"),
        original.harness_files,
        original.semantic_environment,
        original.origin,
    )
    context_path.write_text(json.dumps(context.json()))
    plan_file = tmp_path / "ini-plan.json"
    created = _plan_cli(project, context_path, plan_file, None, "-c", "pytest.ini")
    assert created.returncode == 0, created.stdout + created.stderr
    record = tmp_path / "record.txt"
    same_ini = _apply(project, plan_file, 1, selectors=context.pytest_args, record=record)
    assert same_ini.returncode == 1, same_ini.stdout + same_ini.stderr
    assert "XPASS(strict)" in same_ini.stdout and record.read_text() == "executed\n"
    record.unlink()
    ini.write_text("[pytest]\nxfail_strict = false\n")
    changed_ini = _apply(project, plan_file, 1, selectors=context.pytest_args, record=record)
    assert changed_ini.returncode != 0, changed_ini.stdout + changed_ini.stderr
    assert "CI shard refused" in changed_ini.stdout or "CI shard refused" in changed_ini.stderr
    assert not record.exists()
    ini.write_text("[pytest]\nxfail_strict = true\n")
    restored = _apply(project, plan_file, 1, selectors=context.pytest_args, record=record)
    assert restored.returncode == 1 and "XPASS(strict)" in restored.stdout
    assert record.read_text() == "executed\n"


def test_legacy_separated_adapter_values_refuse_execution(tmp_path: Path) -> None:
    project, context_path, _ = _project(tmp_path)
    plan = tmp_path / "plan.json"
    created = _plan_cli(project, context_path, plan)
    assert created.returncode == 0, created.stdout + created.stderr
    record = tmp_path / "unexecuted.txt"
    legacy = _run(
        project,
        "-m",
        "pytest",
        "-p",
        "scripts.ci_shard_plugin",
        "--ci-plan",
        str(plan),
        "--ci-shard",
        "1",
        "-m",
        "not slow",
        record=record,
    )
    assert legacy.returncode != 0
    assert "require --ci-name=value" in legacy.stdout or "require --ci-name=value" in legacy.stderr
    assert not record.exists()


def test_real_cli_to_pytest_full_partition_and_native_failed_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, context_path, context = _project(tmp_path)
    profile, inventory = _inventory(project, context_path, context, monkeypatch)
    assert len(inventory) == 5 and "[two]" in inventory[2]
    history = tmp_path / "duration.json"
    _history(history, profile, inventory, 220.0)
    output = tmp_path / "plan.json"
    created = _plan_cli(project, context_path, output, history)
    assert created.returncode == 0, created.stdout + created.stderr
    plan, raw = read_plan(output)
    assert plan.inventory == inventory and len(plan.shards) == 5
    assert b'"digest_sha256"' in raw
    observed = tmp_path / "observed.txt"
    results = [_apply(project, output, shard_id, record=observed) for shard_id in range(1, len(plan.shards) + 1)]
    assert [result.returncode for result in results] == [0, 0, 1, 0, 0], [(result.stdout, result.stderr) for result in results]
    assert "skipped" in results[-1].stdout.lower()
    assert Counter(observed.read_text().splitlines()) == Counter(inventory)
    unsharded = tmp_path / "unsharded.txt"
    full = _run(project, "-m", "pytest", "-m", "not slow", record=unsharded)
    assert full.returncode == 1
    assert Counter(unsharded.read_text().splitlines()) == Counter(observed.read_text().splitlines())
    assert (tmp_path / "logs/ci-shard-plan.log").is_file()


@pytest.mark.parametrize("variation", ["selector", "profile", "digest", "shard", "new-node", "deleted-node", "source-edit", "checkout", "harness"])
def test_real_application_refuses_mismatches_before_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variation: str) -> None:
    project, context_path, context = _project(tmp_path)
    profile, inventory = _inventory(project, context_path, context, monkeypatch)
    history = tmp_path / "duration.json"
    _history(history, profile, inventory, 220)
    plan = tmp_path / "plan.json"
    assert _plan_cli(project, context_path, plan, history).returncode == 0
    record = tmp_path / "should-not-execute.txt"
    selectors: tuple[str, ...] = ("-m", "not slow")
    shard = 1
    extra_env: dict[str, str] | None = None
    if variation == "selector":
        selectors = ("-m", "slow")
    elif variation == "profile":
        extra_env = {"CI_SHARD_PROFILE_ENV": "different"}
    elif variation == "digest":
        data = json.loads(plan.read_text())
        data["semantic"]["budget_ms"] = 1
        plan.write_text(json.dumps(data))
    elif variation == "shard":
        shard = 999
    elif variation == "new-node":
        (project / "tests/test_new_node.py").write_text("def test_new_node(): pass\n")
    elif variation == "deleted-node":
        (project / "tests/test_sample.py").write_text(TEST_SOURCE.replace("range(5)", "range(4)"))
    elif variation == "source-edit":
        (project / "tests/test_sample.py").write_text(TEST_SOURCE.replace('assert False, "the actual selected test must fail"', "assert True"))
    elif variation == "checkout":
        (project / "tests/test_sample.py").write_text(TEST_SOURCE + "\ndef test_extra(): pass\n")
        for arguments in (("add", "-A"), ("commit", "-qm", "new source")):
            subprocess.run(["git", *arguments], cwd=project, check=True)
    elif variation == "harness":
        (project / "tests/conftest.py").write_text("# changed semantic harness\n")
    result = _apply(project, plan, shard, selectors=selectors, record=record, env_patch=extra_env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not record.exists()
    assert "CI shard refused" in result.stderr or "CI shard refused" in result.stdout


def test_corrupt_or_stale_optional_history_does_not_drop_required_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, context_path, context = _project(tmp_path)
    profile, inventory = _inventory(project, context_path, context, monkeypatch)
    corrupt = tmp_path / "history.json"
    corrupt.write_text('{"schema_version": "ci-duration-profile-v1", "nodes": {"x": NaN}}')
    plan = tmp_path / "plan.json"
    result = _plan_cli(project, context_path, plan, corrupt)
    assert result.returncode == 0, result.stdout + result.stderr
    semantic = json.loads(plan.read_text())["semantic"]
    assert semantic["inventory"] == list(inventory)
    assert semantic["duration_source"]["status"] == "unavailable"
    assert {entry["provenance"] for entry in semantic["estimates"]} == {"fallback_one_second"}
    assert len(semantic["shards"]) == 1
    missing_estimates, source = duration_estimates(tmp_path / "missing.json", profile, inventory)
    assert source["status"] == "unavailable" and len(missing_estimates) == len(inventory)


def test_stale_nodes_ignored_and_known_current_max_is_positive_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, context_path, context = _project(tmp_path)
    profile, inventory = _inventory(project, context_path, context, monkeypatch)
    history = tmp_path / "history.json"
    _history(history, profile, (inventory[0], "tests/deleted.py::test_old"), 820)
    estimates, source = duration_estimates(history, profile, inventory)
    assert source["status"] == "matched"
    assert estimates[0]["provenance"] == "native_p95" and estimates[0]["estimated_ms"] == 820_000
    assert all(item["estimated_ms"] == 820_000 and item["provenance"] == "fallback_current_max_p95" for item in estimates[1:])
    mismatched = dict(profile)
    mismatched["configuration_digest"] = "sha256:" + "0" * 64
    ignored, source = duration_estimates(history, mismatched, inventory)
    assert source["status"] == "unavailable" and all(item["estimated_ms"] == 1000 for item in ignored)


def test_extreme_finite_native_measurement_is_never_clipped_or_lost(tmp_path: Path) -> None:
    profile = {"repository": "kitamura-tetsuo/auto-coder", "profile_id": "duration-boundary", "configuration_digest": "sha256:" + "a" * 64, "python_major_minor": "3.12"}
    node = "tests/test_long.py::test_indivisible"
    history = tmp_path / "huge-duration.json"
    _history(history, profile, (node,), 1e305)
    data = json.loads(history.read_text())
    sample = data["nodes"][node]["samples"][0]
    sample["native_phase_ms"] = {phase: 1e308 for phase in ("setup", "call", "teardown")}
    sample["raw_duration_seconds"] = 3e305
    sample["duration_seconds"] = 3e305
    data["nodes"][node]["p95_seconds"] = 3e305
    data["digest_sha256"] = native_digest({key: value for key, value in data.items() if key != "digest_sha256"})
    history.write_text(json.dumps(data))
    estimates, source = duration_estimates(history, profile, (node,))
    assert source["status"] == "matched"
    assert estimates[0]["provenance"] == "native_p95"
    assert estimates[0]["estimated_ms"] == 3 * 10**308
    shards, capacity, limited = allocate((node,), estimates)
    assert not limited and capacity == 420_000
    assert shards[0]["nodeids"] == [node] and shards[0]["over_budget_nodes"] == [node]


def test_execution_keeps_plan_immutable_even_when_a_test_rewrites_it(tmp_path: Path) -> None:
    source = """import os
from pathlib import Path

def test_mutates_plan():
    Path(os.environ["CI_SHARD_MUTATE_PLAN"]).write_text("corrupted")
"""
    project, context_path, _ = _project(tmp_path, source=source)
    plan = tmp_path / "plan.json"
    created = _plan_cli(project, context_path, plan)
    assert created.returncode == 0, created.stdout + created.stderr
    result = _apply(project, plan, 1, env_patch={"CI_SHARD_MUTATE_PLAN": str(plan)})
    assert result.returncode != 0
    assert "plan integrity failure" in result.stdout
    assert "1 passed" in result.stdout


def test_empty_real_selection_and_duplicate_post_selector_inventory_fail_closed(tmp_path: Path) -> None:
    project, context_path, _ = _project(tmp_path, source="import pytest\n@pytest.mark.slow\ndef test_only_slow(): pass\n")
    empty = _plan_cli(project, context_path, tmp_path / "empty.json")
    assert empty.returncode != 0 and not (tmp_path / "empty.json").exists()
    # A registered collection plugin can duplicate IDs without adding a test.
    (project / "tests/test_sample.py").write_text(TEST_SOURCE)
    (project / "duplicate_plugin.py").write_text("def pytest_collection_modifyitems(items):\n    items[1]._id = items[0]._id\n")
    for arguments in (("add", "-A"), ("commit", "-qm", "registered collection hook")):
        subprocess.run(["git", *arguments], cwd=project, check=True)
    updated_context = ShardContext.read(context_path)
    updated_context = ShardContext(
        updated_context.repository,
        updated_context.profile_id,
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip(),
        ("-p", "duplicate_plugin", "-m", "not slow"),
        updated_context.harness_files,
        updated_context.semantic_environment,
        updated_context.origin,
    )
    context_path.write_text(json.dumps(updated_context.json()))
    duplicate = _plan_cli(project, context_path, tmp_path / "duplicate.json", None, *updated_context.pytest_args)
    assert duplicate.returncode != 0 and not (tmp_path / "duplicate.json").exists()


def test_canonical_digest_assignments_and_origin_separation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, context_path, context = _project(tmp_path)
    profile, inventory = _inventory(project, context_path, context, monkeypatch)
    plan = build_plan(context, profile, inventory, None)
    verified = verify_plan(plan)
    assert verified.digest == plan["digest_sha256"]
    different = {**plan, "origin": {**context.origin, "planning_attempt": "plan-b"}}
    assert verify_plan(different).digest == verified.digest
    semantic = dict(plan["semantic"])
    semantic["inventory"] = list(reversed(inventory))
    modified = {**plan, "semantic": semantic, "digest_sha256": _digest(semantic)}
    with pytest.raises(ShardPlanError, match="inventory digest"):
        verify_plan(modified)
    assignments = [dict(shard) for shard in plan["semantic"]["shards"]]
    assignments[0]["nodeids"] = list(reversed(assignments[0]["nodeids"]))
    tampered_semantic = {**plan["semantic"], "shards": assignments}
    with pytest.raises(ShardPlanError, match="assignments violate"):
        verify_plan({**plan, "semantic": tampered_semantic, "digest_sha256": _digest(tampered_semantic)})
    assert len(plan["semantic"]["shards"]) == 1


def _bootstrap_snapshot(tmp_path: Path, durations: dict[str, float], *, selector: str = "not opencode_live") -> tuple[Path, Path]:
    import hashlib

    snapshot = tmp_path / "bootstrap-snapshot.json"
    provenance = tmp_path / "bootstrap-provenance.json"
    snapshot.write_text(json.dumps(durations), encoding="utf-8")
    provenance.write_text(
        json.dumps(
            {
                "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                "nodes": len(durations),
                "selector": selector,
                "unit": "seconds",
            }
        ),
        encoding="utf-8",
    )
    return snapshot, provenance


@pytest.mark.parametrize("failure", ("sha", "node-count", "selector", "unit", "duplicate", "negative", "nonfinite", "boolean", "missing"))
def test_source_bootstrap_never_accepts_unverified_estimates(tmp_path: Path, failure: str) -> None:
    from auto_coder.ci_shard_plan import _snapshot_estimates

    snapshot, provenance = _bootstrap_snapshot(tmp_path, {"tests/test_a.py::test_a": 2.15})
    if failure in ("sha", "node-count", "selector", "unit"):
        data = json.loads(provenance.read_text())
        data[{"sha": "snapshot_sha256", "node-count": "nodes", "selector": "selector", "unit": "unit"}[failure]] = "invalid"
        provenance.write_text(json.dumps(data))
    elif failure in ("duplicate", "negative", "nonfinite", "boolean"):
        contents = {
            "duplicate": '{"tests/test_a.py::test_a": 1, "tests/test_a.py::test_a": 2}',
            "negative": '{"tests/test_a.py::test_a": -1}',
            "nonfinite": '{"tests/test_a.py::test_a": NaN}',
            "boolean": '{"tests/test_a.py::test_a": true}',
        }[failure]
        snapshot.write_text(contents)
        import hashlib

        data = json.loads(provenance.read_text())
        data["snapshot_sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        provenance.write_text(json.dumps(data))
    else:
        snapshot.unlink()
    weights, evidence = _snapshot_estimates(snapshot, provenance)
    assert weights == {}
    assert evidence["status"] == "unavailable" and evidence["reason"]
    assert evidence["digest"] is None


def test_source_exact_priority_same_file_and_source_only_floor(tmp_path: Path) -> None:
    from auto_coder.ci_shard_plan import source_duration_estimates

    inventory = (
        "tests/test_a.py::test_native",
        "tests/test_a.py::test_snapshot",
        "tests/test_a.py::test_new[param]",
        "tests/test_b.py::test_unknown",
        "tests/test_c.py::test_snapshot_zero",
    )
    snapshot, provenance = _bootstrap_snapshot(
        tmp_path,
        {inventory[0]: 1.25, inventory[1]: 7.0000001, inventory[4]: 0.0, "tests/test_obsolete.py::test_old": 900.0},
    )
    weights, evidence = __import__("auto_coder.ci_shard_plan", fromlist=["_snapshot_estimates"])._snapshot_estimates(snapshot, provenance)
    assert weights[inventory[1]] == 7001 and weights[inventory[4]] == 1
    assert evidence["status"] == "matched"
    # A validated snapshot is advisory and never decides which node IDs exist.
    estimates, native_source, snapshot_source = source_duration_estimates(None, {}, inventory, snapshot, provenance, cold_start_fallback_ms=120_000)
    assert [entry["nodeid"] for entry in estimates] == list(inventory)
    assert [entry["provenance"] for entry in estimates] == ["snapshot_exact", "snapshot_exact", "same_file_max", "source_floor", "snapshot_exact"]
    assert [entry["estimated_ms"] for entry in estimates] == [1250, 7001, 7001, 120000, 1]
    assert estimates[2]["basis_nodeid"] == inventory[1]
    assert native_source["status"] == "unavailable" and snapshot_source["status"] == "matched"


def test_source_policy_actual_cli_full_collection_and_native_outcomes(tmp_path: Path) -> None:
    project, context_path, context = _project(tmp_path)
    selected = tuple(f"tests/test_sample.py::test_parameterized[{name}]" for name in ("zero", "one", "two", "three", "four"))
    bootstrap, provenance = _bootstrap_snapshot(tmp_path, {selected[0]: 210, selected[2]: 800, selected[4]: 0.0, "old_test.py::old": 300.0})
    plan = tmp_path / "source-plan.json"
    result = _run(
        project,
        str(ROOT / "scripts/plan_ci_shards.py"),
        "--context",
        str(context_path),
        "--duration-policy",
        "source-required-v1",
        "--bootstrap-duration-snapshot",
        str(bootstrap),
        "--bootstrap-duration-provenance",
        str(provenance),
        "--output",
        str(plan),
        "--log-dir",
        str(tmp_path / "logs"),
        "--",
        *context.pytest_args,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    verified, _ = read_plan(plan)
    assert Counter(verified.inventory) == Counter(selected)
    payload = json.loads(plan.read_text())["semantic"]
    assert payload["duration_policy"] == "source-required-v1"
    assert payload["snapshot_source"]["status"] == "matched"
    assert payload["duration_source"]["status"] == "unavailable"
    assert set(payload["inventory"]) == set(selected) and len(verified.shards) <= 12
    assert payload["estimates"][2]["estimated_ms"] == 800000
    assert any(shard["over_budget_nodes"] for shard in payload["shards"])
    assert Counter(node for shard in verified.shards for node in shard) == Counter(selected)
    observed: list[str] = []
    actual_results: list[subprocess.CompletedProcess[str]] = []
    for number in range(1, len(verified.shards) + 1):
        log = tmp_path / f"shard-{number}.log"
        completed = _apply(project, plan, number, record=log)
        assert completed.returncode in (0, 1), completed.stdout + completed.stderr
        actual_results.append(completed)
        observed.extend(log.read_text().splitlines())
    assert Counter(observed) == Counter(selected)
    assert selected[2] in observed and selected[4] in observed
    assert any(item.returncode == 1 and "failed" in item.stdout for item in actual_results)
    assert any("skipped" in item.stdout for item in actual_results)


def test_source_policy_immutable_provenance_and_no_partial_cli_pair(tmp_path: Path) -> None:
    project, context_path, context = _project(tmp_path)
    snapshot, provenance = _bootstrap_snapshot(tmp_path, {"tests/test_sample.py::test_parameterized[zero]": 3.0})
    cli = [
        str(ROOT / "scripts/plan_ci_shards.py"),
        "--context",
        str(context_path),
        "--duration-policy",
        "source-required-v1",
        "--bootstrap-duration-snapshot",
        str(snapshot),
        "--bootstrap-duration-provenance",
        str(provenance),
        "--log-dir",
        str(tmp_path / "logs"),
        "--output",
        str(tmp_path / "source-plan.json"),
        "--",
        *context.pytest_args,
    ]
    created = _run(project, *cli)
    assert created.returncode == 0, created.stdout + created.stderr
    plan = json.loads((tmp_path / "source-plan.json").read_text())
    semantic = plan["semantic"]
    assert any(entry["provenance"] == "same_file_max" for entry in semantic["estimates"])
    for field in ("basis_nodeid", "estimated_ms", "basis_provenance"):
        corrupted = json.loads(json.dumps(plan))
        entry = next(row for row in corrupted["semantic"]["estimates"] if row["provenance"] == "same_file_max")
        entry[field] = "other.py::other" if field == "basis_nodeid" else ("native_p95" if field == "basis_provenance" else 123456)
        corrupted["digest_sha256"] = _digest(corrupted["semantic"])
        with pytest.raises(ShardPlanError):
            verify_plan(corrupted)
    paired = list(cli)
    start = paired.index("--bootstrap-duration-provenance")
    del paired[start : start + 2]
    paired[paired.index(str(tmp_path / "source-plan.json"))] = str(tmp_path / "invalid-plan.json")
    refused = _run(project, *paired)
    assert refused.returncode != 0 and "must be supplied together" in (refused.stdout + refused.stderr)
    assert not (tmp_path / "invalid-plan.json").exists()


def test_source_policy_balances_weighted_long_files_without_changing_membership(tmp_path: Path) -> None:
    from auto_coder.ci_shard_plan import source_duration_estimates

    inventory = tuple(f"tests/test_fast.py::test_{x}" for x in range(20)) + tuple(f"tests/test_management.py::test_{x}" for x in range(5)) + tuple(f"tests/test_new.py::test_{x}" for x in range(3))
    snapshot, provenance = _bootstrap_snapshot(tmp_path, {inventory[0]: 1.0, inventory[20]: 220.0, inventory[22]: 240.0})
    estimates, _native, bootstrap = source_duration_estimates(None, {}, inventory, snapshot, provenance)
    assert bootstrap["status"] == "matched"
    shards, cap, limited = allocate(inventory, estimates)
    assert cap == 420000 and not limited
    assert Counter(node for shard in shards for node in shard["nodeids"]) == Counter(inventory)
    assert sum(1 for shard in shards if any("test_management.py" in node for node in shard["nodeids"])) >= 3
    assert all(sum("test_management.py" in node for node in shard["nodeids"]) <= 1 for shard in shards)
    assert [entry["estimated_ms"] for entry in estimates[20:25]] == [220000, 240000, 240000, 240000, 240000]


def test_source_workflow_opt_in_is_bounded_to_complete_source() -> None:
    source = (ROOT / ".github/workflows/test-source-for-runtime.yml").read_text()
    assert "--duration-policy source-required-v1" in source
    assert "--bootstrap-duration-snapshot tests/data/pr-test-durations.json" in source
    assert "--bootstrap-duration-provenance tests/data/pr-test-durations.provenance.json" in source
    assert "--ci-shard=${{ matrix.shard }}" in source
    assert "-m 'not opencode_live'" in source
    assert "--source-ci-receipt=" in source
    for path in (ROOT / ".github/workflows/pr-tests.yml", ROOT / ".github/workflows/supervisor-integration-tests.yml"):
        assert "--duration-policy source-required-v1" not in path.read_text()


def test_source_valid_partial_native_profile_and_invalid_history_preserve_current_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder.ci_shard_plan import source_duration_estimates

    project, context_path, context = _project(tmp_path)
    profile, actual_nodes = _inventory(project, context_path, context, monkeypatch)
    current = (actual_nodes[0], actual_nodes[1], "tests/test_unrelated.py::test_brand_new", "tests/test_other.py::test_param[new]")
    history = tmp_path / "native-profile.json"
    _history(history, profile, (current[0],), seconds=126)
    snapshot, provenance = _bootstrap_snapshot(tmp_path, {current[1]: 2.5})
    estimated, native_source, bootstrap = source_duration_estimates(history, profile, current, snapshot, provenance)
    assert native_source["status"] == "matched"
    assert bootstrap["status"] == "matched"
    assert [entry["provenance"] for entry in estimated] == ["native_p95", "snapshot_exact", "source_floor", "source_floor"]
    assert [entry["estimated_ms"] for entry in estimated] == [126000, 2500, 5000, 5000]
    assert estimated[0]["sample_count"] == 1
    assert sum("native_p95" in str(row) for row in estimated) == 1
    # Foreign native identity cannot override validated non-native estimates or the full current inventory.
    foreign = dict(profile)
    foreign["configuration_digest"] = "sha256:" + "f" * 64
    degraded, native_source, bootstrap = source_duration_estimates(history, foreign, current, snapshot, provenance)
    assert native_source["status"] == "unavailable" and native_source["reason"]
    assert bootstrap["status"] == "matched"
    assert [row["nodeid"] for row in degraded] == list(current)
    assert [row["provenance"] for row in degraded] == ["same_file_max", "snapshot_exact", "source_floor", "source_floor"]
    assert [row["estimated_ms"] for row in degraded] == [2500, 2500, 5000, 5000]


def test_source_policy_degraded_history_cli_applies_every_fresh_node_with_native_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover partial/foreign/corrupt history through real collection, plan, and pytest shards."""
    source = TEST_SOURCE + """
@pytest.mark.parametrize("variant", ["recent"], ids=["new-parameter"])
def test_added_parameter(variant):
    assert variant == "recent"
"""
    project, context_path, original = _project(tmp_path, source=source)
    (project / "tests/test_unseen.py").write_text(
        """
import os
import pytest

@pytest.mark.parametrize("variant", ["brand-new"], ids=["unseen-file"])
def test_new_file(variant, request):
    record = os.environ.get("CI_SHARD_TEST_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as stream:
            stream.write(request.node.nodeid + "\\n")
    assert variant == "brand-new"
""",
        encoding="utf-8",
    )
    for args in (("add", "-A"), ("commit", "-qm", "include new parameter and unseen file")):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    context = ShardContext(
        original.repository,
        original.profile_id,
        checkout,
        original.pytest_args,
        original.harness_files,
        original.semantic_environment,
        original.origin,
    )
    context_path.write_text(json.dumps(context.json()), encoding="utf-8")
    profile, selected = _inventory(project, context_path, context, monkeypatch)
    assert len(selected) == 7
    assert "tests/test_sample.py::test_added_parameter[new-parameter]" in selected
    assert "tests/test_unseen.py::test_new_file[unseen-file]" in selected

    # The unsharded native execution supplies an independent membership/outcome oracle.
    baseline_record = tmp_path / "unsharded-record.txt"
    baseline = _run(project, "-m", "pytest", "-m", "not slow", "-q", record=baseline_record)
    assert baseline.returncode == 1, baseline.stdout + baseline.stderr
    assert "1 failed" in baseline.stdout and "1 skipped" in baseline.stdout
    assert Counter(baseline_record.read_text().splitlines()) == Counter(selected)

    native_node = "tests/test_sample.py::test_parameterized[zero]"
    snapshot_node = "tests/test_sample.py::test_parameterized[one]"
    heavy_node = "tests/test_sample.py::test_parameterized[two]"
    added_node = "tests/test_sample.py::test_added_parameter[new-parameter]"
    unseen_node = "tests/test_unseen.py::test_new_file[unseen-file]"
    valid_history = tmp_path / "native-partial.json"
    _history(valid_history, profile, (native_node,), seconds=126)
    foreign_history = tmp_path / "native-foreign.json"
    foreign_identity = dict(profile)
    foreign_identity["configuration_digest"] = "sha256:" + "f" * 64
    _history(foreign_history, foreign_identity, (native_node,), seconds=126)
    corrupt_history = tmp_path / "native-corrupt.json"
    corrupt_history.write_text("{invalid-json", encoding="utf-8")

    snapshot, provenance = _bootstrap_snapshot(
        tmp_path,
        {snapshot_node: 2.5, heavy_node: 801, "tests/test_retired.py::test_removed": 999},
    )
    bad_provenance = tmp_path / "bootstrap-bad-provenance.json"
    mismatched = json.loads(provenance.read_text())
    mismatched["selector"] = "not slow"
    bad_provenance.write_text(json.dumps(mismatched), encoding="utf-8")
    scenarios = (
        ("partial", valid_history, snapshot, provenance, "matched", "matched"),
        ("foreign", foreign_history, snapshot, provenance, "unavailable", "matched"),
        ("corrupt", corrupt_history, snapshot, provenance, "unavailable", "matched"),
        ("missing", tmp_path / "missing-native.json", tmp_path / "missing-snapshot.json", tmp_path / "missing-provenance.json", "unavailable", "unavailable"),
        ("bad-provenance", corrupt_history, snapshot, bad_provenance, "unavailable", "unavailable"),
    )
    for name, history, bootstrap, provenance_file, native_status, snapshot_status in scenarios:
        plan_file = tmp_path / f"source-{name}.json"
        result = _run(
            project,
            str(ROOT / "scripts/plan_ci_shards.py"),
            "--context",
            str(context_path),
            "--duration-policy",
            "source-required-v1",
            "--duration-profile",
            str(history),
            "--bootstrap-duration-snapshot",
            str(bootstrap),
            "--bootstrap-duration-provenance",
            str(provenance_file),
            "--output",
            str(plan_file),
            "--log-dir",
            str(tmp_path / f"logs-{name}"),
            "--",
            *context.pytest_args,
        )
        assert result.returncode == 0, f"{name}: {result.stdout}{result.stderr}"
        verified, _ = read_plan(plan_file)
        semantic = json.loads(plan_file.read_text())["semantic"]
        assert semantic["duration_policy"] == "source-required-v1"
        assert semantic["duration_source"]["status"] == native_status
        assert semantic["snapshot_source"]["status"] == snapshot_status
        if native_status == "unavailable":
            assert semantic["duration_source"]["reason"]
        if snapshot_status == "unavailable":
            assert semantic["snapshot_source"]["reason"]
        assert Counter(verified.inventory) == Counter(selected)
        assert Counter(node for shard in verified.shards for node in shard) == Counter(selected)
        estimates = {row["nodeid"]: row for row in semantic["estimates"]}
        assert set(estimates) == set(selected)
        assert all(row["estimated_ms"] > 0 for row in estimates.values())
        if snapshot_status == "matched":
            assert estimates[snapshot_node]["provenance"] == "snapshot_exact"
            assert estimates[heavy_node]["provenance"] == "snapshot_exact"
            assert estimates[heavy_node]["estimated_ms"] == 801_000
            assert estimates[added_node]["provenance"] == "same_file_max"
            assert estimates[added_node]["basis_nodeid"] == heavy_node
            assert estimates[unseen_node]["provenance"] == "source_floor"
            assert any(heavy_node in shard["over_budget_nodes"] for shard in semantic["shards"])
            if native_status == "matched":
                assert estimates[native_node]["provenance"] == "native_p95"
                assert estimates[native_node]["estimated_ms"] == 126_000
            else:
                assert estimates[native_node]["provenance"] == "same_file_max"
        else:
            assert {row["provenance"] for row in estimates.values()} == {"source_floor"}
            assert {row["estimated_ms"] for row in estimates.values()} == {5_000}
        observed: list[str] = []
        shard_results: list[subprocess.CompletedProcess[str]] = []
        for shard_number in range(1, len(verified.shards) + 1):
            record = tmp_path / f"{name}-shard-{shard_number}.txt"
            completed = _apply(project, plan_file, shard_number, record=record)
            assert completed.returncode in (0, 1), f"{name} shard {shard_number}: {completed.stdout}{completed.stderr}"
            shard_results.append(completed)
            observed.extend(record.read_text().splitlines())
        assert Counter(observed) == Counter(selected), name
        assert Counter(observed) == Counter(baseline_record.read_text().splitlines()), name
        assert any(completed.returncode == 1 and "failed" in completed.stdout for completed in shard_results), name
        assert any("skipped" in completed.stdout for completed in shard_results), name
