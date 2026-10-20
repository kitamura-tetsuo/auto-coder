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

from auto_coder.automation_config import AutomationConfig
from auto_coder.llm_backend_config import TASK_ONLY_BACKEND_TYPES, get_llm_config, resolve_config_path
from auto_coder.pr_processor import is_local_llm_pr
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
    local_attributed: bool = True
    head_repository: str = ""
    base_repository: str = ""


@dataclass(frozen=True)
class ProcessingRecord:
    outcome: str
    backend: str
    execution_origin: str
    actions: tuple[str, ...]
    disposition: str
    invocation_id: str = ""
    repository: str = ""
    repair: dict[str, object] | None = None
    final_gate: dict[str, object] | None = None
    editing_invocations: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class FinalPRObservation:
    state: str
    merged: bool
    head: str
    base: str
    merge_commit: str


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
    repair_backend: str = ""
    repair_actions: tuple[str, ...] = ()
    ownership_disposition: str = ""
    gate_actions: tuple[str, ...] = ()
    configured_primary_backend: str = ""
    editing_invocations: tuple[dict[str, object], ...] = ()
    gate_exit_code: int | None = None


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
        pr_data = {
            "body": str(pr.body or ""),
            "head": {"ref": str(pr.head.ref)},
            "user": {"login": str(pr.user.login)},
        }
        return Snapshot(
            head,
            base,
            pr.mergeable,
            tuple(int(run.id) for run in candidates),
            conclusion,
            is_local_llm_pr(pr_data),
            str(getattr(getattr(pr.head, "repo", None), "full_name", "")),
            str(getattr(getattr(pr.base, "repo", None), "full_name", "")),
        )

    def final_pr(self, pr_number: int) -> FinalPRObservation:
        pr = self.api.pulls.get(self.owner, self.repo, pr_number)
        return FinalPRObservation(
            state=str(pr.state),
            merged=bool(pr.merged),
            head=str(pr.head.sha),
            base=str(pr.base.sha),
            merge_commit=str(pr.merge_commit_sha or ""),
        )


def validate_target(repository: str, workspace: Path) -> None:
    normalized = repository.strip().lower().removesuffix(".git")
    if normalized in FORBIDDEN_REPOSITORIES:
        raise Blocked("operator-designated repository must not be auto-coder or outliner")
    if len(repository.split("/")) != 2:
        raise Blocked("repository must be an owner/name pair")
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=workspace, text=True, capture_output=True, check=False)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != workspace:
        raise Blocked("workspace must be the top level of the designated disposable clone")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=workspace, text=True, capture_output=True, check=False)
    if status.returncode != 0:
        raise Blocked("git status failed for the designated workspace")
    if status.stdout.strip():
        raise Blocked("designated workspace has unrelated local changes")
    for key in ("remote.origin.url", "remote.origin.pushurl"):
        remote = subprocess.run(["git", "config", "--get", key], cwd=workspace, text=True, capture_output=True, check=False)
        if key.endswith("pushurl") and remote.returncode != 0:
            continue
        value = remote.stdout.strip().lower().removesuffix(".git")
        identity = value.split("github.com", 1)[-1].lstrip("/: ")
        if remote.returncode != 0 or identity != normalized:
            raise Blocked(f"workspace {key} does not identify the designated repository")


def require_initial_state(scenario: str, before: Snapshot, repository: str = "") -> None:
    if repository and (before.head_repository or before.base_repository) and (before.head_repository != repository or before.base_repository != repository):
        raise Blocked("fixture PR head and base must belong to the designated repository")
    if not before.local_attributed:
        raise Blocked("PR is missing supported local Auto-Coder attribution; add <!-- auto-coder:local-llm --> to its body")
    if scenario == "ci" and before.conclusion not in {"failure", "cancelled", "timed_out", "action_required"}:
        raise Blocked(f"CI scenario requires terminal-failing current-head Actions, observed {before.conclusion}")
    if scenario == "conflict" and before.mergeable is not False:
        raise Blocked("conflict scenario requires GitHub to report the PR as conflicting")


