"""Deterministic, read-only identities for an explicitly selected Git checkout."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

SCHEMA_VERSION = 1
UNKNOWN_VERSION = "0+unknown"
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_COMMITTER_RE = re.compile(r"^committer .* (?P<timestamp>-?\d+) [+-]\d{4}$")
_VERSION_RE = re.compile(r"(?P<year>\d+)\.(?P<month>\d+)\.(?P<day>\d+)\.(?P<count>\d+)\+g(?P<sha>[0-9a-f]{40}(?:[0-9a-f]{24})?)(?P<dirty>\.dirty)?\Z")


class BuildIdentityError(ValueError):
    """Raised when a serialized identity is invalid."""


@dataclass(frozen=True)
class BuildIdentity:
    """Portable representation of a source checkout's build identity."""

    schema_version: int = SCHEMA_VERSION
    state: Literal["clean", "dirty", "unknown"] = "unknown"
    version: str = UNKNOWN_VERSION
    source_revision: str | None = None
    source_date_epoch: int | None = None
    revision_count: int | None = None
    reason: str | None = "identity has not been resolved"


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    return environment


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.abbrev=40", "-C", str(repo), *arguments],
        check=check,
        capture_output=True,
        text=True,
        env=_git_environment(),
    )


def _unknown(reason: str, revision: str | None = None, timestamp: int | None = None, count: int | None = None) -> BuildIdentity:
    return BuildIdentity(source_revision=revision, source_date_epoch=timestamp, revision_count=count, reason=reason.strip() or "Git observation failed")


def _base_version(timestamp: int, count: int, revision: str) -> str:
    date = datetime.fromtimestamp(timestamp, timezone.utc)
    if date.year < 1:
        raise ValueError("committer timestamp has no positive UTC year")
    return f"{date.year}.{date.month}.{date.day}.{count}+g{revision}"


def _hidden_tracked_change(repo: Path) -> bool:
    """Check entries whose index flags suppress ordinary worktree inspection."""

    root = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    entries = _git(root, "ls-files", "-v", "-z").stdout.split("\0")
    for entry in entries:
        if not entry:
            continue
        marker, path = entry[0], entry[2:]
        if not (marker.islower() or marker == "S"):
            continue
        staged = _git(root, "ls-files", "--stage", "-z", "--", path).stdout
        metadata, separator, _ = staged.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[2] != "0":
            return True
        mode, expected_hash, _stage = fields
        candidate = root / path
        if mode == "160000":
            observed = _git(candidate, "rev-parse", "--verify", "HEAD", check=False)
            if observed.returncode != 0 or observed.stdout.strip() != expected_hash:
                return True
            continue
        observed = _git(root, "hash-object", f"--path={path}", "--", path, check=False)
        if observed.returncode != 0 or observed.stdout.strip() != expected_hash:
            return True
        executable = bool(candidate.stat().st_mode & 0o111)
        if (mode == "100755") != executable:
            return True
    return False


def resolve_build_identity(source_checkout: str | Path) -> BuildIdentity:
    """Resolve the identity of ``source_checkout`` without changing it or fetching."""

    repo = Path(source_checkout).expanduser()
    if not repo.is_dir():
        return _unknown(f"Source checkout is not a directory: {repo}")

    revision: str | None = None
    timestamp: int | None = None
    count: int | None = None
    try:
        inside = _git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip()
        if inside != "true":
            return _unknown(f"Source path is not a Git worktree: {repo}")

        # HEAD is observed exactly once. Every identity query below names this object ID.
        revision = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        if not _SHA_RE.fullmatch(revision):
            return _unknown("Git returned an invalid HEAD object ID")

        shallow = _git(repo, "rev-parse", "--is-shallow-repository").stdout.strip()
        if shallow != "false":
            return _unknown("Complete ancestry is unavailable because the checkout is shallow", revision)

        commit = _git(repo, "cat-file", "-p", revision).stdout
        committer_line = next((line for line in commit.splitlines() if line.startswith("committer ")), None)
        match = _COMMITTER_RE.fullmatch(committer_line or "")
        if match is None:
            return _unknown("Selected commit has no readable committer timestamp", revision)
        timestamp = int(match.group("timestamp"))

        count_text = _git(repo, "rev-list", "--count", revision).stdout.strip()
        count = int(count_text)
        if count < 1:
            return _unknown("Selected commit has an invalid reachable revision count", revision, timestamp)

        dirty = bool(_git(repo, "status", "--porcelain=v1", "--untracked-files=normal").stdout) or _hidden_tracked_change(repo)
        base = _base_version(timestamp, count, revision)
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        return _unknown(f"Unable to establish source identity: {str(detail).strip()}", revision, timestamp, count)

    state: Literal["clean", "dirty"] = "dirty" if dirty else "clean"
    version = f"{base}.dirty" if dirty else base
    return BuildIdentity(state=state, version=version, source_revision=revision, source_date_epoch=timestamp, revision_count=count, reason=None)


