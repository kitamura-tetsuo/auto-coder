#!/usr/bin/env python3
"""Validate tracked repository paths against the root-file hygiene policy."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ALLOWLIST_PATH = "scripts/repository_hygiene_allowlist.json"
GUIDANCE = (
    "Disposable artifacts should remain untracked; maintained scripts belong under "
    "scripts/."
)


class InspectionError(Exception):
    """The requested repository state could not be inspected safely."""


def run_git(repo: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(repo), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise InspectionError(f"could not execute Git: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise InspectionError(detail or f"Git command failed: {' '.join(arguments)}")
    return result.stdout


def repository_root(path: Path) -> Path:
    if not path.exists() or not path.is_dir():
        raise InspectionError(f"repository path is not a directory: {path}")
    output = run_git(path, "rev-parse", "--show-toplevel")
    return Path(os.fsdecode(output.rstrip(b"\n")))


def selected_paths(repo: Path, source: str) -> list[str]:
    if source == "index":
        if run_git(repo, "ls-files", "--unmerged", "-z"):
            raise InspectionError("the index contains unresolved conflicts")
        output = run_git(repo, "ls-files", "--cached", "-z")
    else:
        output = run_git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    return [os.fsdecode(item) for item in output.split(b"\0") if item]


def selected_allowlist(repo: Path, source: str) -> set[str]:
    object_name = f":{ALLOWLIST_PATH}" if source == "index" else f"HEAD:{ALLOWLIST_PATH}"
    raw = run_git(repo, "show", object_name)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InspectionError(f"invalid {ALLOWLIST_PATH}: {error}") from error
    if not isinstance(value, list):
        raise InspectionError(f"invalid {ALLOWLIST_PATH}: expected a JSON array")

    entries: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            raise InspectionError(f"invalid {ALLOWLIST_PATH}: every entry must be a string")
        if (
            not entry
            or entry in {".", "..", ".agent-tmp"}
            or "/" in entry
            or any(character in entry for character in "*?[]")
            or entry.endswith(".py")
        ):
            raise InspectionError(
                f"invalid {ALLOWLIST_PATH} entry: {json.dumps(entry, ensure_ascii=True)}"
            )
        if entry in entries:
            raise InspectionError(
                f"invalid {ALLOWLIST_PATH}: duplicate entry "
                f"{json.dumps(entry, ensure_ascii=True)}"
            )
        entries.add(entry)
    return entries


def violations(paths: list[str], allowlist: set[str]) -> list[tuple[str, str]]:
    rejected: list[tuple[str, str]] = []
    for path in paths:
        if path == ".agent-tmp" or path.startswith(".agent-tmp/"):
            rejected.append((path, "tracked .agent-tmp content is prohibited"))
        elif "/" not in path and path.endswith(".py"):
            rejected.append((path, "tracked root-level Python files are prohibited"))
        elif "/" not in path and path not in allowlist:
            rejected.append((path, "root filename is not in the explicit allowlist"))
    return sorted(rejected, key=lambda item: os.fsencode(item[0]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("index", "head"), default="index")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        repo = repository_root(args.repo)
        paths = selected_paths(repo, args.source)
        allowlist = selected_allowlist(repo, args.source)
    except InspectionError as error:
        print(f"ERROR: repository hygiene check could not be performed: {error}", file=sys.stderr)
        print(GUIDANCE, file=sys.stderr)
        return 2

    rejected = violations(paths, allowlist)
    if rejected:
        for path, reason in rejected:
            print(f"VIOLATION: {json.dumps(path, ensure_ascii=True)}: {reason}")
        print(GUIDANCE)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
