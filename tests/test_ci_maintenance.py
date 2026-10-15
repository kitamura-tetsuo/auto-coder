import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("ci_maintenance", ROOT / "scripts/ci_maintenance.py")
assert SPEC and SPEC.loader
ci_maintenance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ci_maintenance
SPEC.loader.exec_module(ci_maintenance)


def policy(**changes):
    value = json.loads((ROOT / ".github/ci-maintenance.json").read_text(encoding="utf-8"))
    value.update(changes)
    return value


def event(body="", *, number=10, repository="kitamura-tetsuo/auto-coder", head_repository=None):
    return {
        "repository": {"full_name": repository},
        "pull_request": {
            "number": number,
            "body": body,
            "head": {"sha": "abc123", "repo": {"full_name": head_repository or repository}},
        },
    }


def run_selector(tmp_path, command, configured_policy, source_event=None):
    policy_path = tmp_path / f"{command}-policy.json"
    output_path = tmp_path / f"{command}-output"
    policy_path.write_text(json.dumps(configured_policy), encoding="utf-8")
    arguments = [sys.executable, "-S", str(ROOT / "scripts/ci_maintenance.py"), command, "--policy", str(policy_path)]
    if source_event is not None:
        event_path = tmp_path / f"{command}-event.json"
        event_path.write_text(json.dumps(source_event), encoding="utf-8")
        arguments.extend(("--event", str(event_path)))
    environment = os.environ.copy()
    environment["GITHUB_OUTPUT"] = str(output_path)
    result = subprocess.run(arguments, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return output_path.read_text(encoding="utf-8").splitlines()[-1].removeprefix("mode=")


def selected_jobs(workflow, mode):
    selected = set()
    for name, job in workflow["jobs"].items():
        condition = job.get("if")
        if condition is None or condition == f"needs.maintenance.outputs.mode == '{mode}'" or (condition == "always() && needs.maintenance.outputs.mode == 'NORMAL'" and mode == "NORMAL"):
            selected.add(name)
    return selected


def job_commands(workflow, job_name):
    return [step.get("run", f"uses:{step.get('uses')}") for step in workflow["jobs"][job_name]["steps"]]


def test_selector_is_normal_without_exact_valid_scope():
    cases = [
        (event("mentions #2503"), policy()),
        (event("Closes #2502"), policy()),
        (event("Closes #2503", head_repository="someone/fork"), policy()),
        (event("Closes #2503"), policy(active=False)),
        (event("Closes #2503"), policy(repository="other/repo")),
        ({}, policy()),
    ]
    assert [ci_maintenance.select(source, configured).mode for source, configured in cases] == ["NORMAL"] * len(cases)


def test_core_closing_reference_pauses_but_restoration_wins():
    assert ci_maintenance.select(event("Fixes #2503"), policy()).mode == "PAUSED"
    assert ci_maintenance.select(event("Fixes https://github.com/kitamura-tetsuo/auto-coder/issues/2506"), policy()).mode == "PAUSED"
    restored = ci_maintenance.select(event("Closes #2503\nCloses #2507"), policy())
    assert restored.mode == "NORMAL"
    assert restored.reason == "restoration issue takes precedence"


def test_repository_qualified_restoration_reference_selects_full_workload(tmp_path):
    mode = run_selector(
        tmp_path,
        "select-pr",
        policy(),
        event("Closes #2503\nFixes kitamura-tetsuo/auto-coder#2507"),
    )
    workflow = yaml.safe_load((ROOT / ".github/workflows/pr-tests.yml").read_text(encoding="utf-8"))

    assert mode == "NORMAL"
    assert {"tests-shard", "tests"} <= selected_jobs(workflow, mode)
    assert workflow["jobs"]["tests-shard"]["strategy"]["matrix"]["group"] == [1, 2, 3, 4]
    assert any("scripts/run_pr_test_shard.py" in command for command in job_commands(workflow, "tests-shard"))
    assert any("needs.tests-shard.result" in command for command in job_commands(workflow, "tests"))


def test_explicit_same_repository_pr_identity_is_supported():
    assert ci_maintenance.select(event(number=44), policy(associated_pull_requests=[44])).mode == "PAUSED"
    assert ci_maintenance.select(event(number=45), policy(associated_pull_requests=[44])).mode == "NORMAL"


def test_exact_source_run_requires_one_successful_build_job():
    success = {"jobs": [{"name": "Build tested artifact", "status": "completed", "conclusion": "success"}]}
    assert ci_maintenance.require_published_job(success, "Build tested artifact") is True
    for jobs in (
        {"jobs": []},
        {"jobs": [{"name": "Build tested artifact", "status": "completed", "conclusion": "skipped"}]},
        {"jobs": success["jobs"] * 2},
        {},
    ):
        assert ci_maintenance.require_published_job(jobs, "Build tested artifact") is False


def test_selector_runs_before_dependencies_are_installed(tmp_path):
    event_path = tmp_path / "event.json"
    output_path = tmp_path / "output"
    event_path.write_text(json.dumps(event("Closes #2502")), encoding="utf-8")
    environment = os.environ.copy()
    environment["GITHUB_OUTPUT"] = str(output_path)

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "scripts/ci_maintenance.py"),
            "select-pr",
            "--event",
            str(event_path),
            "--policy",
            str(ROOT / ".github/ci-maintenance.json"),
        ],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "NORMAL\n"
    assert output_path.read_text(encoding="utf-8") == "mode=NORMAL\n"


