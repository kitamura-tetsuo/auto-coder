"""Fail-closed runtime contract for externally deployed release/beta instances."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

VALID_CHANNELS = ("release", "beta")


class DeploymentChannelError(RuntimeError):
    """Raised when deployment identity or repository ownership is unsafe."""


@dataclass(frozen=True)
class DeploymentIdentity:
    channel: str
    artifact: str
    runtime_root: Path
    ownership_file: Path


def deployment_identity() -> DeploymentIdentity | None:
    """Read and strictly validate external deployment identity from the environment."""
    channel = os.environ.get("AUTO_CODER_CHANNEL")
    if channel is None:
        return None
    if channel not in VALID_CHANNELS:
        raise DeploymentChannelError("AUTO_CODER_CHANNEL must be 'release' or 'beta'")
    artifact = os.environ.get("AUTO_CODER_ARTIFACT_DIGEST", "").strip()
    runtime = os.environ.get("AUTO_CODER_RUNTIME_ROOT", "").strip()
    ownership = os.environ.get("AUTO_CODER_OWNERSHIP_FILE", "").strip()
    if not artifact or not runtime or not ownership:
        raise DeploymentChannelError("Externally deployed instances require artifact digest, runtime root, and ownership file")
    runtime_root = Path(runtime).expanduser().resolve()
    ownership_file = Path(ownership).expanduser().resolve()
    if ownership_file == runtime_root or runtime_root in ownership_file.parents:
        raise DeploymentChannelError("The shared ownership file must be outside the channel-private runtime root")
    return DeploymentIdentity(channel, artifact, runtime_root, ownership_file)


@contextmanager
def _locked_registry(path: Path) -> Iterator[dict[str, str]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or any(not isinstance(k, str) or v not in VALID_CHANNELS for k, v in value.items()):
                    raise DeploymentChannelError("Repository ownership registry is invalid")
            else:
                value = {}
            yield value
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentChannelError(f"Cannot safely read repository ownership: {exc}") from exc
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def validate_repository_ownership(repo_name: str) -> DeploymentIdentity | None:
    """Fail before dispatch unless the repository is assigned to this instance."""
    identity = deployment_identity()
    if identity is None:
        return None
    with _locked_registry(identity.ownership_file) as assignments:
        owner = assignments.get(repo_name)
    if owner != identity.channel:
        detail = "unassigned" if owner is None else f"assigned to {owner}"
        raise DeploymentChannelError(f"Repository {repo_name} is {detail}; {identity.channel} refuses to process it")
    identity.runtime_root.mkdir(parents=True, exist_ok=True)
    (identity.runtime_root / "running-artifact.json").write_text(
        json.dumps({"channel": identity.channel, "artifact": identity.artifact}, indent=2) + "\n",
        encoding="utf-8",
    )
    return identity


@contextmanager
def repository_dispatch_authority(repo_name: str) -> Iterator[None]:
    """Serialize ownership validation with the creation of durable active work.

    Callers must establish their implementation slot while this context is held.
    Reassignment uses the same registry lock and therefore either happens first
    (making this validation fail) or happens after the slot is durable (making
    reassignment fail).
    """
    identity = deployment_identity()
    if identity is None:
        yield
        return
    with _locked_registry(identity.ownership_file) as assignments:
        owner = assignments.get(repo_name)
        if owner != identity.channel:
            detail = "unassigned" if owner is None else f"assigned to {owner}"
            raise DeploymentChannelError(f"Repository {repo_name} is {detail}; {identity.channel} refuses to dispatch work")
        yield


def assign_repository(repo_name: str, channel: str, ownership_file: Path, runtime_parent: Path) -> None:
    """Atomically reassign a repository only when its old channel has no active work."""
    if channel not in VALID_CHANNELS:
        raise DeploymentChannelError("channel must be 'release' or 'beta'")
    with _locked_registry(ownership_file) as assignments:
        old_channel = assignments.get(repo_name)
        if old_channel and old_channel != channel:
            slot_path = runtime_parent / old_channel / "state" / repo_name / "implementation_slots.json"
            if slot_path.exists():
                try:
                    slots = json.loads(slot_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise DeploymentChannelError(f"Cannot prove the old channel has no active work: {exc}") from exc
                if not isinstance(slots, dict) or slots:
                    raise DeploymentChannelError(f"Repository {repo_name} still has active work in {old_channel}")
        assignments[repo_name] = channel
        temporary = ownership_file.with_suffix(ownership_file.suffix + ".tmp")
        temporary.write_text(json.dumps(assignments, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, ownership_file)
