#!/usr/bin/env python3
"""Fail-closed selector for the temporary issue-2500 CI maintenance window."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

POLICY_PATH = Path(".github/ci-maintenance.json")
CLOSING_REFERENCE = re.compile(
    r"(?im)(?:^|\s)(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:https://github\.com/([^/\s]+)/([^/\s]+)/issues/|#)(\d+)\b"
)


@dataclass(frozen=True)
class Selection:
    mode: str = "NORMAL"
    reason: str = "maintenance scope did not match"
    repository: str = "unknown"
    pr_number: str = "none"
    head: str = "unknown"


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_policy(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    return (
        raw.get("schema_version") == 1
        and raw.get("repository") == "kitamura-tetsuo/auto-coder"
        and raw.get("parent_issue") == 2500
        and isinstance(raw.get("active"), bool)
        and isinstance(raw.get("reason"), str)
        and bool(raw.get("reason", "").strip())
        and raw.get("core_issues") == [2503, 2504, 2505, 2506]
        and isinstance(raw.get("associated_pull_requests"), list)
        and all(isinstance(number, int) and number > 0 for number in raw["associated_pull_requests"])
        and raw.get("restoration_issue") == 2507
    )


def _closing_issues(body: str, repository: str) -> set[int]:
    owner, repo = repository.split("/", 1)
    result: set[int] = set()
    for match in CLOSING_REFERENCE.finditer(body):
        reference_owner, reference_repo, number = match.groups()
        if reference_owner is None or (reference_owner == owner and reference_repo == repo):
            result.add(int(number))
    return result


def select(event: object, policy: object) -> Selection:
    if not isinstance(event, dict) or not _valid_policy(policy):
        return Selection(reason="policy or event evidence is absent or invalid")
    repository = str(event.get("repository", {}).get("full_name", "unknown"))
    pull = event.get("pull_request")
    if not policy["active"]:
        return Selection(repository=repository, reason="maintenance policy is inactive")
    if repository != policy["repository"] or not isinstance(pull, dict):
        return Selection(repository=repository, reason="event is not an in-scope pull request")
    head_repo = str(pull.get("head", {}).get("repo", {}).get("full_name", ""))
    number = pull.get("number")
    head = str(pull.get("head", {}).get("sha", "unknown"))
    identity = str(number) if isinstance(number, int) else "none"
    if head_repo != repository or not isinstance(number, int):
        return Selection(repository=repository, pr_number=identity, head=head, reason="fork or invalid pull request identity")
    issues = _closing_issues(str(pull.get("body") or ""), repository)
    if policy["restoration_issue"] in issues:
        return Selection(repository=repository, pr_number=identity, head=head, reason="restoration issue takes precedence")
    eligible = number in policy["associated_pull_requests"] or bool(issues.intersection(policy["core_issues"]))
    if not eligible:
        return Selection(repository=repository, pr_number=identity, head=head)
    return Selection("PAUSED", policy["reason"], repository, identity, head)


def _write_selection(selection: Selection, policy: dict[str, object]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"mode={selection.mode}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [
            "## CI maintenance selection",
            f"- Mode: **{selection.mode}**",
            f"- Repository / PR / head: `{selection.repository}` / `{selection.pr_number}` / `{selection.head}`",
            f"- Parent: `#{policy.get('parent_issue', 'unavailable')}`",
            f"- Stopped workload: {policy.get('stopped_workload', 'none') if selection.mode == 'PAUSED' else 'none'}",
            f"- Retained checks: {policy.get('retained_checks', 'normal workflow')}",
            f"- Reason: {selection.reason}",
            f"- Restoration target: `#{policy.get('restoration_issue', 'unavailable')}`",
        ]
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def require_published_job(jobs: object, expected_name: str) -> bool:
    if not isinstance(jobs, dict) or not isinstance(jobs.get("jobs"), list):
        return False
    matches = [job for job in jobs["jobs"] if isinstance(job, dict) and job.get("name") == expected_name]
    return len(matches) == 1 and matches[0].get("status") == "completed" and matches[0].get("conclusion") == "success"


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    selector = subcommands.add_parser("select-pr")
    selector.add_argument("--event", type=Path, required=True)
    selector.add_argument("--policy", type=Path, default=POLICY_PATH)
    main_selector = subcommands.add_parser("select-main")
    main_selector.add_argument("--policy", type=Path, default=POLICY_PATH)
    attestation = subcommands.add_parser("require-published-job")
    attestation.add_argument("--jobs", type=Path, required=True)
    attestation.add_argument("--name", default="Build tested artifact")
    args = parser.parse_args()
    try:
        if args.command in {"select-pr", "select-main"}:
            policy = _load_json(args.policy)
            if args.command == "select-pr":
                selection = select(_load_json(args.event), policy)
            else:
                paused = _valid_policy(policy) and policy["active"]
                selection = Selection("PAUSED" if paused else "NORMAL", policy.get("reason", "policy is invalid or inactive"), policy.get("repository", "unknown")) if isinstance(policy, dict) else Selection(reason="policy is invalid")
            _write_selection(selection, policy if isinstance(policy, dict) else {})
            print(selection.mode)
            return 0
        if require_published_job(_load_json(args.jobs), args.name):
            return 0
        logger.error("Exact source run lacks one successful {!r} job", args.name)
        return 1
    except (OSError, ValueError, TypeError) as error:
        logger.error("CI maintenance evidence unavailable: {}", error)
        if args.command == "require-published-job":
            return 1
        _write_selection(Selection(reason="policy or event evidence is unavailable"), {})
        print("NORMAL")
        return 0


if __name__ == "__main__":
    sys.exit(main())
