"""Operator-owned source credentials configuration outside immutable lifecycle YAML.

Only repository and raw-token file selection are extensible. No lifecycle,
authentication, policy, environment or retained-state property is accepted here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

REPOSITORY = "kitamura-tetsuo/auto-coder"


def extension_path(config: Path) -> Path:
    return config.with_name(config.name + ".source-evidence.json")


def _protected(path: Path, *, mode: int | None = None) -> int:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("source evidence path must be canonical and nonsymlink")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parent.parts[1:]:
            successor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = successor
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
            raise PermissionError("source evidence directory is not operator-owned")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022 or (mode is not None and stat.S_IMODE(info.st_mode) != mode):
            raise PermissionError("source evidence file is not protected")
        if (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino):
            raise ValueError("source evidence file changed")
        current_parent = path.parent.stat()
        if path != path.resolve(strict=True) or (parent.st_dev, parent.st_ino) != (current_parent.st_dev, current_parent.st_ino):
            raise ValueError("source evidence directory changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(path: Path, *, mode: int | None = None) -> bytes:
    descriptor = _protected(path, mode=mode)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        content = stream.read(65537)
        after = os.fstat(stream.fileno())
    current = path.stat()

    def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

    if len(content) > 65536 or identity(before) != identity(after) or identity(after) != identity(current):
        raise ValueError("source evidence configuration changed or is too large")
    return content


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate source evidence configuration key")
        result[key] = value
    return result


def prepare(config: Path, repository: str, token_file: Path) -> dict[str, object]:
    """Prepare non-secret configuration; never copy or read a credential value."""
    content = _read(config)
    if repository != REPOSITORY or token_file != config.parent / "source_evidence_read":
        raise ValueError("source evidence repository or token path differs")
    descriptor = _protected(token_file, mode=0o600)
    os.close(descriptor)
    return {"schema_version": 1, "management_config_sha256": hashlib.sha256(content).hexdigest(), "source_repository": repository, "source_evidence_token_file": str(token_file)}


def load_extension(config: Path, original: bytes) -> tuple[str, Path] | None:
    """Read the fixed operator extension, refusing malformed or insecure inputs."""
    path = extension_path(config)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(_read(path, mode=0o600), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid source evidence configuration") from error
    fields = {"schema_version", "management_config_sha256", "source_repository", "source_evidence_token_file"}
    if not isinstance(raw, dict) or set(raw) != fields or type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or not isinstance(raw["source_evidence_token_file"], str) or not isinstance(raw["source_repository"], str):
        raise ValueError("source evidence configuration schema differs")
    token = Path(raw["source_evidence_token_file"])
    expected = prepare(config, raw["source_repository"], token)
    if raw != expected or hashlib.sha256(original).hexdigest() != expected["management_config_sha256"]:
        raise ValueError("source evidence configuration binding differs")
    return REPOSITORY, token


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare source evidence configuration JSON; no credential values or live writes")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.config, args.repository, args.token_file), sort_keys=True))
    except (OSError, ValueError):
        parser.exit(1, "source evidence configuration unavailable\n")


if __name__ == "__main__":
    main()
