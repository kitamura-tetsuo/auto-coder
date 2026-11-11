"""Prospective original fresh-attempt admission facts, never recovery authority.

Only explicit intents enroll. The history lock serializes original admission and
host grants; an incomplete write poisons the frontier rather than being repaired.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from loguru import logger

from .fresh_initialization import FreshInitializationIntent, parse_fresh_intent

PROFILE = "fresh-original-attempts-v1"
MAX_EVENTS = 4096


class OriginalEvidenceUnavailable(ValueError):
    """Missing original authority must never become an empty history."""


@dataclass(frozen=True)
class HistoryBinding:
    root: str
    deployment: str
    history: str
    source: str


@dataclass(frozen=True)
class HistoryAuthority:
    binding: HistoryBinding
    profile: str
    root: tuple[int, int]
    lock: tuple[int, int]


@dataclass(frozen=True)
class HistoryEvent:
    previous: str
    sequence: int
    kind: str
    attempt: str
    value_json: str

    def record(self) -> dict[str, object]:
        return {"previous": self.previous, "sequence": self.sequence, "kind": self.kind, "attempt": self.attempt, "value": json.loads(self.value_json)}


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def source_identity() -> str:
    """Bind the executing Python package, not a caller's claimed source SHA."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if path.is_symlink():
            raise OriginalEvidenceUnavailable("source contains a symlink")
        relative = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big") + relative + len(data).to_bytes(8, "big") + data)
    return "sha256:" + digest.hexdigest()


def identity(path: Path, *, directory: bool = False) -> tuple[int, int]:
    if not path.is_absolute() or path != path.resolve():
        raise OriginalEvidenceUnavailable("noncanonical protected path")
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    mode = stat.S_IMODE(info.st_mode)
    if not expected(info.st_mode) or info.st_uid != os.geteuid() or (mode != 0o700 if directory else mode != 0o600) or (not directory and info.st_nlink != 1):
        raise OriginalEvidenceUnavailable("unprotected original evidence: " + str(path))
    return info.st_dev, info.st_ino


def read_original(path: Path) -> tuple[bytes, tuple[int, int]]:
    expected = identity(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (before.st_dev, before.st_ino) != expected or before.st_size > 8 * 1024 * 1024:
            raise OriginalEvidenceUnavailable("original evidence changed or oversized")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        after = os.fstat(fd)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields) or identity(path) != expected or len(data) != before.st_size:
            raise OriginalEvidenceUnavailable("original evidence changed while reading")
        return data, expected
    finally:
        os.close(fd)


