from pathlib import Path

import pytest
from scripts.node24_policy import (
    development_dockerfile_policy_errors,
    repository_policy_errors,
    workflow_policy_errors,
)

REPOSITORY = Path(__file__).parents[1]


def test_repository_development_and_ci_configs_follow_node24_policy() -> None:
    assert repository_policy_errors(REPOSITORY) == []

    dockerfile = (REPOSITORY / ".autocoder-dev" / "Dockerfile").read_text(encoding="utf-8")
    for retained_tool in ("python3.12", "docker-ce-cli", "gh", "uv pip install"):
        assert retained_tool in dockerfile


def test_development_dockerfile_policy_rejects_nodesource_20(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("RUN curl https://deb.nodesource.com/setup_20.x | bash -\n", encoding="utf-8")

    assert development_dockerfile_policy_errors(dockerfile) != []


@pytest.mark.parametrize(
    "steps",
    [
        "      - run: npm install\n",
        "      - uses: actions/setup-node@v7\n      - run: npm install\n",
        "      - uses: actions/setup-node@v7\n        with:\n          node-version: '22'\n      - run: npm install\n",
        "      - run: npm install\n      - uses: actions/setup-node@v7\n        with:\n          node-version: '24'\n",
    ],
    ids=("missing-setup", "missing-version", "wrong-major", "setup-after-consumer"),
)
def test_workflow_policy_rejects_invalid_setup_in_consuming_job(tmp_path: Path, steps: str) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text("jobs:\n  consumer:\n    steps:\n" + steps, encoding="utf-8")

    assert workflow_policy_errors(workflow, tmp_path) != []


def test_workflow_policy_rejects_setup_only_in_another_job(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        """jobs:
  setup-job:
    steps:
      - uses: actions/setup-node@v7
        with:
          node-version: '24'
  consumer:
    steps:
      - run: npx example
""",
        encoding="utf-8",
    )

    assert workflow_policy_errors(workflow, tmp_path) != []


@pytest.mark.parametrize("script_name", ["runner.py", "runner"])
def test_workflow_policy_detects_node_use_inside_invoked_script(tmp_path: Path, script_name: str) -> None:
    (tmp_path / script_name).write_text('subprocess.run(["npx", "example"])\n', encoding="utf-8")
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(f"jobs:\n  consumer:\n    steps:\n      - run: ./{script_name}\n", encoding="utf-8")

    assert workflow_policy_errors(workflow, tmp_path) != []


def test_workflow_policy_accepts_node24_before_script_invocation(tmp_path: Path) -> None:
    (tmp_path / "runner.py").write_text('subprocess.run(["npx", "example"])\n', encoding="utf-8")
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        """jobs:
  consumer:
    steps:
      - uses: actions/setup-node@v7
        with:
          node-version: '24'
      - run: python runner.py
""",
        encoding="utf-8",
    )

    assert workflow_policy_errors(workflow, tmp_path) == []
