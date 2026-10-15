"""Permanent regressions for the normal CI and tested-beta publication path."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


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