def validate_build_identity(record: BuildIdentity) -> BuildIdentity:
    """Validate all fields and cross-field invariants of an identity record."""

    if type(record.schema_version) is not int or record.schema_version != SCHEMA_VERSION:
        raise BuildIdentityError("schema_version must equal 1")
    if record.state not in ("clean", "dirty", "unknown"):
        raise BuildIdentityError("state must be clean, dirty, or unknown")
    if not isinstance(record.version, str):
        raise BuildIdentityError("version must be a string")
    if record.source_revision is not None and (not isinstance(record.source_revision, str) or not _SHA_RE.fullmatch(record.source_revision)):
        raise BuildIdentityError("source_revision must be a full lowercase Git object ID or null")
    if record.source_date_epoch is not None and (type(record.source_date_epoch) is not int):
        raise BuildIdentityError("source_date_epoch must be an integer or null")
    if record.revision_count is not None and (type(record.revision_count) is not int or record.revision_count < 1):
        raise BuildIdentityError("revision_count must be a positive integer or null")

    if record.state == "unknown":
        if record.version != UNKNOWN_VERSION:
            raise BuildIdentityError("unknown records must use version 0+unknown")
        if not isinstance(record.reason, str) or not record.reason.strip():
            raise BuildIdentityError("unknown records must contain a diagnostic reason")
        return record

    if record.reason is not None:
        raise BuildIdentityError("clean and dirty records must have a null reason")
    if record.source_revision is None or record.source_date_epoch is None or record.revision_count is None:
        raise BuildIdentityError("clean and dirty records require complete source fields")
    match = _VERSION_RE.fullmatch(record.version)
    if match is None:
        raise BuildIdentityError("canonical version has an invalid format")
    try:
        expected = _base_version(record.source_date_epoch, record.revision_count, record.source_revision)
    except (OSError, OverflowError, ValueError) as exc:
        raise BuildIdentityError(f"source_date_epoch is invalid: {exc}") from exc
    if record.state == "dirty":
        expected += ".dirty"
    if record.version != expected:
        raise BuildIdentityError("version is inconsistent with state or source fields")
    return record


def serialize_build_identity(record: BuildIdentity) -> str:
    """Serialize a validated identity as stable JSON."""

    validate_build_identity(record)
    return json.dumps(asdict(record), sort_keys=True, separators=(",", ":")) + "\n"


def deserialize_build_identity(payload: str) -> BuildIdentity:
    """Parse JSON strictly; invalid input is never converted to unknown."""

    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise BuildIdentityError(f"Malformed build identity JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BuildIdentityError("Build identity JSON must be an object")
    expected = {field.name for field in BuildIdentity.__dataclass_fields__.values()}
    if set(value) != expected:
        raise BuildIdentityError("Build identity JSON has missing or unexpected fields")
    return validate_build_identity(BuildIdentity(**value))


def output_path_is_allowed(source_checkout: str | Path, destination: str | Path) -> bool:
    """Return whether destination is external to the worktree or Git-ignored."""

    repo = Path(source_checkout).expanduser()
    output = Path(os.path.abspath(Path(destination).expanduser()))
    try:
        root = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip()).resolve()
        common_text = _git(repo, "rev-parse", "--git-common-dir").stdout.strip()
        common_dir = Path(common_text)
        if not common_dir.is_absolute():
            common_dir = repo / common_dir
        common_dir = common_dir.resolve()
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError):
        try:
            output.resolve(strict=False).relative_to(repo.resolve())
        except (OSError, ValueError):
            return True
        return False
    candidates = {output, output.resolve(strict=False)}
    for candidate in candidates:
        for metadata_root in (git_dir, common_dir):
            try:
                candidate.relative_to(metadata_root)
            except ValueError:
                continue
            return False
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            return False
        tracked = _git(root, "ls-files", "--error-unmatch", "--", relative.as_posix(), check=False)
        if tracked.returncode == 0:
            return False
        ignored = _git(root, "check-ignore", "--no-index", "-q", "--", relative.as_posix(), check=False)
        if ignored.returncode != 0:
            return False
    return True


def write_build_identity(record: BuildIdentity, source_checkout: str | Path, destination: str | Path) -> None:
    """Write a record only to a permitted explicitly selected destination."""

    target = Path(destination).expanduser()
    if not output_path_is_allowed(source_checkout, target):
        raise BuildIdentityError("Output must be outside the checkout or ignored by it")
    payload = serialize_build_identity(record)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
