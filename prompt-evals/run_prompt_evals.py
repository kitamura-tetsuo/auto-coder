#!/usr/bin/env python3
"""Select affected Promptfoo targets and produce an advisory execution record."""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROMPTFOO_VERSION = "0.122.2"
INFRASTRUCTURE_PATHS = {
    ".github/workflows/prompt-regression.yml",
    ".github/workflows/prompt-regression-report.yml",
    "prompt-evals/registry.json",
    "prompt-evals/run_prompt_evals.py",
    "prompt-evals/report_prompt_evals.py",
}


class SelectionError(RuntimeError):
    """Raised when affected targets cannot be determined safely."""


@dataclass
class TargetRecord:
    id: str
    outcome: str = "NOT_RUN"
    reason: str = "not reached"
    expected_cases: int = 0
    executed_cases: int = 0
    failures: list[dict[str, object]] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if result.returncode:
        raise SelectionError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def resolve_revision(repo: Path, revision: str, label: str) -> str:
    if not revision:
        raise SelectionError(f"{label} revision is empty")
    sha = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if len(sha) != 40:
        raise SelectionError(f"{label} revision did not resolve to a full commit SHA")
    return sha


def _value_at(document: object, dotted_key: str) -> object:
    if isinstance(document, dict) and dotted_key in document:
        return document[dotted_key]
    value = document
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return {"__prompt_eval_missing__": True}
        value = value[part]
    return value


def _glob_matches(path: str, pattern: str) -> bool:
    path_parts, pattern_parts = Path(path).parts, Path(pattern).parts

    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        if pattern_parts[pattern_index] == "**":
            return match(path_index, pattern_index + 1) or (path_index < len(path_parts) and not path_parts[path_index].startswith(".") and match(path_index + 1, pattern_index))
        part = pattern_parts[pattern_index]
        return path_index < len(path_parts) and (not path_parts[path_index].startswith(".") or part.startswith(".")) and fnmatch.fnmatchcase(path_parts[path_index], part) and match(path_index + 1, pattern_index + 1)

    return match(0, 0)


def _revision_yaml(repo: Path, revision: str, path: str) -> object:
    result = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=repo, text=True, capture_output=True)
    if result.returncode:
        return {"__prompt_eval_file_missing__": True}
    try:
        return yaml.safe_load(result.stdout)
    except yaml.YAMLError as exc:
        raise SelectionError(f"cannot parse {path} at {revision}: {exc}") from exc


