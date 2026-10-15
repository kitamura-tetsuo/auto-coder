"""Permanent regressions for the normal CI and tested-beta publication path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _run_publish_beta_steps(tmp_path: Path, *, tests_pass: bool) -> list[list[str]]:
    """Execute the workflow's real run blocks with controlled external effects."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-beta.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build-tested-artifact"]["steps"]
    effects = tmp_path / "effects.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = """#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ["EFFECT_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "bash" and sys.argv[1:3] == ["scripts/test.sh", "-m"]:
    raise SystemExit(0 if os.environ["TESTS_PASS"] == "1" else 23)
if name == "bash":
    raise SystemExit(subprocess.call(["/bin/bash", *sys.argv[1:]]))
if name == "uv" and "resolve_build_identity" in " ".join(sys.argv):
    Path(".agent-tmp").mkdir(exist_ok=True)
    Path(".agent-tmp/build-identity.json").write_text(json.dumps({"version": "1.0"}))
if name == "uv" and "json.load" in " ".join(sys.argv): print("1.0")
if name == "uv": raise SystemExit(0)
if name == "docker" and "build" in sys.argv: raise SystemExit(0)
if name == "docker" and "importlib.metadata" in " ".join(sys.argv): print("1.0")
elif name == "docker" and "--version" in sys.argv: print("auto-coder, version 1.0")
elif name == "docker" and "build_provenance" in " ".join(sys.argv): print(json.dumps({"distribution_version":{"value":"1.0"},"source_revision":{"value":os.environ["GITHUB_SHA"]}}))
elif name == "docker" and "inspect" in sys.argv: print(os.environ["GITHUB_SHA"])
raise SystemExit(0)
"""
    for name in ("bash", "uv", "docker"):
        executable = bin_dir / name
        executable.write_text(shim, encoding="utf-8")
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "EFFECT_LOG": str(effects),
        "TESTS_PASS": "1" if tests_pass else "0",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_ENV": str(tmp_path / "github-env"),
    }
    for step in steps:
        if step.get("uses") == "docker/build-push-action@v7":
            subprocess.run(["docker", "build"], cwd=ROOT, env=env, check=True)
        if "run" not in step:
            continue
        command = step["run"].replace("${{ github.sha }}", env["GITHUB_SHA"]).replace("${{ github.repository }}", "owner/repo")
        result = subprocess.run(["/bin/bash", "-Eeuo", "pipefail", "-c", command], cwd=ROOT, env=env)
        if result.returncode:
            break
    return [json.loads(line) for line in effects.read_text(encoding="utf-8").splitlines()]


def test_pr_workflow_has_one_unconditional_normal_profile() -> None:
    text = (ROOT / ".github/workflows/pr-tests.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)

    assert set(workflow["jobs"]) == {"lint", "tests-shard", "tests"}
    shard = workflow["jobs"]["tests-shard"]
    assert shard["strategy"] == {"fail-fast": False, "matrix": {"group": [1, 2, 3, 4]}}
    assert "if" not in shard and "needs" not in shard
    assert workflow["jobs"]["tests"]["needs"] == "tests-shard"
    assert workflow["jobs"]["tests"]["if"] == "always()"
    assert "continue-on-error" not in text
    assert "ci-maintenance" not in text


def test_publish_beta_always_enters_tested_build_path() -> None:
    text = (ROOT / ".github/workflows/publish-beta.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)

    assert set(workflow["jobs"]) == {"build-tested-artifact"}
    job = workflow["jobs"]["build-tested-artifact"]
    assert "if" not in job and "needs" not in job
    actions = [step.get("run") or step.get("uses") for step in job["steps"]]
    test_index = next(index for index, action in enumerate(actions) if action and "scripts/test.sh" in action)
    build_index = next(index for index, action in enumerate(actions) if action and "docker/build-push-action" in action)
    push_index = next(index for index, action in enumerate(actions) if action and "docker push" in action)
    assert test_index < build_index < push_index


def test_publish_beta_commands_stop_all_build_and_publication_effects_on_test_failure(tmp_path: Path) -> None:
    effects = _run_publish_beta_steps(tmp_path, tests_pass=False)

    assert ["bash", "scripts/test.sh", "-m", "not opencode_live"] in effects
    assert not any(effect[:2] == ["docker", "build"] for effect in effects)
    assert not any(effect[:2] == ["docker", "push"] for effect in effects)


def test_publish_beta_commands_reach_build_and_publication_after_tests_pass(tmp_path: Path) -> None:
    effects = _run_publish_beta_steps(tmp_path, tests_pass=True)

    test_index = effects.index(["bash", "scripts/test.sh", "-m", "not opencode_live"])
    build_index = effects.index(["docker", "build"])
    push_index = next(index for index, effect in enumerate(effects) if effect[:2] == ["docker", "push"])
    assert test_index < build_index < push_index


def test_delayed_paused_run_cannot_advance_beta(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps({"jobs": [{"name": "Build tested artifact", "status": "completed", "conclusion": "skipped"}]}),
        encoding="utf-8",
    )
    script = ROOT / "scripts/deployment_artifacts.py"
    result = subprocess.run(
        [sys.executable, str(script), "require-successful-job", str(jobs), "Build tested artifact"],
        capture_output=True,
        text=True,
    )
    advance = (ROOT / ".github/workflows/advance-beta.yml").read_text(encoding="utf-8")

    assert result.returncode != 0
    assert "lacks one successful 'Build tested artifact' job" in result.stderr
    assert "require-successful-job" in advance
    assert advance.index("require-successful-job") < advance.index("imagetools create")


def test_delayed_paused_run_stops_actual_advance_steps_before_registry_write(tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/advance-beta.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["advance-latest-successful"]["steps"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    (bin_dir / "gh").write_text(
        '#!/bin/sh\nprintf \'%s\\n\' \'{"jobs":[{"name":"Build tested artifact","status":"completed","conclusion":"skipped"}]}\'\n',
        encoding="utf-8",
    )
    (bin_dir / "docker").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {docker_log}\n", encoding="utf-8")
    for executable in (bin_dir / "gh", bin_dir / "docker"):
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "RUN_ID": "historical-paused-run",
        "IMAGE": "ghcr.io/owner/repo",
        "SOURCE_SHA": "b" * 40,
    }
    results = []
    for step in steps:
        if "run" not in step:
            continue
        command = step["run"].replace("${{ github.repository }}", "owner/repo")
        result = subprocess.run(["/bin/bash", "-Eeuo", "pipefail", "-c", command], cwd=ROOT, env=env)
        results.append(result.returncode)
        if result.returncode:
            break

    assert results == [1]
    assert not docker_log.exists()


def test_current_successful_publish_run_is_accepted(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps({"jobs": [{"name": "Build tested artifact", "status": "completed", "conclusion": "success"}]}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/deployment_artifacts.py"),
            "require-successful-job",
            str(jobs),
            "Build tested artifact",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
