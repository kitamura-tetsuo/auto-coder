"""Worker-origin repository readiness checks for supervised Codex launches."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class RepositoryReadinessError(RuntimeError):
    """Raised when the imminent worker cannot use the bound private repository."""


@dataclass(frozen=True)
class RepositoryReadiness:
    worker_uid: int
    worker_gid: int
    root: Path
    git_dir: Path
    common_dir: Path
    head: str
    readable_regular_files: int
    tracked_contents_checksum: str


_DIRECTORY_OPTIONS = ("-C", "--cd")


def validate_codex_effective_directory(arguments: Sequence[str], workspace: Path) -> None:
    """Reject Codex options which redirect execution away from *workspace*."""
    expected = workspace.resolve(strict=True)
    selected = expected
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in _DIRECTORY_OPTIONS:
            if index + 1 >= len(arguments):
                raise RepositoryReadinessError(f"Codex directory option {argument} has no value")
            selected = Path(arguments[index + 1])
            index += 2
        elif argument.startswith("--cd="):
            selected = Path(argument.partition("=")[2])
            index += 1
        elif argument.startswith("-C="):
            selected = Path(argument.partition("=")[2])
            index += 1
        elif argument.startswith("-C") and len(argument) > 2:
            selected = Path(argument[2:])
            index += 1
        else:
            index += 1
    if not selected.is_absolute():
        selected = expected / selected
    try:
        effective = selected.resolve(strict=True)
    except OSError as exc:
        raise RepositoryReadinessError(f"Codex effective working directory is unavailable: {selected}: {exc}") from exc
    if effective != expected:
        raise RepositoryReadinessError(f"Codex effective working directory {effective} does not match bound private result root {expected}")


_WORKER_PROBE = r"""
import hashlib, json, os, pathlib, stat, subprocess, sys
root = pathlib.Path(sys.argv[1]).resolve(strict=True)
def git(*args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "git probe failed")
    return result.stdout
if git("rev-parse", "--is-inside-work-tree").strip() != b"true":
    raise RuntimeError("bound root is not a non-bare Git working repository")
git_root = pathlib.Path(os.fsdecode(git("rev-parse", "--show-toplevel").strip())).resolve(strict=True)
git_dir = pathlib.Path(os.fsdecode(git("rev-parse", "--absolute-git-dir").strip())).resolve(strict=True)
common_text = os.fsdecode(git("rev-parse", "--git-common-dir").strip())
common_dir = pathlib.Path(common_text)
if not common_dir.is_absolute():
    common_dir = (root / common_dir).resolve(strict=True)
head = os.fsdecode(git("rev-parse", "--verify", "HEAD").strip())
head_type = os.fsdecode(git("cat-file", "-t", "HEAD").strip())
if head_type != "commit":
    raise RuntimeError(f"current HEAD is not a readable commit object: {head_type}")
if git_root != root:
    raise RuntimeError(f"Git discovery selected {git_root} instead of {root}")
for label, path in (("Git directory", git_dir), ("Git common directory", common_dir)):
    if path != root and root not in path.parents:
        raise RuntimeError(f"{label} {path} is outside bound private result root {root}")
    if not os.access(path, os.R_OK | os.X_OK):
        raise RuntimeError(f"{label} is not readable and traversable: {path}")
count = 0
contents = hashlib.sha256()
for raw in git("ls-files", "-z").split(b"\0"):
    if not raw:
        continue
    path = root / os.fsdecode(raw)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        continue
    if stat.S_ISREG(mode):
        with path.open("rb") as stream:
            data = stream.read()
        contents.update(raw)
        contents.update(b"\0")
        contents.update(data)
        count += 1
print(json.dumps({"root": str(root), "git_dir": str(git_dir), "common_dir": str(common_dir), "head": head, "readable_regular_files": count, "tracked_contents_checksum": contents.hexdigest()}))
"""


def verify_worker_repository(
    workspace: Path,
    *,
    worker_uid: int | None,
    worker_gid: int | None,
    environment: Mapping[str, str],
) -> RepositoryReadiness:
    """Probe current Git usability after dropping to the launch worker identity."""
    if worker_uid is None or worker_gid is None or worker_uid == 0:
        raise RepositoryReadinessError("repository readiness requires an explicit non-root worker identity")

    system_python = next((candidate for candidate in (Path("/usr/bin/python3"), Path("/usr/local/bin/python3")) if candidate.is_file()), None)
    python = str(system_python.resolve()) if system_python is not None else shutil.which("python3", path=environment.get("PATH"))
    if python is None:
        raise RepositoryReadinessError("worker repository probe requires python3 on the launch PATH")
    probe_environment = dict(environment)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        probe_environment.pop(name, None)
    try:
        result = subprocess.run(
            [python, "-c", _WORKER_PROBE, str(workspace)],
            cwd=workspace,
            env=probe_environment,
            capture_output=True,
            text=True,
            user=worker_uid,
            group=worker_gid,
            extra_groups=(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositoryReadinessError(f"worker repository probe could not start: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise RepositoryReadinessError(f"worker repository probe failed: {detail}")
    try:
        observation = json.loads(result.stdout)
        return RepositoryReadiness(
            worker_uid=worker_uid,
            worker_gid=worker_gid,
            root=Path(observation["root"]),
            git_dir=Path(observation["git_dir"]),
            common_dir=Path(observation["common_dir"]),
            head=str(observation["head"]),
            readable_regular_files=int(observation["readable_regular_files"]),
            tracked_contents_checksum=str(observation["tracked_contents_checksum"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RepositoryReadinessError("worker repository probe returned invalid evidence") from exc
