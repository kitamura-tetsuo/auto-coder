"""Independent read-only derivation of the bounded original-attempt frontier.

A result carries no OS exclusion, grant, launch or recovery epoch authority.
Consumers hold their own history observation and any deployment owner fences.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from .fresh_initialization import FreshInitializationIntent, parse_fresh_intent, read_fresh_status
from .original_attempts import PROFILE, HistoryBinding, HistoryEvent, OriginalEvidenceUnavailable, canonical, history_lock, identity, read_frontier, read_original, source_identity, validate_original_lifetime
from .original_genesis import HISTORY_PROFILE, GenesisAnchor


@dataclass(frozen=True)
class OriginalFile:
    path: str
    device: int
    inode: int
    sha256: str


@dataclass(frozen=True)
class AttemptEvidence:
    request_id: str
    predecessor: str
    target: str
    state: str
    expected_stores: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OriginalAttemptEvidence:
    binding: HistoryBinding
    profile: str = PROFILE
    earlier_history: str = "UNAVAILABLE: before prospective enrollment"
    frontier: str = ""
    attempts: tuple[AttemptEvidence, ...] = ()
    originals: tuple[OriginalFile, ...] = ()
    unavailable: tuple[str, ...] = ()
    # There is deliberately no completeness boolean or authorization field.


@dataclass(frozen=True)
class OriginalGeneration:
    """Original scoped identities; an unresolved outcome remains unresolved."""

    request_id: str
    instance_id: str
    management_id: str
    image_id: str
    bootstrap_config_sha256: str
    management_config_sha256: str
    state_root: str
    state_device: int
    state_inode: int
    journal_root: str
    journal_device: int
    journal_inode: int
    oauth_root: str
    oauth_device: int
    oauth_inode: int
    owner_path: str
    owner_device: int
    owner_inode: int
    outcome: str
    expected_stores: tuple[tuple[str, str], ...]
    proxy_id: str = ""
    proxy_image_id: str = ""


@dataclass(frozen=True)
class OriginalLineageEvidence:
    binding: HistoryBinding
    profile: str = HISTORY_PROFILE
    genesis: GenesisAnchor | None = None
    frontier: str = ""
    attempts: tuple[AttemptEvidence, ...] = ()
    generations: tuple[OriginalGeneration, ...] = ()
    originals: tuple[OriginalFile, ...] = ()
    unavailable: tuple[str, ...] = ()


def _snapshot(path: Path) -> OriginalFile:
    data, (device, inode) = read_original(path)
    return OriginalFile(str(path), device, inode, hashlib.sha256(data).hexdigest())


def _check_fence(binding: HistoryBinding, fd: int) -> None:
    root = Path(binding.root)
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != identity(root / "history.lock"):
        raise OriginalEvidenceUnavailable("held history fence was replaced")
    # fdinfo is evidence of this exact held open-file-description's kernel lock,
    # unlike a caller's asserted flag or another process's same-inode lock.
    lines = Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
    if not any(line.startswith("lock:") and " FLOCK  ADVISORY  READ " in line for line in lines):
        raise OriginalEvidenceUnavailable("consumer does not hold a shared history fence")


@contextmanager
def observe(binding: HistoryBinding) -> Iterator[int]:
    """Release this temporary observation on every refusal; no continuous proof."""
    with history_lock(Path(binding.root), exclusive=False) as fd:
        _check_fence(binding, fd)
        yield fd


def _require_settled_sqlite(path: Path) -> None:
    # Immutable SQLite readers ignore both uncheckpointed WAL data and hot
    # rollback journals. Either unresolved sidecar invalidates the original.
    # This observation never creates or cleans up sidecars.
    for suffix, description in (("-wal", "WAL"), ("-journal", "rollback journal")):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size):
            raise OriginalEvidenceUnavailable("original database " + description + " is unresolved")


def _database(path: Path) -> None:
    _require_settled_sqlite(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise OriginalEvidenceUnavailable("original database is corrupt")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {
            "management-child.sqlite3": {"metadata", "child_state", "effects", "supervisor_presence"},
            "management-operations.sqlite3": {"metadata", "operations", "operation_history"},
            "management-self-update.sqlite3": {"metadata", "handoffs"},
            "management-recovery.sqlite3": {"metadata", "recovery_requests"},
        }[path.name]
        if not required <= tables:
            raise OriginalEvidenceUnavailable("original database schema incomplete")
    # A writer may expose a new sidecar during the immutable database read.
    # A clean pre-open observation does not prove settled original contents.
    _require_settled_sqlite(path)


def _check_owner_fence(path: Path, fd: int, expected: tuple[int, int]) -> None:
    opened = os.fstat(fd)
    if identity(path) != expected or (opened.st_dev, opened.st_ino) != expected:
        raise OriginalEvidenceUnavailable("original owner fence was replaced")
    locks = Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
    if not any(line.startswith("lock:") and " FLOCK  ADVISORY  READ " in line for line in locks):
        raise OriginalEvidenceUnavailable("consumer does not hold original-store owner fence")


def _derive(binding: HistoryBinding, fence: int, owner_fences: tuple[int, ...] = (), *, preserve_outcomes: bool = False) -> tuple[OriginalAttemptEvidence, tuple[OriginalGeneration, ...]]:
    """Enumerate original authority, independently of any submitted result."""
    files: dict[str, OriginalFile] = {}
    attempts: list[AttemptEvidence] = []
    generations: list[OriginalGeneration] = []
    unavailable: list[str] = []
    held_owners: list[tuple[Path, int, tuple[int, int]]] = []
    scanned_namespaces: dict[Path, tuple[tuple[int, int], tuple[str, ...]]] = {}
    frontier = ""

    def generation(intent: FreshInitializationIntent, outcome: str, stores: tuple[tuple[str, str], ...], owner: tuple[int, int] = (0, 0)) -> OriginalGeneration:
        return OriginalGeneration(
            intent.request_id,
            intent.new_instance,
            intent.new_container_id,
            intent.image_digest,
            intent.bootstrap_config_sha256,
            intent.management_config_sha256,
            intent.host_state_root,
            intent.state_device,
            intent.state_inode,
            intent.host_journal_root,
            intent.journal_device,
            intent.journal_inode,
            intent.host_oauth_root,
            intent.oauth_device,
            intent.oauth_inode,
            str(Path(intent.host_state_root) / ".supervisor-owner.lock"),
            *owner,
            outcome,
            stores,
        )

    def retain(path: Path) -> bytes:
        # Hash the exact bytes consumed by this derivation. A second read
        # could parse changed bytes under the first read's digest.
        data, (device, inode) = read_original(path)
        snapshot = OriginalFile(str(path), device, inode, hashlib.sha256(data).hexdigest())
        if str(path) in files and files[str(path)] != snapshot:
            raise OriginalEvidenceUnavailable("original changed during derivation")
        if any(item.path != snapshot.path and (item.device, item.inode) == (snapshot.device, snapshot.inode) for item in files.values()):
            raise OriginalEvidenceUnavailable("conflicting original file aliases")
        files[str(path)] = snapshot
        return data

    try:
        _check_fence(binding, fence)
        root = Path(binding.root)
        authority, events = read_frontier(root, binding)
        if binding.source != source_identity():
            raise OriginalEvidenceUnavailable("original source profile unavailable in this verifier")
        for path in sorted(root.iterdir()):
            retain(path)
        frontier = hashlib.sha256(canonical([event.record() for event in events])).hexdigest()
        by_attempt: dict[str, list[HistoryEvent]] = {}
        targets: dict[str, FreshInitializationIntent] = {}
        aliases: set[str] = set()
        baseline: tuple[str, str] | None = None
        for event in events:
            attempt = event.attempt
            if not isinstance(attempt, str) or str(uuid.UUID(attempt)) != attempt:
                raise OriginalEvidenceUnavailable("invalid original attempt identity")
            by_attempt.setdefault(attempt, []).append(event)
            if event.kind != "anchor":
                continue
            anchor = json.loads(event.value_json)
            if set(anchor) != {"intent", "lifetime", "stores", "state", "journal", "pristine"}:
                raise OriginalEvidenceUnavailable("incomplete original anchor")
            intent = parse_fresh_intent(anchor["intent"])
            if intent.request_id != attempt or intent.original_history != binding.root or intent.original_history_id != binding.history:
                raise OriginalEvidenceUnavailable("cross-history original anchor")
            protected_roots = [Path(value) for value in (intent.host_state_root, intent.host_journal_root, intent.host_oauth_root, intent.host_prior_journal_root) if value]
            if any(root == path or root in path.parents or path in root.parents for path in protected_roots):
                raise OriginalEvidenceUnavailable("original history overlaps protected management stores")
            validate_original_lifetime(anchor["lifetime"])
            if baseline is None:
                baseline = (intent.old_instance, intent.old_container_id)
            parent = targets.get(intent.old_instance)
            if parent is None:
                if (intent.old_instance, intent.old_container_id) != baseline:
                    raise OriginalEvidenceUnavailable("uncovered original predecessor")
            elif parent.new_container_id != intent.old_container_id:
                raise OriginalEvidenceUnavailable("conflicting predecessor alias")
            elif [prior.kind for prior in by_attempt[parent.request_id]] not in (["anchor", "claimed"], ["anchor", "claimed", "grant-possible"]):
                # Independent derivation must not accept a fabricated child of a
                # failed/partial original attempt, even from a signed-looking frontier.
                raise OriginalEvidenceUnavailable("refused or unproven original predecessor")
            keys = (intent.new_instance, intent.new_container_id, intent.host_state_root, intent.host_journal_root)
            if any(key in aliases for key in keys) or intent.new_instance == baseline[0] or intent.new_container_id == baseline[1]:
                raise OriginalEvidenceUnavailable("cyclic or conflicting target aliases")
            aliases.update(keys)
            targets[intent.new_instance] = intent
        for attempt, branch in by_attempt.items():
            kinds = [event.kind for event in branch]
            allowed = (["anchor"], ["anchor", "never-created"], ["anchor", "unresolved"], ["anchor", "claimed"], ["anchor", "claimed", "grant-possible"])
            if kinds not in allowed:
                raise OriginalEvidenceUnavailable("conflicting original attempt boundaries")
            anchor = json.loads(branch[0].value_json)
            intent = parse_fresh_intent(anchor["intent"])
            if kinds[-1] == "grant-possible":
                grant = json.loads(branch[-1].value_json)
                if not isinstance(grant, dict) or set(grant) != {"lifetime"}:
                    raise OriginalEvidenceUnavailable("original grant lifetime incomplete")
                try:
                    validate_original_lifetime(grant["lifetime"])
                except (ValueError, TypeError) as error:
                    raise OriginalEvidenceUnavailable("original grant lifetime incomplete") from error
            elif kinds[-1] == "unresolved" and json.loads(branch[-1].value_json) != {}:
                raise OriginalEvidenceUnavailable("original unresolved boundary malformed")
            expected_pairs = (
                ("child", str(Path(intent.host_state_root) / "management-child.sqlite3")),
                ("handoff", str(Path(intent.host_state_root) / "management-self-update.sqlite3")),
                ("recovery", str(Path(intent.host_state_root) / "management-recovery.sqlite3")),
                ("operations", str(Path(intent.host_journal_root) / "management-operations.sqlite3")),
            )
            attempts.append(AttemptEvidence(attempt, intent.old_instance, intent.new_instance, kinds[-1], tuple(sorted(expected_pairs))))
            try:
                state = Path(intent.host_state_root)
                journal = Path(intent.host_journal_root)
                state_id = identity(state, directory=True)
                journal_id = identity(journal, directory=True)
                if list(state_id) != anchor["state"] or list(journal_id) != anchor["journal"]:
                    raise OriginalEvidenceUnavailable("original target directory replaced")
                # A valid original may not silently acquire new records after its
                # roots were inspected. Bind the entire top-level namespace as
                # well as each file's retained bytes and identity.
                scanned_namespaces[state] = (state_id, tuple(sorted(path.name for path in state.iterdir())))
                scanned_namespaces[journal] = (journal_id, tuple(sorted(path.name for path in journal.iterdir())))
                # Independently derive the fixed stores, never trust supplied lists.
                expected = {
                    "child": str(state / "management-child.sqlite3"),
                    "handoff": str(state / "management-self-update.sqlite3"),
                    "recovery": str(state / "management-recovery.sqlite3"),
                    "operations": str(journal / "management-operations.sqlite3"),
                }
                if anchor["stores"] != expected:
                    raise OriginalEvidenceUnavailable("omitted or retargeted expected stores")
                if kinds[-1] == "never-created":
                    if anchor["pristine"] is not True or json.loads(branch[-1].value_json) != {"lifetime": anchor["lifetime"], "stores": expected} or scanned_namespaces[state][1] or scanned_namespaces[journal][1]:
                        raise OriginalEvidenceUnavailable("never-created original boundary conflicts with effects")
                    generations.append(generation(intent, "never-created", tuple(sorted(expected.items()))))
                    continue
                if "claimed" not in kinds:
                    unavailable.append(attempt + ": original admission unresolved")
                    continue
                encoded = retain(state / "fresh-initialization.json")
                claim = json.loads(branch[1].value_json)
                original = files[str(state / "fresh-initialization.json")]
                if parse_fresh_intent(json.loads(encoded)) != intent or claim != {"identity": [original.device, original.inode], "sha256": original.sha256}:
                    raise OriginalEvidenceUnavailable("original intent mutated")
                if kinds[-1] != "grant-possible":
                    unavailable.append(attempt + ": original grant boundary not crossed")
                    continue
                receipt = json.loads(retain(state / "fresh-original-stores.json"))
                try:
                    validate_original_lifetime(receipt["lifetime"])
                except (KeyError, TypeError, ValueError) as error:
                    raise OriginalEvidenceUnavailable("original bootstrap receipt lifetime incomplete") from error
                if (
                    set(receipt) != {"request_id", "history", "source", "intent_sha256", "lifetime", "owner_fence", "stores"}
                    or receipt["source"] != binding.source
                    or receipt["intent_sha256"] != original.sha256
                    or receipt["request_id"] != attempt
                    or receipt["history"] != binding.history
                    or set(receipt["stores"]) != set(expected)
                ):
                    raise OriginalEvidenceUnavailable("native original store identities incomplete")
                owner_path = state / ".supervisor-owner.lock"
                retain(owner_path)
                if list(identity(owner_path)) != receipt["owner_fence"]:
                    raise OriginalEvidenceUnavailable("original owner fence was replaced")
                matching = [fd for fd in owner_fences if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == identity(owner_path)]
                if len(matching) != 1:
                    raise OriginalEvidenceUnavailable("current original-store owner fence unavailable")
                owner_fd = matching[0]
                owner_identity = identity(owner_path)
                _check_owner_fence(owner_path, owner_fd, owner_identity)
                held_owners.append((owner_path, owner_fd, owner_identity))
                for name, store_path in expected.items():
                    retain(Path(store_path))
                    if list(identity(Path(store_path))) != receipt["stores"][name]:
                        raise OriginalEvidenceUnavailable("original store identity changed")
                    _database(Path(store_path))
                # Retain authoritative root-level state and journal records.
                # The Supervisor's exact state/logs directory is diagnostic,
                # not an original attempt/store or grant boundary. It must be
                # a real same-UID directory, never a symlink or aliased file.
                for directory in (state, journal):
                    for path in sorted(directory.iterdir()):
                        if directory == state and path.name == "logs":
                            info = path.lstat()
                            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                                raise OriginalEvidenceUnavailable("unsupported original diagnostics directory")
                            continue
                        if path.is_symlink() or path.is_dir():
                            raise OriginalEvidenceUnavailable("unsupported nested evidence profile")
                        retain(path)
                status = read_fresh_status(state, host=True)
                if preserve_outcomes:
                    # The actual native receipt follows this original claim.
                    # A missing claim is a provenance gap, not an unknown outcome.
                    retain(state / "fresh-launch-claimed.json")
                    if status["status"] not in ("succeeded", "unknown", "launched"):
                        raise OriginalEvidenceUnavailable("original first-launch boundary unavailable")
                    generations.append(generation(intent, str(status["status"]), tuple(sorted(expected.items())), owner_identity))
                elif status["status"] != "succeeded":
                    unavailable.append(attempt + ": original outcome remains " + str(status["status"]))
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
                unavailable.append(attempt + ": " + (str(error) or type(error).__name__))
        if not events and not (preserve_outcomes and authority.genesis_path):
            unavailable.append("prospective enrollment has no original attempts; legacy coverage unavailable")
        # Re-read every byte and identity; no consumer verdict supplies stability.
        for original_path, snapshot in files.items():
            if _snapshot(Path(original_path)) != snapshot:
                raise OriginalEvidenceUnavailable("original evidence mutated during observation")
        if read_frontier(root, binding) != (authority, events):
            raise OriginalEvidenceUnavailable("frontier advanced during observation")
        for directory, (expected_id, expected_names) in scanned_namespaces.items():
            if identity(directory, directory=True) != expected_id or tuple(sorted(path.name for path in directory.iterdir())) != expected_names:
                raise OriginalEvidenceUnavailable("original store namespace changed during derivation")
        _check_fence(binding, fence)
        for owner_path, owner_fd, owner_identity in held_owners:
            _check_owner_fence(owner_path, owner_fd, owner_identity)
        if source_identity() != binding.source:
            raise OriginalEvidenceUnavailable("original source changed during derivation")
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        unavailable.append(str(error) or type(error).__name__)
    return OriginalAttemptEvidence(binding=binding, frontier=frontier, attempts=tuple(attempts), originals=tuple(sorted(files.values(), key=lambda item: item.path)), unavailable=tuple(unavailable)), tuple(generations)


def derive(binding: HistoryBinding, fence: int, owner_fences: tuple[int, ...] = ()) -> OriginalAttemptEvidence:
    """Keep the prospective-attempt reader's existing outcome requirements."""
    return _derive(binding, fence, owner_fences)[0]


