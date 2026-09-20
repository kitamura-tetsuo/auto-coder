"""Repository-scoped management commands for Issue review."""

from __future__ import annotations

import json
import platform
import uuid
from typing import Optional

import click

from .automation_config import AutomationConfig
from .automation_engine import AutomationEngine
from .cli_commands_utils import get_github_token_or_fail
from .issue_review_rerun_scope import IssueReviewRerunScopeResolver, RerunScope
from .llm_backend_config import (
    get_issue_decomposition_validation_from_config,
    get_issue_specification_validation_from_config,
)
from .lock_manager import LockManager
from .util.gh_cache import GitHubClient, is_implementation_ready


@click.group(name="review")
def review_group() -> None:
    """Manage durable semantic Issue reviews without deleting history."""


def _positive_number(_ctx: click.Context, param: click.Parameter, value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    if not value.isdecimal() or value.startswith("0") or int(value) <= 0:
        raise click.BadParameter("must be a positive decimal Issue number", param=param)
    return int(value)


def _blockers(
    scope: RerunScope,
    github: object,
    controller_running: bool,
    specification_enabled: bool,
    decomposition_enabled: bool,
) -> dict[str, list[str]]:
    snapshots = dict(scope.snapshots)
    readiness_authorities = dict(scope.readiness_authorities)
    result: dict[str, list[str]] = {}
    for subject in scope.subjects:
        reasons: list[str] = []
        authority_number = readiness_authorities[subject.key]
        if subject.kind == "individual":
            if not specification_enabled:
                reasons.append("issue_specification_validation is disabled")
        elif not decomposition_enabled:
            reasons.append("issue_decomposition_validation is disabled")
        authority = snapshots.get(authority_number)
        if authority is None:
            authority = github.get_issue_dispatch_snapshot_strict(subject.repository, authority_number)  # type: ignore[attr-defined]
        if not is_implementation_ready(authority):
            reasons.append(f"Issue #{authority_number} lacks implementation-ready")
        if not controller_running:
            reasons.append("controller is stopped; execution awaits controller startup")
        if reasons:
            result[subject.key] = reasons
    return result


def _controller_running() -> bool:
    manager = LockManager()
    info = manager.get_lock_info_obj()
    return info is not None and info.hostname == platform.node() and manager._is_process_running(info.pid)


@review_group.command(name="rerun")
@click.option("--repo", required=True, help="Repository in OWNER/REPO form.")
@click.option("--issue", callback=_positive_number, help="Open standalone Issue number.")
@click.option("--family", callback=_positive_number, help="Open parent Issue number; includes all retained direct children.")
@click.option("all_", "--all", is_flag=True, help="Select all open standalone Issues and open native families in this repository.")
@click.option("--dry-run", is_flag=True, help="Resolve and report scope and blockers without accepting or enqueueing a request.")
@click.option("--github-token", envvar="GITHUB_TOKEN", help="GitHub API token.")
def review_rerun(repo: str, issue: Optional[int], family: Optional[int], all_: bool, dry_run: bool, github_token: Optional[str]) -> None:
    """Clear reusable review authorization and queue a fresh review.

    Exactly one selector is required.  ``--family`` expands to decomposition
    review plus individual review of every native direct child, including
    closed retained children.  ``--all`` means every current open standalone
    Issue and open family in only the selected repository.  Acceptance keeps
    history and can remain deferred by readiness, category switches, or a
    stopped controller; it never means that review has completed.
    """
    if sum((issue is not None, family is not None, all_)) != 1:
        raise click.UsageError("exactly one of --issue, --family, or --all is required")
    token = get_github_token_or_fail(github_token)
    github = GitHubClient.get_instance(token)
    resolver = IssueReviewRerunScopeResolver(github, repo)
    try:
        scope = resolver.issue(issue) if issue is not None else resolver.family(family) if family is not None else resolver.all()
    except Exception as exc:
        raise click.ClickException(f"scope resolution failed without accepting a request: {exc}") from exc

    controller_running = _controller_running()
    specification_enabled = get_issue_specification_validation_from_config(repo_name=resolver.repository)
    decomposition_enabled = get_issue_decomposition_validation_from_config(repo_name=resolver.repository)
    blockers = _blockers(scope, github, controller_running, specification_enabled, decomposition_enabled)
    payload: dict[str, object] = {
        "repository": resolver.repository,
        "mode": "dry-run" if dry_run else "accepted",
        "subject_count": len(scope.subjects),
        "subjects": [subject.key for subject in scope.subjects],
        "exclusions": list(scope.exclusions),
        "deferred": blockers,
    }
    if dry_run:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    if not scope.subjects:
        payload["mode"] = "accepted-no-op"
        payload["request_id"] = None
        click.echo(json.dumps(payload, sort_keys=True))
        return
    request_id = f"review-rerun-{uuid.uuid4()}"
    payload["request_id"] = request_id
    try:
        config = AutomationConfig(repo_name=resolver.repository)
        config.issue_specification_validation = specification_enabled
        config.issue_decomposition_validation = decomposition_enabled
        engine = AutomationEngine(github, config=config)
        statuses = engine.accept_issue_review_rerun(request_id, scope.subjects)
    except Exception as exc:
        raise click.ClickException(f"durable acceptance is indeterminate for request {request_id}: {exc}") from exc
    payload["states"] = {status.subject.key: status.state for status in statuses}
    payload["deferred"] = {status.subject.key: [status.reason] for status in statuses if status.reason}
    click.echo(json.dumps(payload, sort_keys=True))
