from pathlib import Path

import yaml


def test_github_actions_ci_workflow_exists_and_has_required_jobs():
    wf_path = Path(".github/workflows/ci.yml")
    assert wf_path.exists(), "CI workflow file must exist at .github/workflows/ci.yml"

    content = wf_path.read_text()
    data = yaml.safe_load(content)

    # Basic structure
    assert data.get("name") == "CI"
    assert "on" in data and (
        "pull_request" in data["on"] or "pull_request" in (data["on"] or {})
    ), "workflow must run on pull_request"

    jobs = data.get("jobs") or {}
    assert "lint" in jobs, "lint job required"
    assert "tests" in jobs, "tests job required"

    # Lint job checks
    lint = jobs["lint"]
    assert lint.get("runs-on") == "ubuntu-latest"
    lint_steps = lint.get("steps") or []
    steps_uses = [s.get("uses") for s in lint_steps if "uses" in s]
    assert "actions/checkout@v4" in steps_uses
    assert "actions/setup-python@v5" in steps_uses

    # Ensure commands include checks we expect
    run_cmds = "\n".join([s.get("run", "") for s in lint_steps if "run" in s])
    assert "black --check" in run_cmds
    assert "isort --check-only" in run_cmds
    assert "flake8" in run_cmds
    assert "mypy" in run_cmds

    # Tests job checks
    tests = jobs["tests"]
    assert tests.get("runs-on") == "ubuntu-latest"
    matrix = ((tests.get("strategy") or {}).get("matrix")) or {}
    pyvers = matrix.get("python-version") or []
    assert any(
        v in ("3.11", "3.12") for v in pyvers
    ), "matrix must include modern Python versions"
    test_steps = tests.get("steps") or []
    test_run_cmds = "\n".join([s.get("run", "") for s in test_steps if "run" in s])
    assert "pytest" in test_run_cmds
