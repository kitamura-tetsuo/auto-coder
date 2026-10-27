"""Validate the repository's Node.js 24 development and CI policy."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml


_NODE_COMMAND = re.compile(r"(?<![\w.-])(?:npm|npx)(?![\w.-])")


def _referenced_scripts(command: str, repository: Path) -> set[Path]:
    """Return repository files named by a workflow command.

    Checked-in executable scripts do not have to use a conventional suffix, so
    file existence and repository confinement are the relevant boundaries.
    """
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        words = command.split()

    scripts: set[Path] = set()
    for word in words:
        candidate = word.strip("'\"").rstrip(";,)]")
        path = (repository / candidate).resolve()
        if path.is_relative_to(repository.resolve()) and path.is_file():
            scripts.add(path)
    return scripts


def step_consumes_node(step: object, repository: Path) -> bool:
    """Whether a workflow step runs npm/npx itself or via a named script."""
    if not isinstance(step, dict):
        return False
    command = step.get("run")
    if not isinstance(command, str):
        return False
    if _NODE_COMMAND.search(command):
        return True
    return any(
        _NODE_COMMAND.search(script.read_text(encoding="utf-8"))
        for script in _referenced_scripts(command, repository)
    )


def _is_node24_setup(step: object) -> bool:
    if not isinstance(step, dict):
        return False
    action = step.get("uses")
    options = step.get("with")
    if not isinstance(action, str) or not action.startswith("actions/setup-node@"):
        return False
    if not isinstance(options, dict):
        return False
    version = options.get("node-version")
    return isinstance(version, (str, int, float)) and str(version).split(".", 1)[0] == "24"


def workflow_policy_errors(workflow: Path, repository: Path) -> list[str]:
    """Find npm/npx-consuming jobs without an earlier Node.js 24 setup."""
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        return [f"{workflow}: workflow must be a mapping"]
    jobs = document.get("jobs", {})
    if not isinstance(jobs, dict):
        return [f"{workflow}: jobs must be a mapping"]

    errors: list[str] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        node24_ready = False
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        for position, step in enumerate(steps, start=1):
            if _is_node24_setup(step):
                node24_ready = True
            if step_consumes_node(step, repository) and not node24_ready:
                errors.append(
                    f"{workflow}: job {job_name!r} step {position} consumes npm/npx "
                    "before that job provisions Node.js 24"
                )
    return errors


def development_dockerfile_policy_errors(dockerfile: Path) -> list[str]:
    """Require NodeSource 24 and explicitly reject the retired 20 installer."""
    docker_text = dockerfile.read_text(encoding="utf-8")
    if "setup_20.x" in docker_text or "setup_24.x" not in docker_text:
        return [f"{dockerfile}: must use NodeSource setup_24.x and reject setup_20.x"]
    return []


def repository_policy_errors(repository: Path) -> list[str]:
    """Validate the development Dockerfile and every active workflow."""
    dockerfile = repository / ".autocoder-dev" / "Dockerfile"
    errors = development_dockerfile_policy_errors(dockerfile)

    workflow_dir = repository / ".github" / "workflows"
    for pattern in ("*.yml", "*.yaml"):
        for workflow in sorted(workflow_dir.glob(pattern)):
            errors.extend(workflow_policy_errors(workflow, repository))
    return errors