def load_registry(repo: Path) -> list[dict[str, object]]:
    try:
        registry = json.loads((repo / "prompt-evals/registry.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectionError(f"cannot load target registry: {exc}") from exc
    if registry.get("schema_version") != 1 or not isinstance(registry.get("targets"), list):
        raise SelectionError("registry must contain schema_version 1 and a targets list")
    targets = registry["targets"]
    ids: set[str] = set()
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("id"), str) or target["id"] in ids:
            raise SelectionError("every target must have a unique string id")
        ids.add(target["id"])
        if not isinstance(target.get("config"), str) or not isinstance(target.get("cases"), list) or not all(isinstance(pattern, str) and pattern for pattern in target["cases"]) or not isinstance(target.get("prompt_dependencies"), list):
            raise SelectionError(f"target {target['id']} has an invalid registration")
        for dependency in target["prompt_dependencies"]:
            if not isinstance(dependency, dict) or not isinstance(dependency.get("path"), str):
                raise SelectionError(f"target {target['id']} has an invalid prompt dependency")
            keys = dependency.get("keys", [])
            if not isinstance(keys, list) or not all(isinstance(key, str) and key for key in keys):
                raise SelectionError(f"target {target['id']} has invalid dependency keys")
    return targets


def select_targets(repo: Path, base: str, head: str, changed: set[str], targets: list[dict[str, object]]) -> list[dict[str, object]]:
    if changed & INFRASTRUCTURE_PATHS:
        return targets
    selected = []
    for target in targets:
        patterns = [str(item) for item in target["cases"]]
        affected = str(target["config"]) in changed or any(any(_glob_matches(path, pattern) for pattern in patterns) for path in changed)
        for dependency in target["prompt_dependencies"]:
            path = str(dependency["path"])
            if path not in changed:
                continue
            keys = dependency.get("keys", [])
            if not keys or any(_value_at(_revision_yaml(repo, base, path), key) != _value_at(_revision_yaml(repo, head, path), key) for key in keys):
                affected = True
        if affected:
            selected.append(target)
    return selected


def _case_count(files: list[str]) -> int:
    count = 0
    for filename in files:
        try:
            value = yaml.safe_load(Path(filename).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SelectionError(f"cannot parse corpus {filename}: {exc}") from exc
        tests = value.get("tests", []) if isinstance(value, dict) else []
        if not isinstance(tests, list):
            raise SelectionError(f"corpus {filename} must contain a tests list")
        count += len(tests)
    return count


def _parse_results(path: Path, expected: int) -> tuple[int, list[dict[str, object]], bool]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectionError(f"result parsing failed: {exc}") from exc
    rows = document.get("results", {}).get("results") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise SelectionError("result parsing failed: results.results is missing")
    failures = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("success"), bool):
            raise SelectionError(f"result parsing failed at case {index + 1}")
        if not row["success"]:
            assertion = row.get("gradingResult", {})
            failures.append(
                {
                    "case_id": str(row.get("testCase", {}).get("vars", {}).get("id", index + 1)),
                    "assertion_id": str(assertion.get("componentResults", [{}])[0].get("assertion", {}).get("type", "unknown")) if isinstance(assertion, dict) else "unknown",
                    "expected": assertion.get("componentResults", [{}])[0].get("assertion", {}).get("value") if isinstance(assertion, dict) else None,
                    "actual": row.get("response", {}).get("output"),
                }
            )
    return len(rows), failures, len(rows) == expected


def write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(repo: Path, base: str, head: str, npx: str, report_path: Path | None = None, trusted: bool = True, require_credentials: bool = False, identity: dict[str, object] | None = None) -> int:
    report: dict[str, object] = {"schema_version": 1, "base_sha": "", "head_sha": "", "selected_targets": [], "targets": [], "identity": identity or {}}
    records: list[TargetRecord] = []
    exit_code = 0
    try:
        base_sha, head_sha = resolve_revision(repo, base, "base"), resolve_revision(repo, head, "head")
        report.update(base_sha=base_sha, head_sha=head_sha)
        targets = load_registry(repo)
        changed = set(_git(repo, "diff", "--name-only", "--no-renames", "--diff-filter=ACDMRT", base_sha, head_sha).splitlines())
        selected = select_targets(repo, base_sha, head_sha, changed, targets)
        report["selected_targets"] = [target["id"] for target in selected]
        if not selected:
            report["reason"] = "No registered target was affected; no provider was invoked."
            print("No prompt-evaluation targets affected; no provider will be invoked.")
        for target in selected:
            record = TargetRecord(str(target["id"]))
            records.append(record)
            files = sorted({path for pattern in target["cases"] for path in glob.glob(str(repo / str(pattern)), recursive=True) if Path(path).is_file()})
            record.expected_cases = _case_count(files)
            if not files or record.expected_cases == 0:
                record.reason = "Selected corpus has no executable cases; no provider was invoked."
                print(f"Selected {record.id}, but its corpus is empty; skipping Promptfoo.")
                continue
            if not trusted:
                record.reason = "Credentialed evaluation is not authorized for an untrusted fork pull request."
                continue
            if require_credentials and not os.environ.get("CODEX_AUTH_JSON"):
                record.outcome, record.reason, exit_code = "ERROR", "Provider credentials are missing for selected work.", 2
                continue
            if require_credentials:
                auth = Path.home() / ".codex/auth.json"
                auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                auth.write_text(os.environ["CODEX_AUTH_JSON"], encoding="utf-8")
                auth.chmod(0o600)
            config = repo / str(target["config"])
            if not config.is_file():
                record.outcome = "ERROR"
                record.reason = f"Selected target has no config: {target['config']}"
                exit_code = 2
                continue
            output = (report_path.parent if report_path else repo) / f"promptfoo-{record.id}.json"
            print(f"Evaluating affected target: {record.id}", flush=True)
            try:
                result = subprocess.run(
                    [npx, "--yes", f"promptfoo@{PROMPTFOO_VERSION}", "eval", "--config", str(config), "--output", str(output)],
                    cwd=repo,
                    timeout=25 * 60,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                record.outcome, record.reason, exit_code = "ERROR", f"Evaluator execution failed: {exc}", 2
                continue
            try:
                record.executed_cases, record.failures, complete = _parse_results(output, record.expected_cases)
                if not complete:
                    record.outcome = "ERROR"
                    record.reason = f"Partial evaluation: executed {record.executed_cases} of {record.expected_cases} cases."
                else:
                    record.outcome = "MISMATCH" if record.failures else "PASS"
                    record.reason = "Semantic assertions failed." if record.failures else "All semantic assertions passed."
            except SelectionError as exc:
                record.outcome, record.reason = "ERROR", str(exc)
            if result.returncode and record.outcome != "MISMATCH":
                record.outcome = "ERROR"
                record.reason = f"Evaluator exited {result.returncode}; {record.reason}"
            if record.outcome == "ERROR":
                exit_code = result.returncode or 2
            elif record.outcome == "MISMATCH":
                exit_code = result.returncode or 1
    except SelectionError as exc:
        report["error"] = str(exc)
        print(f"Prompt evaluation selection failed closed: {exc}", file=sys.stderr)
        exit_code = 2
    finally:
        report["targets"] = [record.__dict__ for record in records]
        if report_path:
            write_report(report_path, report)
        if require_credentials:
            (Path.home() / ".codex/auth.json").unlink(missing_ok=True)
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--npx", default="npx")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--untrusted", action="store_true")
    parser.add_argument("--require-credentials", action="store_true")
    parser.add_argument("--repository", default="")
    parser.add_argument("--pr-number", type=int, default=0)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--run-attempt", type=int, default=0)
    args = parser.parse_args()
    identity = {"repository": args.repository, "pr_number": args.pr_number, "run_id": args.run_id, "run_attempt": args.run_attempt}
    return run(args.repo.resolve(), args.base, args.head, args.npx, args.report, not args.untrusted, args.require_credentials, identity)


if __name__ == "__main__":
    raise SystemExit(main())
