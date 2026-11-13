"""Host validation for sealed, complete ancestor exclusions; no process effects."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .archived_exclusions import ArchivedExclusion, parse_exclusions, unique_pairs
from .bootstrap_supervisor import BootstrapConfig
from .fresh_initialization import FreshInitializationIntent, load_fresh_initialization, read_fresh_status

if TYPE_CHECKING:
    from .fresh_host_observation import PriorContainer


def protected_bytes(path: Path, *, private: bool = True) -> bytes:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("noncanonical exclusion provenance")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValueError("unprotected exclusion provenance")
        content = stream.read(65537)
    if len(content) > 65536:
        raise ValueError("exclusion provenance too large")
    return content


def _mount(mounts: list[object], destination: str, *, rw: bool = False) -> Path:
    matches = [item for item in mounts if isinstance(item, dict) and item.get("Destination") == destination]
    if len(matches) != 1 or matches[0].get("Type") != "bind" or matches[0].get("RW") is not rw or not isinstance(matches[0].get("Source"), str):
        raise ValueError("exclusion supporting mount differs")
    path = Path(matches[0]["Source"])
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("exclusion supporting source differs")
    for item in mounts:
        if not isinstance(item, dict) or not isinstance(item.get("Destination"), str):
            raise ValueError("incomplete supporting mount profile")
        other = Path(item["Destination"])
        target = Path(destination)
        if other != target and (other in target.parents or target in other.parents):
            raise ValueError("exclusion supporting mount is shadowed")
    return path


def check_archive_mounts(mounts: list[object], bindings: tuple[ArchivedExclusion, ...]) -> set[str]:
    accepted: set[str] = set()
    for binding in bindings:
        root = _mount(mounts, binding.journal_root)
        fence = _mount(mounts, binding.fence)
        if str(root) != binding.host_journal_root or str(fence) != binding.host_fence:
            raise ValueError("archive source differs from sealed provenance")
        database = root / "management-operations.sqlite3"
        info, lock = database.lstat(), fence.lstat()
        if not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(lock.st_mode) or (info.st_dev, info.st_ino) != (binding.database_device, binding.database_inode) or (lock.st_dev, lock.st_ino) != (binding.fence_device, binding.fence_inode):
            raise ValueError("archive database or fence identity differs")
        accepted.update((binding.journal_root, binding.fence))
    return accepted


def inherited_support(mounts: list[object]) -> tuple[tuple[ArchivedExclusion, ...], set[str]]:
    """Derive ancestors from the predecessor's original protected initialization."""
    bootstrap = _mount(mounts, "/bootstrap") / "config.json"
    content = protected_bytes(bootstrap, private=False)
    raw = json.loads(content, object_pairs_hook=unique_pairs)
    archived = parse_exclusions(raw.get("archived_exclusions", []))
    if raw.get("fresh_prior_lock_path") is None:
        if archived:
            raise ValueError("archives lack fresh predecessor provenance")
        if raw.get("original_genesis") is not None:
            from .original_genesis import GENESIS_NAME, HISTORY_MOUNT, read_genesis, validate_history_genesis

            if raw["original_genesis"] != "/bootstrap/" + GENESIS_NAME:
                raise ValueError("ordinary original genesis path differs")
            original = read_genesis(bootstrap.parent / GENESIS_NAME)
            if validate_history_genesis(Path(original.binding.root)) != original or original.instance_id != raw.get("instance_id") or original.file("bootstrap").sha256 != hashlib.sha256(content).hexdigest():
                raise ValueError("ordinary original genesis authority differs")
            for role in ("state", "journal", "oauth", "history"):
                expected = original.root(role)
                selected = _mount(mounts, expected.container_path, rw=role != "history")
                current = selected.lstat()
                if str(selected) != expected.host_path or (current.st_dev, current.st_ino) != (expected.device, expected.inode):
                    raise ValueError("ordinary original genesis mounted root differs")
            return (), {str(HISTORY_MOUNT)}
        return (), set()
    if raw.get("state_root") != "/management-child-state" or raw.get("fresh_prior_lock_path") != "/management-prior-owner.lock":
        raise ValueError("predecessor fresh paths differ")
    state = _mount(mounts, "/management-child-state", rw=True)
    intent, _ = load_fresh_initialization(state, host=True)
    if read_fresh_status(state, host=True)["status"] != "succeeded":
        raise ValueError("predecessor fresh initialization unresolved")
    if (intent.state_root, intent.journal_root, intent.oauth_root) != (raw.get("state_root"), raw.get("journal_root"), raw.get("oauth_root")):
        raise ValueError("predecessor retained roots differ")
    if intent.bootstrap_config_sha256 != hashlib.sha256(content).hexdigest() or intent.new_instance != raw.get("instance_id") or not intent.host_prior_journal_root:
        raise ValueError("predecessor initialization or ancestor journal provenance differs")
    prior_root = _mount(mounts, intent.prior_journal_root)
    fence = _mount(mounts, "/management-prior-owner.lock")
    guard = _mount(mounts, "/management-initialization")
    if (guard.stat().st_dev, guard.stat().st_ino) != (intent.guard_device, intent.guard_inode):
        raise ValueError("predecessor initialization mount differs")
    direct = ArchivedExclusion(intent.old_instance, intent.host_prior_journal_root, "/management-excluded/0/journal", intent.prior_journal_device, intent.prior_journal_inode, str(fence), "/management-excluded/0/owner.lock", intent.owner_lock_device, intent.owner_lock_inode)
    database, lock = (prior_root / "management-operations.sqlite3").lstat(), fence.lstat()
    if str(prior_root) != direct.host_journal_root or not stat.S_ISREG(database.st_mode) or not stat.S_ISREG(lock.st_mode) or (database.st_dev, database.st_ino) != (direct.database_device, direct.database_inode) or (lock.st_dev, lock.st_ino) != (direct.fence_device, direct.fence_inode):
        raise ValueError("predecessor prior database or fence differs")
    accepted = check_archive_mounts(mounts, archived)
    accepted.update((intent.prior_journal_root, "/management-prior-owner.lock", "/management-initialization"))
    bindings = (direct, *(replace(item, journal_root=f"/management-excluded/{index}/journal", fence=f"/management-excluded/{index}/owner.lock") for index, item in enumerate(archived, 1)))
    # Reuse strict set validation, including the finite ancestry bound.
    from dataclasses import asdict

    return parse_exclusions([asdict(item) for item in bindings]), accepted