def verify_configured_local_backend(repository: str, requested_backend: str, config_path: str | None = None) -> None:
    """Require the requested backend to be the actual first ordinary producer."""
    config = get_llm_config(repo_name=repository, config_path=config_path)
    candidates = [name for group in config.get_ordinary_priority_groups() for name in group]
    if not candidates or candidates[0] != requested_backend:
        actual = candidates[0] if candidates else "none"
        raise Blocked(f"configured first ordinary backend is {actual!r}, not {requested_backend!r}")
    try:
        backend_type = config.resolve_backend_type(requested_backend)
    except ValueError as exc:
        raise Blocked(f"configured backend {requested_backend!r} is not resolvable: {exc}") from exc
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
        "--skip-codex-auth-secret-sync",
    ]
    if args.scenario == "conflict":
        command.append("--no-skip-main-update")
    return command


def run_production(command: list[str], workspace: Path, config_path: str) -> subprocess.CompletedProcess[str]:
    try:
        child_env = os.environ.copy()
        child_env["AUTO_CODER_CONFIG_PATH"] = config_path
        return subprocess.run(command, cwd=workspace, text=True, check=False, env=child_env)
    except FileNotFoundError as exc:
        raise Blocked("the auto-coder executable is unavailable") from exc


def read_processing_record(result_file: Path, pr_number: int) -> ProcessingRecord:
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Blocked("processing did not produce an authoritative result artifact") from exc
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise Blocked("processing result artifact has an unsupported schema")
    if result.get("target_number") != pr_number or result.get("target_type") != "pr":
        raise Blocked("processing result artifact target identity does not match the requested PR")
    outcome = result.get("target_outcome")
    backend = result.get("configured_primary_backend")
    origin = result.get("execution_origin")
    actions = result.get("target_actions")
    invocation_id = result.get("invocation_id")
    repository = result.get("repository")
    if not isinstance(outcome, str) or not isinstance(backend, str) or not isinstance(origin, str) or not isinstance(actions, list) or not all(isinstance(action, str) for action in actions) or not isinstance(invocation_id, str) or not invocation_id or not isinstance(repository, str):
        raise Blocked("processing result artifact is missing authoritative execution fields")
    reason = result.get("target_reason")
    repair = result.get("repair")
    final_gate = result.get("final_gate")
    invocations = result.get("editing_invocations")
    if repair is not None and not isinstance(repair, dict):
        raise Blocked("processing repair evidence has an invalid type")
    if final_gate is not None and not isinstance(final_gate, dict):
        raise Blocked("processing final-gate evidence has an invalid type")
    if not isinstance(invocations, list) or not all(isinstance(item, dict) for item in invocations):
        raise Blocked("processing editing-invocation evidence has an invalid type")
    return ProcessingRecord(outcome, backend, origin, tuple(actions), reason if isinstance(reason, str) else "", invocation_id, repository, repair, final_gate, tuple(invocations))


def repair_matches_run(record: ProcessingRecord, repository: str, pr_number: int, h1: str, h2: str, requested_backend: str) -> bool:
    repair = record.repair or {}
    witnessed_edit = any(item.get("backend_alias") == requested_backend and item.get("backend_type") not in TASK_ONLY_BACKEND_TYPES | {"unresolved"} and str(item.get("purpose", "")).startswith("local-") for item in record.editing_invocations)
    return (
        record.repository == repository
        and repair.get("repository") == repository
        and repair.get("pr_number") == pr_number
        and repair.get("input_head") == h1
        and repair.get("disposition") == "applied"
        and repair.get("attempt_started") is True
        and repair.get("resulting_head_state") == "available"
        and repair.get("resulting_head") == h2
        and repair.get("synchronization_state") == "available"
        and repair.get("synchronized") is True
        and witnessed_edit
    )


def gate_matches_run(record: ProcessingRecord, repository: str, pr_number: int, h2: str) -> bool:
    gate = record.final_gate or {}
    return record.repository == repository and gate.get("disposition") == "MERGE_COMPLETED" and gate.get("repository") == repository and gate.get("pr_number") == pr_number and gate.get("evaluated_head") == h2


