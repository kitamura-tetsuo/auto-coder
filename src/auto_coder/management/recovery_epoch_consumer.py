"""Retain recovery issuer/consumer custody before any child launch authority.

This helper does not claim a child, create stores, launch, or declare readiness.
The owning Supervisor must retain the returned handle until it can safely end
custody. A current-host failure never implicitly releases already received FDs.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import select
import socket
import stat
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .archived_exclusions import unique_pairs
from .original_attempts import read_original
from .recovery_epoch_peer import CHALLENGE_SCHEMA, MAX_MESSAGE_BYTES, MAX_RESOURCES, OFFER_SCHEMA, RECEIPT_SCHEMA, HeldFence, KernelLock, PeerUnavailable, ResourceIdentity, _name, _reject_constant, _token, encode_peer_message, inspect_held_resource, inspect_kernel_lock, receive_peer_message

CONSUMER_CONFIG = Path("/bootstrap/recovery-epoch.json")
ISSUER_DIRECTORY = Path("/management-recovery")
ISSUER_SOCKET = ISSUER_DIRECTORY / "issuer.sock"
GRANT_SCHEMA = "management-recovery-epoch-grant-v1"
# Linux UAPI asm-generic/socket.h; unavailable kernels refuse without a fallback.
_SO_PEERPIDFD = getattr(socket, "SO_PEERPIDFD", 77)


@dataclass(frozen=True)
class OriginalFence:
    name: str
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class TargetOwner:
    name: str
    device: int
    inode: int


@dataclass(frozen=True)
class TransferredResource:
    name: str
    kind: Literal["transferred-ex", "network-namespace"]
    device: int
    inode: int


@dataclass(frozen=True)
class ConsumerConfiguration:
    schema_version: int
    request_id: str
    authorization_id: str
    target_id: str
    issuer_uid: int
    socket_directory_device: int
    socket_directory_inode: int
    original_fences: tuple[OriginalFence, ...]
    target_owner: TargetOwner
    transferred_resources: tuple[TransferredResource, ...]


@dataclass(frozen=True)
class RecoveryGrant:
    request_id: str
    authorization_id: str
    target_id: str
    challenge: str
    receipt_nonce: str
    inventory_sha256: str
    binding_sha256: str
    consumed_sha256: str
    receipt_sha256: str
    host_boot_id: str
    host_pid: int
    host_start_token: str


@dataclass(frozen=True)
class ConsumerTransfer:
    expected: TransferredResource
    descriptor: int
    mode: int
    uid: int
    link: str
    lock: KernelLock | None


def _identity(value: object) -> bool:
    return type(value) is int and value > 0


def parse_consumer_configuration(encoded: bytes) -> ConsumerConfiguration:
    """Parse a complete pinned target configuration, not host authorization."""
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise PeerUnavailable("consumer configuration exceeds its bound")
    value = json.loads(encoded, object_pairs_hook=unique_pairs, parse_constant=_reject_constant)
    if not isinstance(value, dict) or set(value) != set(ConsumerConfiguration.__dataclass_fields__):
        raise PeerUnavailable("consumer configuration schema differs")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or type(value["issuer_uid"]) is not int or value["issuer_uid"] < 0:
        raise PeerUnavailable("consumer configuration version or issuer differs")
    if not all(_name(value[name]) for name in ("request_id", "authorization_id", "target_id")) or not all(_identity(value[name]) for name in ("socket_directory_device", "socket_directory_inode")):
        raise PeerUnavailable("consumer binding or socket directory identity is invalid")
    originals, transferred, target = value["original_fences"], value["transferred_resources"], value["target_owner"]
    if not isinstance(originals, list) or not originals or not isinstance(transferred, list) or not transferred or len(originals) + len(transferred) + 1 > MAX_RESOURCES:
        raise PeerUnavailable("consumer resource set is incomplete or oversized")
    if not isinstance(target, dict) or set(target) != set(TargetOwner.__dataclass_fields__):
        raise PeerUnavailable("consumer target owner binding is incomplete")
    for item in originals:
        if not isinstance(item, dict) or set(item) != set(OriginalFence.__dataclass_fields__) or not isinstance(item.get("path"), str) or not Path(item["path"]).is_absolute() or "\x00" in item["path"]:
            raise PeerUnavailable("consumer original fence binding is incomplete")
    for item in transferred:
        if not isinstance(item, dict) or set(item) != set(TransferredResource.__dataclass_fields__) or item.get("kind") not in ("transferred-ex", "network-namespace"):
            raise PeerUnavailable("consumer transferred resource binding is incomplete")
    entries = [*originals, target, *transferred]
    if any(not _name(item["name"]) or not _identity(item["device"]) or not _identity(item["inode"]) for item in entries):
        raise PeerUnavailable("consumer resource identity is invalid")
    if len({item["name"] for item in entries}) != len(entries) or len({(item["device"], item["inode"]) for item in entries}) != len(entries) or len({item["path"] for item in originals}) != len(originals):
        raise PeerUnavailable("consumer resource aliases are conflicting")
    if not any(item["kind"] == "transferred-ex" for item in transferred):
        raise PeerUnavailable("consumer deployment/controller exclusive custody is missing")
    return ConsumerConfiguration(
        value["schema_version"],
        value["request_id"],
        value["authorization_id"],
        value["target_id"],
        value["issuer_uid"],
        value["socket_directory_device"],
        value["socket_directory_inode"],
        tuple(OriginalFence(**item) for item in originals),
        TargetOwner(**target),
        tuple(TransferredResource(**item) for item in transferred),
    )


def _acquire_originals(configuration: ConsumerConfiguration) -> tuple[HeldFence, ...]:
    fences: list[HeldFence] = []
    try:
        for expected in configuration.original_fences:
            path = Path(expected.path)
            if path != path.resolve(strict=True):
                raise PeerUnavailable("consumer original fence path is not canonical")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            fences.append(HeldFence(expected.name, descriptor, "original-sh"))
            actual = os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino) != (expected.device, expected.inode):
                raise PeerUnavailable("consumer original fence object differs")
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            inspect_held_resource(fences[-1])
        return tuple(fences)
    except BaseException:
        for fence in fences:
            os.close(fence.descriptor)
        raise


def _target(configuration: ConsumerConfiguration, descriptor: int) -> HeldFence:
    # Using the independent EX inspection also rejects an inherited target lock.
    fence = HeldFence(configuration.target_owner.name, descriptor, "transferred-ex")
    actual = inspect_held_resource(fence)
    if (actual.device, actual.inode) != (configuration.target_owner.device, configuration.target_owner.inode):
        raise PeerUnavailable("consumer target owner differs from the pinned configuration")
    return fence


def _transfer(expected: TransferredResource, descriptor: int, issuer_pid: int, issuer_uid: int) -> ConsumerTransfer:
    info = os.fstat(descriptor)
    link = os.readlink(f"/proc/self/fd/{descriptor}")
    if (info.st_dev, info.st_ino) != (expected.device, expected.inode) or os.get_inheritable(descriptor):
        raise PeerUnavailable("received resource identity or close-on-exec flag differs")
    if expected.kind == "network-namespace":
        if link != f"net:[{info.st_ino}]":
            raise PeerUnavailable("received resource is not the pinned network namespace")
        lock = None
    else:
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_uid != issuer_uid:
            raise PeerUnavailable("received exclusive fence protection differs")
        # A host outside the consumer PID namespace maps to PID 0 in fdinfo.
        # SO_PEERPIDFD, obtained independently from this socket, pins that peer.
        lock = inspect_kernel_lock(os.getpid(), descriptor, info.st_dev, info.st_ino, allow_unmapped_owner=issuer_pid == 0)
        if lock is None or lock.mode != "WRITE" or lock.pid != issuer_pid:
            raise PeerUnavailable("received exclusive fence is not held by the authenticated issuer")
    return ConsumerTransfer(expected, descriptor, info.st_mode, info.st_uid, link, lock)


class RecoveryEpochConsumer:
    """Retained prelaunch custody; the owner alone decides when it may close."""

    def __init__(self, connection: socket.socket, configuration: ConsumerConfiguration, target_owner_fd: int, *, timeout: float = 30, configuration_file: Path | None = None) -> None:
        self.connection = connection
        self.configuration = configuration
        self.grant: RecoveryGrant | None = None
        self.challenge = secrets.token_hex(32)
        self.receipt_nonce = ""
        self._timeout = timeout
        self._configuration_file = configuration_file
        self._configuration_bytes: bytes | None = None
        self._configuration_identity: tuple[int, int] | None = None
        self._issuer_pidfd = -1
        self._issuer_pid = -1
        self._closed = False
        self._originals: tuple[HeldFence, ...] = ()
        self._target: HeldFence | None = None
        self._owned: list[int] = []
        self._initial: tuple[ResourceIdentity, ...] = ()
        self._transfers: tuple[ConsumerTransfer, ...] = ()
        try:
            if type(timeout) not in (int, float) or not 0 < timeout <= 3600 or connection.family != socket.AF_UNIX or connection.type not in (socket.SOCK_STREAM, socket.SOCK_SEQPACKET):
                raise PeerUnavailable("consumer requires a bounded authenticated local exchange")
            if configuration_file is not None:
                self._configuration_bytes, self._configuration_identity = read_original(configuration_file)
                if parse_consumer_configuration(self._configuration_bytes) != configuration:
                    raise PeerUnavailable("consumer configuration changed before connection")
            self._target = _target(configuration, target_owner_fd)
            self._originals = _acquire_originals(configuration)
            self._initial = tuple(inspect_held_resource(fence) for fence in (*self._originals, self._target))
            self._issuer_pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid != configuration.issuer_uid or self._issuer_pid < 0:
                raise PeerUnavailable("connected issuer credentials differ")
            self._issuer_pidfd = connection.getsockopt(socket.SOL_SOCKET, _SO_PEERPIDFD)
            os.set_inheritable(self._issuer_pidfd, False)
            if os.readlink(f"/proc/self/fd/{self._issuer_pidfd}") != "anon_inode:[pidfd]":
                raise PeerUnavailable("connected issuer has no kernel peer lifetime pin")
            self._check_issuer()
            self._exchange()
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, ValueError, TypeError, KeyError)) and not isinstance(error, PeerUnavailable):
                raise PeerUnavailable("recovery consumer custody is unavailable") from error
            raise

    def __enter__(self) -> RecoveryEpochConsumer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def issuer_pidfd(self) -> int:
        return self._issuer_pidfd

    @property
    def retained_descriptors(self) -> tuple[int, ...]:
        return (*[fence.descriptor for fence in self._originals], *self._owned)

    def close(self) -> None:
        """Release only when the owning lifecycle has established it is safe."""
        if not self._closed:
            self._closed = True
            self.connection.close()
            for descriptor in self.retained_descriptors:
                os.close(descriptor)
            self._originals = ()
            self._owned.clear()
            if self._issuer_pidfd >= 0:
                os.close(self._issuer_pidfd)
                self._issuer_pidfd = -1

    def _check_issuer(self) -> None:
        if self._closed or self._issuer_pidfd < 0:
            raise PeerUnavailable("issuer custody handle is closed")
        pid, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        poll = select.poll()
        poll.register(self._issuer_pidfd, select.POLLIN)
        if (pid, uid) != (self._issuer_pid, self.configuration.issuer_uid) or poll.poll(0):
            raise PeerUnavailable("the authenticated issuer lifetime ended")
        channel = select.poll()
        channel.register(self.connection, select.POLLERR | select.POLLHUP | select.POLLRDHUP)
        if channel.poll(0):
            raise PeerUnavailable("the authenticated issuer channel ended")

    def _binding(self) -> dict[str, object]:
        return {"request_id": self.configuration.request_id, "authorization_id": self.configuration.authorization_id, "target_id": self.configuration.target_id, "challenge": self.challenge}

    def _exchange(self) -> None:
        deadline = time.monotonic() + self._timeout
        self.connection.settimeout(min(self._timeout, 60))
        self.connection.sendall(encode_peer_message({"schema": CHALLENGE_SCHEMA, **self._binding()}))
        offer, descriptors = receive_peer_message(self.connection, timeout=self._timeout, allow_rights=True, deadline=deadline)
        self._owned.extend(descriptors)
        if set(offer) != {"schema", *self._binding(), "receipt_nonce", "resources"} or offer["schema"] != OFFER_SCHEMA or any(offer[name] != value for name, value in self._binding().items()) or not _token(offer["receipt_nonce"]):
            raise PeerUnavailable("issuer custody offer is stale or bound to another target")
        self.receipt_nonce = str(offer["receipt_nonce"])
        resources = offer["resources"]
        expected: dict[str, tuple[str, int, int]] = {fence.name: ("original-sh", fence.device, fence.inode) for fence in self.configuration.original_fences}
        owner = self.configuration.target_owner
        expected[owner.name] = ("target-ex", owner.device, owner.inode)
        expected.update({resource.name: (resource.kind, resource.device, resource.inode) for resource in self.configuration.transferred_resources})
        if not isinstance(resources, list) or len(resources) != len(expected):
            raise PeerUnavailable("issuer omitted required custody resources")
        seen: set[str] = set()
        transferred: dict[str, int] = {}
        for resource in resources:
            if not isinstance(resource, dict) or set(resource) != {"name", "kind", "device", "inode", "transfer_index"} or not isinstance(resource["name"], str):
                raise PeerUnavailable("issuer custody resource schema differs")
            name = resource["name"]
            if name in seen or name not in expected or (resource["kind"], resource["device"], resource["inode"]) != expected[name] or type(resource["device"]) is not int or type(resource["inode"]) is not int:
                raise PeerUnavailable("issuer custody resource differs from the pinned configuration")
            seen.add(name)
            index = resource["transfer_index"]
            if resource["kind"] in ("transferred-ex", "network-namespace"):
                if type(index) is not int or not 0 <= index < len(descriptors) or index in transferred.values():
                    raise PeerUnavailable("issuer transfer descriptor mapping is incomplete or repeated")
                transferred[name] = index
            elif index is not None:
                raise PeerUnavailable("original shared or target exclusive custody must be independently acquired")
        if seen != set(expected) or set(transferred.values()) != set(range(len(descriptors))) or len(descriptors) != len(self.configuration.transferred_resources):
            raise PeerUnavailable("issuer delivered an incomplete or unexpected descriptor set")
        self._transfers = tuple(_transfer(resource, descriptors[transferred[resource.name]], self._issuer_pid, self.configuration.issuer_uid) for resource in self.configuration.transferred_resources)
        self.check_current()
        assert self._target is not None
        received = [{"name": fence.name, "fd": fence.descriptor} for fence in (*self._originals, self._target)]
        received.extend({"name": item.expected.name, "fd": item.descriptor} for item in self._transfers)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PeerUnavailable("consumer verification exceeded its bounded deadline")
        self.connection.settimeout(min(remaining, 60))
        self.connection.sendall(encode_peer_message({"schema": RECEIPT_SCHEMA, **self._binding(), "receipt_nonce": self.receipt_nonce, "descriptors": received}))
        grant, _ = receive_peer_message(self.connection, timeout=self._timeout, deadline=deadline)
        expected_fields = {"schema", "granted", *RecoveryGrant.__dataclass_fields__}
        if set(grant) != expected_fields or grant["schema"] != GRANT_SCHEMA or grant["granted"] != "recovery-epoch-consumed" or any(grant[name] != value for name, value in self._binding().items()) or grant["receipt_nonce"] != self.receipt_nonce:
            raise PeerUnavailable("issuer grant is not the exact current consumed challenge")
        if not all(_token(grant[name]) for name in ("inventory_sha256", "binding_sha256", "consumed_sha256", "receipt_sha256")):
            raise PeerUnavailable("issuer grant durable identities are incomplete")
        boot, pid, started = grant["host_boot_id"], grant["host_pid"], grant["host_start_token"]
        if not isinstance(boot, str) or str(uuid.UUID(boot)) != boot or boot != Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or not _identity(pid) or not isinstance(started, str) or re.fullmatch(r"[1-9][0-9]*", started) is None:
            raise PeerUnavailable("issuer grant original lifetime is malformed or from another boot")
        if self._issuer_pid > 0:
            fields = Path(f"/proc/{self._issuer_pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            if pid != self._issuer_pid or len(fields) < 20 or fields[19] != started or fields[0] in ("Z", "X", "x"):
                raise PeerUnavailable("issuer grant differs from its currently visible lifetime")
        self.check_current()
        self.grant = RecoveryGrant(
            self.configuration.request_id,
            self.configuration.authorization_id,
            self.configuration.target_id,
            self.challenge,
            self.receipt_nonce,
            cast(str, grant["inventory_sha256"]),
            cast(str, grant["binding_sha256"]),
            cast(str, grant["consumed_sha256"]),
            cast(str, grant["receipt_sha256"]),
            boot,
            cast(int, pid),
            started,
        )

    def check_current(self) -> None:
        """Refuse stale readiness without releasing any already retained custody."""
        try:
            self._check_issuer()
            assert self._target is not None
            if tuple(inspect_held_resource(fence) for fence in (*self._originals, self._target)) != self._initial:
                raise PeerUnavailable("consumer independent fence custody changed")
            if tuple(_transfer(item.expected, item.descriptor, self._issuer_pid, self.configuration.issuer_uid) for item in self._transfers) != self._transfers:
                raise PeerUnavailable("consumer transferred custody changed")
            if self._configuration_file is not None:
                current, identity = read_original(self._configuration_file)
                if current != self._configuration_bytes or identity != self._configuration_identity:
                    raise PeerUnavailable("consumer configuration changed while holding custody")
            self._check_issuer()
        except (OSError, ValueError, TypeError, KeyError) as error:
            if isinstance(error, PeerUnavailable):
                raise
            raise PeerUnavailable("current consumer custody could not be verified") from error


def _validate_socket_directory(configuration: ConsumerConfiguration) -> None:
    if ISSUER_DIRECTORY != ISSUER_DIRECTORY.resolve(strict=True):
        raise PeerUnavailable("recovery issuer directory is not canonical")
    directory = os.open(ISSUER_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(directory)
        if (info.st_dev, info.st_ino) != (configuration.socket_directory_device, configuration.socket_directory_inode) or info.st_uid != configuration.issuer_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise PeerUnavailable("recovery issuer directory differs from the target configuration")
        entry = ISSUER_SOCKET.lstat()
        if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != configuration.issuer_uid or stat.S_IMODE(entry.st_mode) != 0o600:
            raise PeerUnavailable("recovery issuer socket protection differs")
        current = ISSUER_DIRECTORY.lstat()
        if (current.st_dev, current.st_ino, current.st_uid, current.st_mode) != (info.st_dev, info.st_ino, info.st_uid, info.st_mode):
            raise PeerUnavailable("recovery issuer directory changed during inspection")
    finally:
        os.close(directory)


def connect_recovery_epoch(config_path: Path, target_owner_fd: int, *, timeout: float = 30) -> RecoveryEpochConsumer:
    """Connect only the fixed production origin; no request or environment root."""
    if config_path != CONSUMER_CONFIG:
        raise PeerUnavailable("recovery consumer configuration must use its fixed bootstrap path")
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
        raise PeerUnavailable("recovery consumer verification deadline must be positive and at most one hour")
    encoded, _ = read_original(config_path)
    configuration = parse_consumer_configuration(encoded)
    _validate_socket_directory(configuration)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(min(timeout, 60))
        connection.connect(str(ISSUER_SOCKET))
        _validate_socket_directory(configuration)
        return RecoveryEpochConsumer(connection, configuration, target_owner_fd, timeout=timeout, configuration_file=config_path)
    except BaseException:
        connection.close()
        raise