def required_exclusions(prior: PriorContainer) -> tuple[ArchivedExclusion, ...]:
    from .fresh_host_observation import _inspect

    if hashlib.sha256(protected_bytes(Path(prior.bootstrap_source), private=False)).hexdigest() != prior.bootstrap_sha256:
        raise ValueError("captured predecessor bootstrap changed")
    mounts = _inspect(prior.container_id)[11]
    if not isinstance(mounts, list):
        raise ValueError("predecessor mount observation unavailable")
    observed = tuple(sorted((str(item["Source"]), str(item["Destination"]), item["RW"]) for item in mounts if isinstance(item, dict) and item.get("Type") == "bind" and type(item.get("RW")) is bool))
    if len(observed) != len(mounts) or hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest() != prior.mounts_sha256:
        raise ValueError("captured predecessor mounts changed")
    return inherited_support(mounts)[0]


def validate_successor(prior: PriorContainer, intent: FreshInitializationIntent, bootstrap_path: Path | None) -> tuple[ArchivedExclusion, ...]:
    required = required_exclusions(prior)
    prior_config = json.loads(protected_bytes(Path(prior.bootstrap_source), private=False), object_pairs_hook=unique_pairs)
    if prior_config.get("original_genesis") is not None:
        from .original_genesis import GENESIS_NAME, read_genesis, validate_history_genesis

        if prior_config["original_genesis"] != "/bootstrap/" + GENESIS_NAME:
            raise ValueError("original genesis predecessor selection differs")
        genesis = read_genesis(Path(prior.bootstrap_source).parent / GENESIS_NAME)
        if validate_history_genesis(Path(genesis.binding.root)) != genesis or (genesis.instance_id, genesis.management_id, genesis.file("bootstrap").sha256) != (prior.instance_id, prior.container_id, prior.bootstrap_sha256):
            raise ValueError("original genesis predecessor authority differs")
        if (intent.original_history, intent.original_history_id) != (genesis.binding.root, genesis.binding.history):
            raise ValueError("covered original genesis history cannot be omitted or replaced")
    if prior_config.get("fresh_prior_lock_path") is not None:
        # An explicitly enrolled original predecessor cannot silently lose its
        # source-bound history when the next real host cutover is prepared.
        # This does not claim coverage before enrollment or for sibling roots.
        from .fresh_host_observation import _inspect

        mounts = _inspect(prior.container_id)[11]
        if not isinstance(mounts, list):
            raise ValueError("original predecessor mount observation unavailable")
        prior_intent, _ = load_fresh_initialization(_mount(mounts, "/management-child-state", rw=True), host=True)
        if prior_intent.original_history and (intent.original_history, intent.original_history_id) != (prior_intent.original_history, prior_intent.original_history_id):
            raise ValueError("covered original history cannot be omitted or replaced")
    if bootstrap_path is None:
        if required:
            raise ValueError("inherited exclusions require sealed successor bootstrap")
        return ()
    content = protected_bytes(bootstrap_path)
    if hashlib.sha256(content).hexdigest() != intent.bootstrap_config_sha256:
        raise ValueError("successor bootstrap differs from initialization")
    config = BootstrapConfig.read(bootstrap_path)
    if config.instance_id != intent.new_instance or tuple(item.identity() for item in config.archived_exclusions) != tuple(item.identity() for item in required):
        raise ValueError("successor exclusions omit or change an ancestor")
    if required and not intent.host_prior_journal_root:
        raise ValueError("direct prior journal cannot be omitted")
    if any(item.instance_id in {intent.old_instance, intent.new_instance} or (item.fence_device, item.fence_inode) == (intent.owner_lock_device, intent.owner_lock_inode) or (item.database_device, item.database_inode) == (intent.prior_journal_device, intent.prior_journal_inode) for item in required):
        raise ValueError("excluded ancestor overlaps current or direct prior")
    protected_roots = [Path(root) for root in (intent.host_state_root, intent.host_journal_root, intent.host_oauth_root, intent.host_prior_journal_root) if root]
    for item in config.archived_exclusions:
        for root in protected_roots:
            archived = Path(item.host_journal_root)
            if archived == root or archived in root.parents or root in archived.parents or Path(item.host_fence) == root or root in Path(item.host_fence).parents:
                raise ValueError("archive overlaps active or direct-prior roots")
    return config.archived_exclusions