def _sync(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create(root: Path, name: str, value: object) -> None:
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    _sync(root)


def enroll(root: Path, deployment: str) -> HistoryBinding:
    """Explicitly begin a new prospective scope, without inspecting old gaps."""
    root_id = identity(root, directory=True)
    if not deployment or len(deployment) > 128 or any(root.iterdir()):
        raise OriginalEvidenceUnavailable("new empty history and deployment identity required")
    # O_EXCL arbitrates concurrent enrollment. Never recover a partial enrollment.
    _create(root, "history.lock", {})
    binding = HistoryBinding(str(root), deployment, str(uuid.uuid4()), source_identity())
    _create(root, "authority.json", {"binding": asdict(binding), "profile": PROFILE, "root": root_id, "lock": identity(root / "history.lock")})
    _create(root, "frontier.json", {"count": 0, "digest": ""})
    return binding


@contextmanager
def history_lock(root: Path, *, exclusive: bool) -> Iterator[int]:
    identity(root, directory=True)
    expected = identity(root / "history.lock")
    fd = os.open(root / "history.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != expected:
            raise OriginalEvidenceUnavailable("history lock replaced")
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            logger.bind(original_history=str(root), original_boundary="contested", evidence_only=True).warning("Original attempt history admission contested: {}", root)
            raise OriginalEvidenceUnavailable("history admission contested") from error
        if identity(root / "history.lock") != expected:
            raise OriginalEvidenceUnavailable("history lock replaced")
        yield fd
    finally:
        os.close(fd)


def read_frontier(root: Path, expected: HistoryBinding | None = None) -> tuple[HistoryAuthority, list[HistoryEvent]]:
    """Validate the whole bounded append-only event namespace, never truncate."""
    raw, _ = read_original(root / "authority.json")
    authority = json.loads(raw)
    if not isinstance(authority, dict) or set(authority) != {"binding", "profile", "root", "lock"} or authority["profile"] != PROFILE or canonical(authority) != raw:
        raise OriginalEvidenceUnavailable("unsupported history profile")
    binding = HistoryBinding(**authority["binding"])
    if binding.root != str(root) or (expected is not None and binding != expected) or authority["root"] != list(identity(root, directory=True)) or authority["lock"] != list(identity(root / "history.lock")):
        raise OriginalEvidenceUnavailable("history/source/deployment identity differs")
    frontier_raw, _ = read_original(root / "frontier.json")
    frontier = json.loads(frontier_raw)
    if not isinstance(frontier, dict) or set(frontier) != {"count", "digest"} or type(frontier["count"]) is not int or not 0 <= frontier["count"] <= MAX_EVENTS or canonical(frontier) != frontier_raw:
        raise OriginalEvidenceUnavailable("frontier is malformed or unbounded")
    names = {"history.lock", "authority.json", "frontier.json"} | {f"{i:08d}.json" for i in range(frontier["count"])}
    if {p.name for p in root.iterdir()} != names:
        raise OriginalEvidenceUnavailable("partial, omitted or conflicting frontier")
    previous = ""
    events = []
    for index in range(frontier["count"]):
        encoded, _ = read_original(root / f"{index:08d}.json")
        event = json.loads(encoded)
        if not isinstance(event, dict) or set(event) != {"previous", "sequence", "kind", "attempt", "value"} or event["previous"] != previous or type(event["sequence"]) is not int or event["sequence"] != index or canonical(event) != encoded:
            raise OriginalEvidenceUnavailable("corrupt or cyclic original frontier")
        if event["kind"] == "anchor":
            anchor = event["value"]
            if not isinstance(anchor, dict) or set(anchor) != {"intent", "lifetime", "stores", "state", "journal", "pristine"}:
                raise OriginalEvidenceUnavailable("incomplete original anchor")
            try:
                original_intent = parse_fresh_intent(anchor["intent"])
            except (KeyError, TypeError, ValueError) as error:
                raise OriginalEvidenceUnavailable("incomplete original anchor") from error
            if original_intent.request_id != event["attempt"]:
                raise OriginalEvidenceUnavailable("original anchor attempt mismatch")
        previous = hashlib.sha256(encoded).hexdigest()
        events.append(HistoryEvent(event["previous"], index, event["kind"], event["attempt"], canonical(event["value"]).decode()))
    if previous != frontier["digest"]:
        raise OriginalEvidenceUnavailable("truncated original frontier")
    return HistoryAuthority(binding, authority["profile"], tuple(authority["root"]), tuple(authority["lock"])), events


def _append(root: Path, kind: str, attempt: str, value: object) -> None:
    _, events = read_frontier(root)
    if len(events) >= MAX_EVENTS:
        raise OriginalEvidenceUnavailable("history capacity exhausted; no truncation permitted")
    previous = hashlib.sha256(canonical(events[-1].record())).hexdigest() if events else ""
    event = {"previous": previous, "sequence": len(events), "kind": kind, "attempt": attempt, "value": value}
    _create(root, f"{len(events):08d}.json", event)
    # A crash anywhere before the directory fsync leaves unavailable evidence.
    _create(root, "frontier.pending", {"count": len(events) + 1, "digest": hashlib.sha256(canonical(event)).hexdigest()})
    os.replace(root / "frontier.pending", root / "frontier.json")
    _sync(root)
    logger.bind(original_history=str(root), original_attempt=attempt, original_boundary=kind, evidence_only=True).info("Original attempt provenance: history={} attempt={} boundary={}", root, attempt, kind)


def _lifetime() -> dict[str, object]:
    fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    start = fields[19]
    if not start.isdecimal() or int(start) <= 0:
        raise OriginalEvidenceUnavailable("producer lifetime unavailable")
    return {"pid": os.getpid(), "start": start, "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "nonce": str(uuid.uuid4())}


def expected_stores(intent: FreshInitializationIntent) -> dict[str, str]:
    # Kept at the original bootstrap's fixed store boundary, not consumer input.
    state = Path(intent.host_state_root)
    return {
        "child": str(state / "management-child.sqlite3"),
        "handoff": str(state / "management-self-update.sqlite3"),
        "recovery": str(state / "management-recovery.sqlite3"),
        "operations": str(Path(intent.host_journal_root) / "management-operations.sqlite3"),
    }


@contextmanager
def capture_claim(intent: FreshInitializationIntent) -> Iterator[None]:
    root = Path(intent.original_history)
    protected_roots = [Path(value) for value in (intent.host_state_root, intent.host_journal_root, intent.host_oauth_root, intent.host_prior_journal_root) if value]
    if any(root == path or root in path.parents or path in root.parents for path in protected_roots):
        raise OriginalEvidenceUnavailable("original history overlaps protected management stores")
    with history_lock(root, exclusive=True):
        authority, events = read_frontier(root)
        if authority.binding.history != intent.original_history_id or authority.binding.source != source_identity():
            raise OriginalEvidenceUnavailable("original history source or enrollment differs")
        anchors = [event for event in events if event.kind == "anchor"]
        keys = {intent.new_instance, intent.new_container_id, intent.host_state_root, intent.host_journal_root}
        prior_targets = [json.loads(event.value_json)["intent"] for event in anchors]
        if any(event.attempt == intent.request_id for event in anchors) or any(keys & {prior["new_instance"], prior["new_container_id"], prior["host_state_root"], prior["host_journal_root"]} for prior in prior_targets):
            raise OriginalEvidenceUnavailable("original attempt or target already anchored; no replay")
        if anchors:
            baseline = json.loads(anchors[0].value_json)["intent"]
            predecessors = [(baseline["old_instance"], baseline["old_container_id"])]
            for prior_event in anchors:
                prior_kinds = [event.kind for event in events if event.attempt == prior_event.attempt]
                # A refused, interrupted or merely anchored attempt never established
                # the next usable original predecessor, even if its target was named.
                if prior_kinds in (["anchor", "claimed"], ["anchor", "claimed", "grant-possible"]):
                    prior = json.loads(prior_event.value_json)["intent"]
                    predecessors.append((prior["new_instance"], prior["new_container_id"]))
            if (intent.old_instance, intent.old_container_id) not in predecessors or intent.new_instance == baseline["old_instance"] or intent.new_container_id == baseline["old_container_id"]:
                raise OriginalEvidenceUnavailable("uncovered, refused or cyclic original predecessor")
        if any(prior["old_instance"] == intent.new_instance for prior in prior_targets):
            raise OriginalEvidenceUnavailable("attempt would cycle covered ancestry")
        stores = expected_stores(intent)
        # Enroll only pristine targets. Missing legacy stores are never backfilled.
        state_id = identity(Path(intent.host_state_root), directory=True)
        journal_id = identity(Path(intent.host_journal_root), directory=True)
        if state_id != (intent.state_device, intent.state_inode) or journal_id != (intent.journal_device, intent.journal_inode):
            raise OriginalEvidenceUnavailable("intended root identities differ before anchor")
        prior_root_ids = {tuple(json.loads(event.value_json)[field]) for event in anchors for field in ("state", "journal")}
        if state_id == journal_id or state_id in prior_root_ids or journal_id in prior_root_ids:
            raise OriginalEvidenceUnavailable("original target aliases a protected mounted root")
        pristine = not any(Path(intent.host_state_root).iterdir()) and not any(Path(intent.host_journal_root).iterdir())
        anchor = {"intent": asdict(intent), "lifetime": _lifetime(), "stores": stores, "state": state_id, "journal": journal_id, "pristine": pristine}
        _append(root, "anchor", intent.request_id, anchor)
        try:
            if not pristine:
                raise OriginalEvidenceUnavailable("original target is not pristine")
            yield
        except BaseException:
            # Affirmative original pre-grant boundary, serialized with all grants.
            # No intent may have been written: a partial claim stays unresolved.
            if pristine and not any(Path(intent.host_state_root).iterdir()) and not any(Path(intent.host_journal_root).iterdir()) and identity(Path(intent.host_state_root), directory=True) == state_id and identity(Path(intent.host_journal_root), directory=True) == journal_id:
                _append(root, "never-created", intent.request_id, {"lifetime": anchor["lifetime"], "stores": stores})
            else:
                _append(root, "unresolved", intent.request_id, {})
            raise
        else:
            encoded, file_id = read_original(Path(intent.host_state_root) / "fresh-initialization.json")
            _append(root, "claimed", intent.request_id, {"identity": file_id, "sha256": hashlib.sha256(encoded).hexdigest()})


@contextmanager
def grant_boundary(intent: FreshInitializationIntent) -> Iterator[None]:
    """Hold admission across the host guard, including upgrade launch callbacks."""
    if not intent.original_history:
        yield
        return
    root = Path(intent.original_history)
    with history_lock(root, exclusive=True):
        authority, events = read_frontier(root)
        if authority.binding.history != intent.original_history_id or authority.binding.source != source_identity():
            raise OriginalEvidenceUnavailable("grant source or history differs")
        branch = [event for event in events if event.attempt == intent.request_id]
        if [event.kind for event in branch] != ["anchor", "claimed"] or json.loads(branch[0].value_json)["intent"] != asdict(intent):
            raise OriginalEvidenceUnavailable("grant has no original complete one-time admission")
        encoded, file_id = read_original(Path(intent.host_state_root) / "fresh-initialization.json")
        if json.loads(branch[-1].value_json) != {"identity": list(file_id), "sha256": hashlib.sha256(encoded).hexdigest()}:
            raise OriginalEvidenceUnavailable("original claim changed before grant")
        # Persist before any supported callback can launch a supervisor or grant.
        # Interruptions are unresolved; this fact is not proof of grant delivery.
        _append(root, "grant-possible", intent.request_id, {"lifetime": _lifetime()})
        yield


def record_original_stores(state_root: Path, intent: FreshInitializationIntent) -> None:
    """Called by the actual bootstrap after native creation, before child launch."""
    if not intent.original_history:
        return
    from .fresh_initialization import _write_once

    # Container paths share the exact mounted inodes recorded by the host intent.
    paths = {
        "child": state_root / "management-child.sqlite3",
        "handoff": state_root / "management-self-update.sqlite3",
        "recovery": state_root / "management-recovery.sqlite3",
        "operations": Path(intent.journal_root) / "management-operations.sqlite3",
    }
    from .fresh_host_observation import _assert_pid_flock

    descriptor = os.open(state_root / ".supervisor-owner.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        _assert_pid_flock(os.getpid(), descriptor, "WRITE")
    finally:
        os.close(descriptor)
    encoded = (state_root / "fresh-initialization.json").read_bytes()
    _write_once(
        state_root,
        "fresh-original-stores.json",
        {
            "request_id": intent.request_id,
            "history": intent.original_history_id,
            "source": source_identity(),
            "intent_sha256": hashlib.sha256(encoded).hexdigest(),
            "lifetime": _lifetime(),
            "owner_fence": identity(state_root / ".supervisor-owner.lock"),
            "stores": {name: identity(path) for name, path in paths.items()},
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Enroll an empty prospective original history; no deployment or recovery")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--deployment", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(asdict(enroll(args.root, args.deployment)), sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, "original attempt enrollment unavailable: " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
