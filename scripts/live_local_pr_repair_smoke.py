#!/usr/bin/env python3
"""Opt-in observer for live local PR repair smoke runs.

This program deliberately does not create repositories, alter protections, or
seed credentials.  It runs Auto-Coder only after proving that the explicitly
named PR is in the requested initial state, then writes redacted evidence from
GitHub's API.  See the companion runbook before use.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from auto_coder.llm_backend_config import TASK_ONLY_BACKEND_TYPES, get_llm_config
from auto_coder.util.gh_cache import get_ghapi_client
from auto_coder.util.github_request_outcome import GitHubRequestError

FORBIDDEN_REPOSITORIES = frozenset({"kitamura-tetsuo/auto-coder", "kitamura-tetsuo/outliner"})
TERMINAL = frozenset({"success", "failure", "cancelled", "timed_out", "action_required", "neutral", "skipped", "stale"})
PASSING = frozenset({"success", "neutral", "skipped"})


class Blocked(RuntimeError):
    """A missing live prerequisite; never represents a successful smoke."""


@dataclass(frozen=True)
class Snapshot:
    head: str
    base: str
    mergeable: bool | None
    run_ids: tuple[int, ...]
    conclusion: str


@dataclass(frozen=True)
class Evidence:
    status: str
    scenario: str
    repository: str
    pr_number: int
    backend: str
    execution_origin: str
    captured_head: str
    resulting_head: str
    captured_base: str
    resulting_base: str
    pre_ci_run_ids: tuple[int, ...]
    pre_ci_conclusion: str
    post_ci_run_ids: tuple[int, ...]
    post_ci_conclusion: str
    repair_exit_code: int | None
    gate_outcome: str
    review_merge_result: str
    failure_phase: str
    disposition: str


class GitHubObserver:
    def __init__(self, token: str, repository: str) -> None:
        self.api = get_ghapi_client(token, subsystem="live-local-pr-repair-smoke", extra_headers={"Cache-Control": "no-cache"})
        self.owner, self.repo = repository.split("/", 1)

    def snapshot(self, pr_number: int) -> Snapshot:
        pr = self.api.pulls.get(self.owner, self.repo, pr_number)
        head = str(pr.head.sha)
        base = str(pr.base.sha)
        runs = self.api.actions.list_workflow_runs_for_repo(self.owner, self.repo, head_sha=head, per_page=100)
        candidates = [run for run in runs.workflow_runs if str(run.head_sha) == head and str(run.event) in {"pull_request", "pull_request_target"}]
        candidates.sort(key=lambda run: str(run.created_at), reverse=True)
        conclusions = tuple(str(run.conclusion or run.status or "unknown") for run in candidates)
        if not conclusions:
            conclusion = "KNOWN_EMPTY"
        elif any(value not in TERMINAL for value in conclusions):
            conclusion = "pending"
        elif all(value in PASSING for value in conclusions):
            conclusion = "success"
        else:
            conclusion = next(value for value in conclusions if value not in PASSING)
        return Snapshot(head, base, pr.mergeable, tuple(int(run.id) for run in candidates), conclusion)

    def merge_state(self, pr_number: int) -> str:
        pr = self.api.pulls.get(self.owner, self.repo, pr_number)
        if bool(pr.merged):
            return "merged"
        return f"open:{str(pr.mergeable_state or 'unknown')}"


def validate_target(repository: str, workspace: Path) -> None:
    if repository in FORBIDDEN_REPOSITORIES:
        raise Blocked("operator-designated repository must not be auto-coder or outliner")
    if len(repository.split("/")) != 2:
        raise Blocked("repository must be an owner/name pair")
    if not workspace.is_dir() or not (workspace / ".git").exists():
        raise Blocked("workspace must be an existing clone of the designated disposable repository")
    if subprocess.run(["git", "status", "--porcelain"], cwd=workspace, text=True, capture_output=True, check=False).stdout.strip():
        raise Blocked("designated workspace has unrelated local changes")


def require_initial_state(scenario: str, before: Snapshot) -> None:
    if scenario == "ci" and before.conclusion not in {"failure", "cancelled", "timed_out", "action_required"}:
        raise Blocked(f"CI scenario requires terminal-failing current-head Actions, observed {before.conclusion}")
    if scenario == "conflict" and before.mergeable is not False:
        raise Blocked("conflict scenario requires GitHub to report the PR as conflicting")


def verify_configured_local_backend(repository: str, requested_backend: str) -> None:
    """Require the requested backend to be the actual first ordinary producer."""
    config = get_llm_config(repo_name=repository)
    candidates = [name for group in config.get_ordinary_priority_groups() for name in group]
    if not candidates or candidates[0] != requested_backend:
        actual = candidates[0] if candidates else "none"
        raise Blocked(f"configured first ordinary backend is {actual!r}, not {requested_backend!r}")
    backend = config.get_backend_config(requested_backend)
    backend_type = (backend.backend_type if backend else None) or requested_backend
    if backend_type in TASK_ONLY_BACKEND_TYPES:
        raise Blocked(f"configured backend {requested_backend!r} is not a synchronous local editing backend")


def production_command(args: argparse.Namespace, result_file: Path) -> list[str]:
    command = [
        "auto-coder",
        "process-issues",
        "--repo",
        args.repository,
        "--only",
        str(args.pr),
        "--disable-webhook",
        "--no-jules-mode",
        "--result-file",
        str(result_file),
    ]
    if args.scenario == "conflict":
        command.append("--no-skip-main-update")
    return command


def run_production(command: list[str], workspace: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=workspace, text=True, check=False)
    except FileNotFoundError as exc:
        raise Blocked("the auto-coder executable is unavailable") from exc


def read_gate_outcome(result_file: Path, pr_number: int) -> str:
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Blocked("independent processing did not produce an authoritative result artifact") from exc
    if result.get("target_number") != pr_number or result.get("target_type") != "pr":
        return "invalid"
    outcome = result.get("target_outcome")
    return outcome if isinstance(outcome, str) else "invalid"


def wait_for_result(observer: GitHubObserver, pr_number: int, old_head: str, timeout: int, sleep: Callable[[float], None] = time.sleep) -> Snapshot:
    deadline = time.monotonic() + timeout
    newest = observer.snapshot(pr_number)
    while time.monotonic() < deadline:
        newest = observer.snapshot(pr_number)
        if newest.head != old_head and newest.conclusion in TERMINAL:
            return newest
        sleep(10)
    return newest


def run_smoke(args: argparse.Namespace) -> Evidence:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise Blocked("GITHUB_TOKEN is missing")
    workspace = Path(args.workspace).resolve()
    validate_target(args.repository, workspace)
    verify_configured_local_backend(args.repository, args.backend)
    observer = GitHubObserver(token, args.repository)
    before = observer.snapshot(args.pr)
    require_initial_state(args.scenario, before)
    result_file = args.evidence.with_suffix(".processing-result.json")
    result_file.unlink(missing_ok=True)
    command = production_command(args, result_file)
    process = run_production(command, workspace)
    after = wait_for_result(observer, args.pr, before.head, args.timeout)
    gate_exit: int | None = None
    gate_outcome = "not_run"
    if process.returncode == 0 and after.head != before.head and after.conclusion == "success":
        # A repair invocation cannot authorize itself from the CI result that
        # appears later. Re-enter ordinary processing so the independent
        # current-head review/merge gates consume GitHub's H2 observation.
        result_file.unlink(missing_ok=True)
        gate_exit = run_production(command, workspace).returncode
        gate_outcome = read_gate_outcome(result_file, args.pr)
    merge_result = observer.merge_state(args.pr)
    changed = after.head != before.head
    ci_passed = after.conclusion == "success"
    passed = process.returncode == 0 and changed and ci_passed and gate_exit == 0 and gate_outcome == "success"
    phase = "" if passed else ("repair" if process.returncode else "publication_or_current_head_ci")
    if changed and ci_passed and (gate_exit not in (None, 0) or gate_outcome not in {"not_run", "success"}):
        phase = "independent_review_or_merge"
    return Evidence(
        "PASSED" if passed else "FAILED",
        args.scenario,
        args.repository,
        args.pr,
        args.backend,
        "auto-coder process-issues --only (synchronous local)",
        before.head,
        after.head,
        before.base,
        after.base,
        before.run_ids,
        before.conclusion,
        after.run_ids,
        after.conclusion,
        process.returncode if gate_exit in (None, 0) else gate_exit,
        gate_outcome,
        merge_result,
        phase,
        "new head and current-head Actions success observed" if passed else "live gate not satisfied",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=("ci", "conflict"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    try:
        evidence = run_smoke(args)
    except (Blocked, GitHubRequestError) as exc:
        disposition = str(exc) if isinstance(exc, Blocked) else "designated GitHub repository or PR is inaccessible with the supplied credential"
        evidence = Evidence("BLOCKED", args.scenario, args.repository, args.pr, args.backend, "not started", "", "", "", "", (), "", (), "", None, "not_run", "not observed", "preflight", disposition)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(asdict(evidence), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{evidence.status}: {evidence.disposition}")
    return 0 if evidence.status == "PASSED" else 2


if __name__ == "__main__":
    sys.exit(main())
