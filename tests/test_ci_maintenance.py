import importlib.util
import json
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