def check_exclusion_peer(bindings: tuple[ArchivedExclusion, ...], descriptors: tuple[int, ...], pid: int, *, mounts: list[object] | None = None) -> None:
    from .fresh_host_observation import assert_peer_holds_shared_fence

    if len(bindings) != len(descriptors):
        raise ValueError("complete exclusion descriptor set required")
    if mounts is not None:
        check_archive_mounts(mounts, bindings)
    for binding, descriptor in zip(bindings, descriptors):
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (binding.fence_device, binding.fence_inode):
            raise ValueError("exclusion descriptor changed")
        assert_peer_holds_shared_fence(pid, descriptor)


def check_host_archives(bindings: tuple[ArchivedExclusion, ...]) -> None:
    from .operation_store import ACTIVE_PHASES
    from .retained_journal import RetainedJournal

    for binding in bindings:
        database = Path(binding.host_journal_root) / "management-operations.sqlite3"
        wal = database.with_name(database.name + "-wal")
        if wal.is_symlink() or (wal.exists() and wal.stat().st_size):
            raise ValueError("excluded WAL is not proven empty")
        with RetainedJournal(database, Path(binding.host_fence), database_identity=(binding.database_device, binding.database_inode), fence_identity=(binding.fence_device, binding.fence_inode), prior_management_instance=binding.instance_id) as reader:
            if any(record.phase in ACTIVE_PHASES for record in reader.read()):
                raise ValueError("excluded generation contains unresolved operations")


def main() -> None:
    """Describe the required non-secret set without claiming or changing state."""
    import argparse
    from dataclasses import asdict

    from .fresh_host import _evidence

    parser = argparse.ArgumentParser(description="Describe mandatory archived exclusions from captured predecessor evidence")
    parser.add_argument("--evidence-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        management, _proxy, _digest = _evidence(args.evidence_file)
        print(json.dumps([asdict(item) for item in required_exclusions(management)], sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, "ancestor exclusion evidence unavailable: " + str(error) + "\n")


if __name__ == "__main__":
    main()
