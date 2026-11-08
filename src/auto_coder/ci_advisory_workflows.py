"""Exact-path classification of non-actionable diagnostic CI workflows."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

ADVISORY_CI_WORKFLOW_PATHS = frozenset(
    {
        ".github/workflows/prompt-regression.yml",
        ".github/workflows/prompt-regression-report.yml",
        ".github/workflows/test-impact-observation.yml",
    }
)


def workflow_path(value: object) -> str:
    """Strip only GitHub's optional ref suffix from a workflow file path."""
    return value.split("@", 1)[0] if isinstance(value, str) else ""


def is_advisory_ci_run(run: object) -> bool:
    return isinstance(run, dict) and workflow_path(run.get("path")) in ADVISORY_CI_WORKFLOW_PATHS


def associated_actions_run_id(observation: object, repository: str) -> int | None:
    """Get a run ID only from a same-repository GitHub Actions URL.

    A check's own numeric ID is not a workflow run ID. Unknown/foreign URL
    provenance must remain actionable, even if its display name collides.
    """
    if not isinstance(observation, dict):
        return None
    for key in ("details_url", "html_url", "url"):
        value = observation.get(key)
        if not isinstance(value, str):
            continue
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc != "github.com":
            continue
        pattern = rf"/{re.escape(repository)}/actions/runs/([1-9][0-9]*)(?:/job/[1-9][0-9]*)?/?"
        match = re.fullmatch(pattern, parsed.path)
        if match:
            return int(match.group(1))
    return None