def derive_lineage(binding: HistoryBinding, fence: int, owner_fences: tuple[int, ...] = ()) -> OriginalLineageEvidence:
    """Join original genesis and every retained attempted successor read-only."""
    from .original_genesis import LAUNCH_NAME, NATIVE_NAME, native_paths, validate_history_genesis, validate_native_genesis

    evidence, successors = _derive(binding, fence, owner_fences, preserve_outcomes=True)
    originals = {item.path: item for item in evidence.originals}
    unavailable = list(evidence.unavailable)
    generations: list[OriginalGeneration] = []
    genesis = None
    try:
        _check_fence(binding, fence)
        authority, events = read_frontier(Path(binding.root), binding)
        if not authority.genesis_path or authority.profile != HISTORY_PROFILE:
            raise OriginalEvidenceUnavailable("full retained lineage requires original prospective genesis; earlier history remains unavailable")
        path = Path(authority.genesis_path)
        genesis = validate_history_genesis(Path(binding.root))
        if genesis.binding != binding or binding.source != source_identity():
            raise OriginalEvidenceUnavailable("original genesis source/history differs")
        state, journal, oauth = (genesis.root(role) for role in ("state", "journal", "oauth"))
        owner_path = Path(state.host_path) / ".supervisor-owner.lock"
        owner_identity = identity(owner_path)
        matching = [fd for fd in owner_fences if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == owner_identity]
        if len(matching) != 1:
            raise OriginalEvidenceUnavailable("current original genesis owner fence unavailable")
        _check_owner_fence(owner_path, matching[0], owner_identity)
        snapshots = [path, owner_path, Path(state.host_path) / NATIVE_NAME, Path(state.host_path) / LAUNCH_NAME]
        snapshots.extend(Path(item.path) for item in genesis.files)
        namespaces = {}
        for directory in (Path(state.host_path), Path(journal.host_path)):
            namespaces[directory] = (identity(directory, directory=True), tuple(sorted(item.name for item in directory.iterdir())))
            for item in sorted(directory.iterdir()):
                if directory == Path(state.host_path) and item.name == "logs":
                    info = item.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                        raise OriginalEvidenceUnavailable("original genesis diagnostics directory differs")
                    continue
                if item.is_symlink() or item.is_dir():
                    raise OriginalEvidenceUnavailable("unsupported nested original genesis evidence")
                snapshots.append(item)
        for original_path in snapshots:
            observed = _snapshot(original_path)
            if observed.path in originals and originals[observed.path] != observed:
                raise OriginalEvidenceUnavailable("original genesis changed during derivation")
            if any(item.path != observed.path and (item.device, item.inode) == (observed.device, observed.inode) for item in originals.values()):
                raise OriginalEvidenceUnavailable("original lineage file identity aliases")
            originals[observed.path] = observed
        validate_native_genesis(genesis, path, host=True)
        stores = native_paths(genesis, host=True)
        for store in stores.values():
            _database(store)
        # This is a factual original launch boundary. It never upgrades child,
        # handoff, recovery or management-operation unknowns into success.
        with closing(sqlite3.connect(stores["child"].as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            rows = connection.execute("SELECT instance_id,record_json FROM child_state").fetchall()
            if len(rows) > 1 or rows and rows[0][0] != genesis.instance_id:
                raise OriginalEvidenceUnavailable("original genesis child identity differs")
            outcome = "unknown" if not rows else json.loads(rows[0][1])["phase"]
            if outcome not in {"stopped", "starting", "running", "draining", "unknown", "needs_attention"}:
                raise OriginalEvidenceUnavailable("original genesis child outcome is malformed")
        generations.append(
            OriginalGeneration(
                genesis.authorization_id,
                genesis.instance_id,
                genesis.management_id,
                genesis.management_image_id,
                genesis.file("bootstrap").sha256,
                genesis.file("management").sha256,
                state.host_path,
                state.device,
                state.inode,
                journal.host_path,
                journal.device,
                journal.inode,
                oauth.host_path,
                oauth.device,
                oauth.inode,
                str(owner_path),
                *owner_identity,
                outcome,
                tuple(sorted((name, str(store)) for name, store in stores.items())),
                genesis.proxy_id,
                genesis.proxy_image_id,
            )
        )
        complete = (generations[0], *successors)
        expected_owners = {(item.owner_device, item.owner_inode) for item in complete if item.owner_inode > 0}
        observed_owners = [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in owner_fences]
        if len(observed_owners) != len(expected_owners) or set(observed_owners) != expected_owners:
            raise OriginalEvidenceUnavailable("original lineage owner descriptor set differs")
        by_instance = {item.instance_id: item for item in complete}
        for event in events:
            if event.kind != "anchor":
                continue
            intent = parse_fresh_intent(json.loads(event.value_json)["intent"])
            generation = by_instance.get(intent.new_instance)
            if generation is None or generation.outcome == "never-created":
                continue
            predecessor = by_instance.get(intent.old_instance)
            if predecessor is None or (intent.old_container_id, intent.owner_lock_device, intent.owner_lock_inode) != (predecessor.management_id, predecessor.owner_device, predecessor.owner_inode):
                raise OriginalEvidenceUnavailable("original native successor owner differs from its retained predecessor")
            if (intent.host_oauth_root, intent.oauth_device, intent.oauth_inode) != (oauth.host_path, oauth.device, oauth.inode) or identity(Path(intent.host_oauth_root), directory=True) != (oauth.device, oauth.inode):
                raise OriginalEvidenceUnavailable("original native successor retained OAuth root differs")
        for original_name, expected in originals.items():
            if _snapshot(Path(original_name)) != expected:
                raise OriginalEvidenceUnavailable("original lineage changed during derivation")
        for directory, (expected_id, names) in namespaces.items():
            if identity(directory, directory=True) != expected_id or tuple(sorted(item.name for item in directory.iterdir())) != names:
                raise OriginalEvidenceUnavailable("original genesis namespace changed during derivation")
        if read_frontier(Path(binding.root), binding) != (authority, events) or validate_history_genesis(Path(binding.root)) != genesis:
            raise OriginalEvidenceUnavailable("original lineage advanced during derivation")
        _check_fence(binding, fence)
        _check_owner_fence(owner_path, matching[0], owner_identity)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        unavailable.append(str(error) or type(error).__name__)
    return OriginalLineageEvidence(binding, HISTORY_PROFILE, genesis, evidence.frontier, evidence.attempts, tuple(generations) + successors, tuple(sorted(originals.values(), key=lambda item: item.path)), tuple(unavailable))


def verify_lineage(submitted: OriginalLineageEvidence, expected: HistoryBinding, fence: int, owner_fences: tuple[int, ...] = ()) -> OriginalLineageEvidence:
    """Independently reconstruct all originals at the consumer's current fence."""
    current = derive_lineage(expected, fence, owner_fences)
    if current != submitted or submitted.binding != expected:
        raise OriginalEvidenceUnavailable("submitted lineage is forged, stale, truncated or cross-deployment")
    if current.unavailable:
        raise OriginalEvidenceUnavailable("; ".join(current.unavailable))
    return current


def verify(submitted: OriginalAttemptEvidence, expected: HistoryBinding, fence: int, owner_fences: tuple[int, ...] = ()) -> OriginalAttemptEvidence:
    """Reopen originals under the caller's current fence; reject asserted success."""
    current = derive(expected, fence, owner_fences)
    if submitted != current or submitted.binding != expected:
        raise OriginalEvidenceUnavailable("submitted evidence is forged, stale, truncated or cross-deployment")
    if current.unavailable:
        raise OriginalEvidenceUnavailable("; ".join(current.unavailable))
    return current


def parse_evidence(data: bytes) -> OriginalAttemptEvidence:
    """Reject malformed submitted shapes before independent re-observation."""
    raw = json.loads(data)
    if not isinstance(raw, dict) or set(raw) != set(OriginalAttemptEvidence.__dataclass_fields__):
        raise OriginalEvidenceUnavailable("unsupported submitted evidence schema")
    binding = raw["binding"]
    if not isinstance(binding, dict) or set(binding) != set(HistoryBinding.__dataclass_fields__) or any(not isinstance(value, str) for value in binding.values()):
        raise OriginalEvidenceUnavailable("unsupported submitted binding schema")
    if any(not isinstance(raw[field], str) for field in ("profile", "earlier_history", "frontier")):
        raise OriginalEvidenceUnavailable("unsupported submitted evidence fields")
    if not isinstance(raw["attempts"], list) or not isinstance(raw["originals"], list) or not isinstance(raw["unavailable"], list):
        raise OriginalEvidenceUnavailable("unsupported submitted evidence collections")
    if any(not isinstance(reason, str) for reason in raw["unavailable"]):
        raise OriginalEvidenceUnavailable("unsupported submitted unavailable reasons")
    attempts = []
    for item in raw["attempts"]:
        if not isinstance(item, dict) or set(item) != set(AttemptEvidence.__dataclass_fields__):
            raise OriginalEvidenceUnavailable("unsupported submitted attempt schema")
        if any(not isinstance(item[field], str) for field in ("request_id", "predecessor", "target", "state")):
            raise OriginalEvidenceUnavailable("unsupported submitted attempt fields")
        pairs = item["expected_stores"]
        if not isinstance(pairs, list) or any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(value, str) for value in pair) for pair in pairs):
            raise OriginalEvidenceUnavailable("unsupported submitted expected stores")
        attempts.append(AttemptEvidence(item["request_id"], item["predecessor"], item["target"], item["state"], tuple(tuple(pair) for pair in pairs)))
    originals = []
    for item in raw["originals"]:
        if not isinstance(item, dict) or set(item) != set(OriginalFile.__dataclass_fields__):
            raise OriginalEvidenceUnavailable("unsupported submitted original file schema")
        if not isinstance(item["path"], str) or not isinstance(item["sha256"], str) or any(type(item[field]) is not int for field in ("device", "inode")):
            raise OriginalEvidenceUnavailable("unsupported submitted original file fields")
        originals.append(OriginalFile(**item))
    return OriginalAttemptEvidence(
        binding=HistoryBinding(**binding),
        profile=raw["profile"],
        earlier_history=raw["earlier_history"],
        frontier=raw["frontier"],
        attempts=tuple(attempts),
        originals=tuple(originals),
        unavailable=tuple(raw["unavailable"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read original attempt evidence; no recovery or launch authority")
    parser.add_argument("--binding", required=True, type=Path, help="original enrollment binding JSON")
    parser.add_argument("--owner-fd", action="append", type=int, default=[], help="inherited currently held shared original-store owner fence")
    parser.add_argument("--submitted", type=Path, help="re-read and verify a previously derived object")
    args = parser.parse_args()
    try:
        binding = HistoryBinding(**json.loads(args.binding.read_bytes()))
        with observe(binding) as fence:
            evidence = verify(parse_evidence(args.submitted.read_bytes()), binding, fence, tuple(args.owner_fd)) if args.submitted else derive(binding, fence, tuple(args.owner_fd))
            print(json.dumps(asdict(evidence), sort_keys=True))
            return 1 if evidence.unavailable else 0
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, "original attempt coverage unavailable: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