def test_real_workflow_wiring_stops_commands_and_retains_failures():
    pr_text = (ROOT / ".github/workflows/pr-tests.yml").read_text(encoding="utf-8")
    publish_text = (ROOT / ".github/workflows/publish-beta.yml").read_text(encoding="utf-8")
    advance_text = (ROOT / ".github/workflows/advance-beta.yml").read_text(encoding="utf-8")
    pr = yaml.safe_load(pr_text)
    publish = yaml.safe_load(publish_text)
    assert pr["jobs"]["tests-shard"]["if"] == "needs.maintenance.outputs.mode == 'NORMAL'"
    assert pr["jobs"]["tests"]["needs"] == ["maintenance", "tests-shard"]
    assert pr["jobs"]["retained-checks"]["if"] == "needs.maintenance.outputs.mode == 'PAUSED'"
    retained = "\n".join(step.get("run", "") for step in pr["jobs"]["retained-checks"]["steps"])
    assert "auto-coder --help" in retained
    assert "-m pr_lifecycle_contract tests/test_pr_lifecycle_contract.py" in retained
    assert "continue-on-error" not in pr_text
    assert publish["jobs"]["build-tested-artifact"]["if"] == "needs.maintenance.outputs.mode == 'NORMAL'"
    build_steps = str(publish["jobs"]["build-tested-artifact"]["steps"])
    assert "bash scripts/test.sh" in build_steps and "docker/build-push-action" in build_steps and "docker push" in build_steps
    assert "require-published-job" in advance_text
    assert "actions/runs/$RUN_ID/jobs" in advance_text


def test_real_main_selector_suppresses_and_restores_publication_commands(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-beta.yml").read_text(encoding="utf-8"))
    paused_mode = run_selector(tmp_path, "select-main", policy())
    normal_mode = run_selector(tmp_path, "select-main", policy(active=False))

    assert paused_mode == "PAUSED"
    assert "build-tested-artifact" not in selected_jobs(workflow, paused_mode)
    paused_commands = [command for job in selected_jobs(workflow, paused_mode) for command in job_commands(workflow, job)]
    assert not any("scripts/test.sh" in command or "docker/build-push-action" in command or "docker push" in command for command in paused_commands)

    assert normal_mode == "NORMAL"
    assert "build-tested-artifact" in selected_jobs(workflow, normal_mode)
    normal_commands = job_commands(workflow, "build-tested-artifact")
    assert any("scripts/test.sh" in command for command in normal_commands)
    assert any("docker/build-push-action" in command for command in normal_commands)
    assert any("docker push" in command for command in normal_commands)


def test_paused_retained_probe_failure_fails_selected_workflow(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/pr-tests.yml").read_text(encoding="utf-8"))
    mode = run_selector(tmp_path, "select-pr", policy(), event("Closes #2503"))
    assert mode == "PAUSED"
    assert "retained-checks" in selected_jobs(workflow, mode)
    assert not {"tests-shard", "tests"}.intersection(selected_jobs(workflow, mode))

    binaries = tmp_path / "bin"
    binaries.mkdir()
    invocation_log = tmp_path / "invocations"
    (binaries / "uv").write_text(
        '#!/bin/sh\necho "uv $*" >> "$INVOCATION_LOG"\ncase "$*" in *"auto-coder --help"*) echo Auto-Coder;; esac\n',
        encoding="utf-8",
    )
    (binaries / "bash").write_text('#!/bin/sh\necho "bash $*" >> "$INVOCATION_LOG"\nexit 42\n', encoding="utf-8")
    (binaries / "uv").chmod(0o755)
    (binaries / "bash").chmod(0o755)
    environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "INVOCATION_LOG": str(invocation_log)}

    results = []
    for command in job_commands(workflow, "retained-checks"):
        if command.startswith("uses:"):
            continue
        result = subprocess.run(["/bin/bash", "-eo", "pipefail", "-c", command], env=environment, capture_output=True, text=True)
        results.append(result.returncode)
        if result.returncode:
            break

    assert results == [0, 0, 42]
    invocations = invocation_log.read_text(encoding="utf-8")
    assert "pr_lifecycle_contract tests/test_pr_lifecycle_contract.py" in invocations
    assert "run_pr_test_shard.py" not in invocations


def test_delayed_paused_publication_cannot_advance_after_resume(tmp_path):
    assert run_selector(tmp_path, "select-main", policy()) == "PAUSED"
    source_jobs = tmp_path / "paused-source-jobs.json"
    source_jobs.write_text(
        json.dumps({"jobs": [{"name": "Build tested artifact", "status": "completed", "conclusion": "skipped"}]}),
        encoding="utf-8",
    )
    assert run_selector(tmp_path, "select-main", policy(active=False)) == "NORMAL"

    guard = subprocess.run(
        [sys.executable, "-S", str(ROOT / "scripts/ci_maintenance.py"), "require-published-job", "--jobs", str(source_jobs)],
        capture_output=True,
        text=True,
    )
    advance = yaml.safe_load((ROOT / ".github/workflows/advance-beta.yml").read_text(encoding="utf-8"))
    commands = job_commands(advance, "advance-latest-successful")
    guard_index = next(index for index, command in enumerate(commands) if "require-published-job" in command)
    promotion_index = next(index for index, command in enumerate(commands) if "imagetools create" in command)

    assert guard.returncode != 0
    assert "lacks one successful 'Build tested artifact' job" in guard.stderr
    assert guard_index < promotion_index