def late_blocked_evidence(
    args: argparse.Namespace,
    before: Snapshot,
    after: Snapshot,
    repair: ProcessingRecord,
    repair_exit: int,
    phase: str,
    disposition: str,
    gate: ProcessingRecord | None = None,
    gate_exit: int | None = None,
) -> Evidence:
    gate = gate or ProcessingRecord("not_run", "", "", (), "")
    return Evidence(
        "BLOCKED",
        args.scenario,
        args.repository,
        args.pr,
        args.backend,
        repair.execution_origin,
        before.head,
        after.head,
        before.base,
        after.base,
        before.run_ids,
        before.conclusion,
        after.run_ids,
        after.conclusion,
        repair_exit,
        gate.outcome,
        "not observed",
        phase,
        disposition,
        repair.backend,
        repair.actions,
        "accepted local Auto-Coder attribution",
        gate.actions,
        repair.backend,
        repair.editing_invocations,
        gate_exit,
    )


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
    if args.evidence.resolve().is_relative_to(workspace):
        raise Blocked("evidence and processing artifacts must be outside the fixture workspace")
    config_path = str(Path(args.config_path).resolve())
    verify_configured_local_backend(args.repository, args.backend, config_path)
    test_script_path = AutomationConfig(repo_name=args.repository).TEST_SCRIPT_PATH
    test_script = workspace / test_script_path
    if not test_script.is_file():
        raise Blocked(f"configured TEST_SCRIPT_PATH is missing from fixture: {test_script_path}")
    observer = GitHubObserver(token, args.repository)
    before = observer.snapshot(args.pr)
    require_initial_state(args.scenario, before, args.repository)
    repair_file = args.evidence.with_suffix(".repair-result.json")
    gate_file = args.evidence.with_suffix(".gate-result.json")
    repair_file.unlink(missing_ok=True)
    gate_file.unlink(missing_ok=True)
    command = production_command(args, repair_file)
    process = run_production(command, workspace, config_path)
    try:
        repair = read_processing_record(repair_file, args.pr)
    except Blocked as exc:
        return late_blocked_evidence(args, before, before, ProcessingRecord("missing", "", "", (), ""), process.returncode, "repair_result", str(exc))
    repair_data = repair.repair or {}
    if process.returncode != 0 or repair_data.get("attempt_started") is not True or repair_data.get("disposition") != "applied":
        return late_blocked_evidence(args, before, before, repair, process.returncode, "repair_result", "processing did not establish an applied local repair")
    after = wait_for_result(observer, args.pr, before.head, args.timeout)
    gate_exit: int | None = None
    gate = ProcessingRecord("not_run", "", "", (), "")
    if process.returncode == 0 and after.head != before.head and after.conclusion == "success":
        # A repair invocation cannot authorize itself from the CI result that
        # appears later. Re-enter ordinary processing so the independent
        # current-head review/merge gates consume GitHub's H2 observation.
        gate_command = production_command(args, gate_file)
        try:
            gate_exit = run_production(gate_command, workspace, config_path).returncode
            gate = read_processing_record(gate_file, args.pr)
        except Blocked as exc:
            return late_blocked_evidence(args, before, after, repair, process.returncode, "independent_review_or_merge", str(exc), gate, gate_exit)
    try:
        final_pr = observer.final_pr(args.pr)
    except GitHubRequestError:
        return late_blocked_evidence(args, before, after, repair, process.returncode, "final_merge_observation", "final merge state is inaccessible with the supplied credential", gate, gate_exit)
    changed = after.head != before.head
    ci_passed = after.conclusion == "success"
    repair_valid = repair_matches_run(repair, args.repository, args.pr, before.head, after.head, args.backend)
    gate_valid = gate_matches_run(gate, args.repository, args.pr, after.head)
    final_valid = final_pr.merged and final_pr.head == after.head and final_pr.state == "closed"
    passed = process.returncode == 0 and changed and ci_passed and gate_exit == 0 and gate.outcome == "success" and repair_valid and gate_valid and final_valid
    phase = "" if passed else ("repair" if process.returncode else "publication_or_current_head_ci")
    if changed and ci_passed and (gate_exit not in (None, 0) or gate.outcome not in {"not_run", "success"}):
        phase = "independent_review_or_merge"
    return Evidence(
        "PASSED" if passed else "FAILED",
        args.scenario,
        args.repository,
        args.pr,
        args.backend,
        repair.execution_origin,
        before.head,
        after.head,
        before.base,
        after.base,
        before.run_ids,
        before.conclusion,
        after.run_ids,
        after.conclusion,
        process.returncode,
        gate.outcome,
        json.dumps(asdict(final_pr), sort_keys=True),
        phase,
        "new head and current-head Actions success observed" if passed else "live gate not satisfied",
        repair.backend,
        repair.actions,
        "accepted local Auto-Coder attribution",
        gate.actions,
        repair.backend,
        repair.editing_invocations,
        gate_exit,
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
    args.evidence = args.evidence.resolve()
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.config_path = resolve_config_path()
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
