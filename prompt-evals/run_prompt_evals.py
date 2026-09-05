#!/usr/bin/env python3
"""Select and run only affected Promptfoo targets."""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import subprocess
import sys
from pathlib import Path

import yaml

PROMPTFOO_VERSION = "0.118.8"
INFRASTRUCTURE_PATHS = {
    ".github/workflows/prompt-regression.yml",
    "prompt-evals/registry.json",
    "prompt-evals/run_prompt_evals.py",
}


class SelectionError(RuntimeError):
    """Raised when affected targets cannot be determined safely."""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if result.returncode:
        raise SelectionError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


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
    """Match a repository path with the same recursive semantics as glob.glob."""
    path_parts = Path(path).parts
    pattern_parts = Path(pattern).parts

    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        if pattern_parts[pattern_index] == "**":
            return match(path_index, pattern_index + 1) or (path_index < len(path_parts) and not path_parts[path_index].startswith(".") and match(path_index + 1, pattern_index))
        pattern_part = pattern_parts[pattern_index]
        return path_index < len(path_parts) and (not path_parts[path_index].startswith(".") or pattern_part.startswith(".")) and fnmatch.fnmatchcase(path_parts[path_index], pattern_part) and match(path_index + 1, pattern_index + 1)

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
    path = repo / "prompt-evals/registry.json"
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
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
    selected: list[dict[str, object]] = []
    for target in targets:
        config = str(target["config"])
        patterns = [str(item) for item in target["cases"]]
        affected = config in changed or any(any(_glob_matches(path, pattern) for pattern in patterns) for path in changed)
        for dependency in target["prompt_dependencies"]:
            path = dependency["path"]
            if path not in changed:
                continue
            keys = dependency.get("keys", [])
            if not keys:
                affected = True
                continue
            before = _revision_yaml(repo, base, path)
            after = _revision_yaml(repo, head, path)
            if any(_value_at(before, key) != _value_at(after, key) for key in keys):
                affected = True
        if affected:
            selected.append(target)
    return selected


def run(repo: Path, base: str, head: str, npx: str) -> int:
    targets = load_registry(repo)
    # Disabling rename detection exposes both the deleted source and added
    # destination, so a stale registration cannot silently miss a rename.
    changed = set(_git(repo, "diff", "--name-only", "--no-renames", "--diff-filter=ACDMRT", base, head).splitlines())
    selected = select_targets(repo, base, head, changed, targets)
    if not selected:
        print("No prompt-evaluation targets affected; no provider will be invoked.")
        return 0
    for target in selected:
        case_files = sorted({path for pattern in target["cases"] for path in glob.glob(str(repo / str(pattern)), recursive=True) if Path(path).is_file()})
        if not case_files:
            print(f"Selected {target['id']}, but its corpus is empty; skipping Promptfoo.")
            continue
        config = repo / str(target["config"])
        if not config.is_file():
            raise SelectionError(f"selected target {target['id']} has no config: {target['config']}")
        print(f"Evaluating affected target: {target['id']}", flush=True)
        result = subprocess.run([npx, "--yes", f"promptfoo@{PROMPTFOO_VERSION}", "eval", "--config", str(config)], cwd=repo)
        if result.returncode:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--npx", default="npx")
    args = parser.parse_args()
    try:
        return run(args.repo.resolve(), args.base, args.head, args.npx)
    except SelectionError as exc:
        print(f"Prompt evaluation selection failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
