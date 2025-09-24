#!/usr/bin/env python3
"""Utility to update the Auto-Coder version string across the repository."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_git_command(args: list[str]) -> str:
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def compute_version() -> str:
    """Compute the YYYY.M.D[.patch]+g<sha> version for the current HEAD."""

    commit_date = _run_git_command(["show", "-s", "--format=%cd", "--date=format:%Y-%m-%d", "HEAD"])
    year_str, month_str, day_str = commit_date.split("-")
    base_date = f"{year_str}.{int(month_str)}.{int(day_str)}"

    commits_today = int(
        _run_git_command(
            [
                "rev-list",
                "--count",
                f"--since={year_str}-{month_str}-{day_str} 00:00:00",
                "HEAD",
            ]
        )
        or "0"
    )
    patch_suffix = f".{commits_today - 1}" if commits_today > 1 else ""

    short_sha = _run_git_command(["rev-parse", "--short", "HEAD"])
    return f"{base_date}{patch_suffix}+g{short_sha}"


def _replace_pattern(path: Path, pattern: str, replacement_value: str) -> bool:
    original = path.read_text()

    def _repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}{replacement_value}{match.group(3)}"

    updated, count = re.subn(pattern, _repl, original, count=1, flags=re.MULTILINE | re.DOTALL)
    if count == 0:
        raise ValueError(f"Could not find pattern in {path}")
    if updated != original:
        path.write_text(updated)
        return True
    return False


def update_version_files(version: str) -> None:
    update_targets: list[tuple[Path, str]] = [
        (REPO_ROOT / "pyproject.toml", r'^(version\s*=\s*")([^\"]+)(")'),
        (REPO_ROOT / "src" / "auto_coder" / "__init__.py", r'^(__version__\s*=\s*")([^\"]+)(")'),
        (
            REPO_ROOT / "docs" / "client-features.yaml",
            r'^(\s*version:\s*")([^\"]+)(")',
        ),
    ]

    uv_lock_path = REPO_ROOT / "uv.lock"
    if uv_lock_path.exists():
        update_targets.append(
            (
                uv_lock_path,
                r'(\[\[package\]\]\s+name\s*=\s*"auto-coder"\s+version\s*=\s*")([^\"]+)(")',
            )
        )

    for path, pattern in update_targets:
        if not path.exists():
            raise FileNotFoundError(f"Expected file not found: {path}")

    for path, pattern in update_targets:
        _replace_pattern(path, pattern, version)


def main() -> None:
    try:
        version = compute_version()
        update_version_files(version)
    except Exception as exc:  # pragma: no cover - simple CLI wrapper
        print(f"Failed to update version: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(version)


if __name__ == "__main__":
    main()
