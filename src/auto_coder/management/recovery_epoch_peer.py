"""Authenticated recovery peer custody evidence, without grant authority.

The issuer supplies the expected Docker-bound process and the complete fence
set. This transport neither decides that scope nor consumes or issues an epoch.
Caller-owned fences stay open; the session owns only its socket and PID pin.
"""

from __future__ import annotations

import array
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
from typing import Literal

from .archived_exclusions import unique_pairs
from .fresh_host_observation import _assert_pid_flock

MAX_MESSAGE_BYTES = 64 * 1024
MAX_RESOURCES = 64
CHALLENGE_SCHEMA = "management-recovery-peer-challenge-v1"
OFFER_SCHEMA = "management-recovery-custody-offer-v1"
RECEIPT_SCHEMA = "management-recovery-custody-receipt-v1"
FenceKind = Literal["original-sh", "target-ex", "transferred-ex"]


class PeerUnavailable(ValueError):
    """Peer custody is unavailable; this result never permits a grant."""


@dataclass(frozen=True)
class ExpectedPeer:
    pid: int
    start_token: str
    boot_id: str
    uid: int
    request_id: str
    authorization_id: str
    target_id: str


@dataclass(frozen=True)
class HeldFence:
    name: str
    descriptor: int
    kind: FenceKind


@dataclass(frozen=True)
class HeldResource:
    name: str
    descriptor: int
    kind: Literal["network-namespace"] = "network-namespace"


@dataclass(frozen=True)
class KernelLock:
    mode: str
    pid: int
    device_major: int
    device_minor: int
    inode: int


@dataclass(frozen=True)
class ResourceIdentity:
    name: str
    kind: str
    path: str
    device: int
    inode: int
    mode: int
    uid: int
    lock: KernelLock | None


@dataclass(frozen=True)
class ReceivedResource:
    identity: ResourceIdentity
    peer_descriptor: int
    peer_lock: KernelLock | None


@dataclass(frozen=True)
class PeerCustody:
    peer: ExpectedPeer
    challenge: str
    receipt_nonce: str
    resources: tuple[ReceivedResource, ...]
    observed_monotonic_ns: int


def _token(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _name(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is not None


def _fd_path(pid: int, descriptor: int, *, info: bool = False) -> Path:
    if type(pid) is not int or pid <= 0 or type(descriptor) is not int or not 0 <= descriptor < 1_048_576:
        raise PeerUnavailable("peer descriptor or process identity is invalid")
    return Path("/proc") / str(pid) / ("fdinfo" if info else "fd") / str(descriptor)


def inspect_kernel_lock(pid: int, descriptor: int, device: int, inode: int, *, allow_unmapped_owner: bool = False) -> KernelLock | None:
    """Read the exact FD's normalized kernel flock record without changing it.

    The optional PID-zero allowance is only for an authenticated host outside
    a consumer's PID namespace; independent holder checks must use the default.
    """
    path = _fd_path(pid, descriptor, info=True)
    with path.open("rb") as stream:
        data = stream.read(MAX_MESSAGE_BYTES + 1)
    if len(data) > MAX_MESSAGE_BYTES:
        raise PeerUnavailable("kernel descriptor observation exceeds its bound")
    matches: list[KernelLock] = []
    for line in data.decode("ascii").splitlines():
        if not line.startswith("lock:"):
            continue
        fields = line.removeprefix("lock:").split()
        if len(fields) != 8 or fields[1:3] != ["FLOCK", "ADVISORY"] or fields[3] not in ("READ", "WRITE") or fields[6:] != ["0", "EOF"]:
            raise PeerUnavailable("descriptor kernel lock profile differs")
        parts = fields[5].split(":")
        if len(parts) != 3 or not fields[4].isdecimal() or int(fields[4]) < (0 if allow_unmapped_owner else 1):
            raise PeerUnavailable("descriptor kernel lock identity is malformed")
        observed = KernelLock(fields[3], int(fields[4]), int(parts[0], 16), int(parts[1], 16), int(parts[2]))
        if (observed.device_major, observed.device_minor, observed.inode) != (os.major(device), os.minor(device), inode):
            raise PeerUnavailable("descriptor kernel lock names another file")
        matches.append(observed)
    if len(matches) > 1:
        raise PeerUnavailable("descriptor kernel lock is ambiguous")
    return matches[0] if matches else None


def inspect_held_resource(resource: HeldFence | HeldResource) -> ResourceIdentity:
    """Inspect a caller-owned descriptor without acquiring or releasing it."""
    if not _name(resource.name):
        raise PeerUnavailable("resource name is invalid")
    descriptor_path = _fd_path(os.getpid(), resource.descriptor)
    info = os.fstat(resource.descriptor)
    path = os.readlink(descriptor_path)
    if isinstance(resource, HeldResource):
        if resource.kind != "network-namespace" or re.fullmatch(r"net:\[[0-9]+\]", path) is None or path != f"net:[{info.st_ino}]":
            raise PeerUnavailable("held resource is not the exact network namespace")
        return ResourceIdentity(resource.name, resource.kind, path, info.st_dev, info.st_ino, info.st_mode, info.st_uid, None)
    if resource.kind not in ("original-sh", "target-ex", "transferred-ex"):
        raise PeerUnavailable("held fence kind is unsupported")
    original = Path(path)
    if not original.is_absolute() or original != original.resolve(strict=True):
        raise PeerUnavailable("held fence path is not canonical")
    current = original.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_uid != os.geteuid() or (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_nlink) != (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink):
        raise PeerUnavailable("held fence identity or protection differs")
    lock = inspect_kernel_lock(os.getpid(), resource.descriptor, info.st_dev, info.st_ino)
    if resource.kind != "target-ex":
        mode = "READ" if resource.kind == "original-sh" else "WRITE"
        if lock is None or lock.mode != mode or lock.pid != os.getpid():
            raise PeerUnavailable("issuer does not independently hold the required fence")
        _assert_pid_flock(os.getpid(), resource.descriptor, mode)
    return ResourceIdentity(resource.name, resource.kind, path, info.st_dev, info.st_ino, info.st_mode, info.st_uid, lock)


def _received(identity: ResourceIdentity, peer: ExpectedPeer, descriptor: int) -> ReceivedResource:
    path = _fd_path(peer.pid, descriptor)
    before = path.stat()
    # /proc/PID/fd intentionally follows the authenticated process's descriptor.
    # NONBLOCK prevents a concurrently replaced FIFO from blocking inspection.
    reopened = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        actual = os.fstat(reopened)
        after = path.stat()
        expected = (identity.device, identity.inode, identity.mode, identity.uid)
        for info in (before, actual, after):
            if (info.st_dev, info.st_ino, info.st_mode, info.st_uid) != expected:
                raise PeerUnavailable("received descriptor names another resource")
        if identity.kind == "network-namespace":
            if os.readlink(path) != identity.path:
                raise PeerUnavailable("received network namespace object differs")
            observed = None
        else:
            observed = inspect_kernel_lock(peer.pid, descriptor, actual.st_dev, actual.st_ino)
            if identity.kind == "transferred-ex":
                if observed is None or observed != identity.lock or observed.mode != "WRITE":
                    raise PeerUnavailable("consumer did not receive the held exclusive open file description")
            else:
                mode = "READ" if identity.kind == "original-sh" else "WRITE"
                if observed is None or observed.pid != peer.pid or observed.mode != mode:
                    raise PeerUnavailable("consumer did not independently acquire the required fence")
                _assert_pid_flock(peer.pid, reopened, mode)
        final = path.stat()
        if (final.st_dev, final.st_ino, final.st_mode, final.st_uid) != expected:
            raise PeerUnavailable("consumer descriptor changed during custody observation")
        return ReceivedResource(identity, descriptor, observed)
    finally:
        os.close(reopened)


def _reject_constant(value: str) -> object:
    raise PeerUnavailable("non-finite peer JSON value: " + value)


def encode_peer_message(value: dict[str, object]) -> bytes:
    """Encode one bounded protocol object; this does not authorize its content."""
    packet = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(packet) > MAX_MESSAGE_BYTES:
        raise PeerUnavailable("peer message exceeds its bound")
    return packet


def receive_peer_message(connection: socket.socket, *, timeout: float, allow_rights: bool = False, deadline: float | None = None) -> tuple[dict[str, object], tuple[int, ...]]:
    """Read one message, closing all kernel-delivered FDs on any refusal."""
    deadline = time.monotonic() + timeout if deadline is None else deadline
    content = bytearray()
    received: list[int] = []
    try:
        while b"\n" not in content:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PeerUnavailable("peer exchange exceeded its bounded deadline")
            connection.settimeout(remaining)
            data, ancillary, flags, _ = connection.recvmsg(MAX_MESSAGE_BYTES + 1, socket.CMSG_SPACE(MAX_RESOURCES * array.array("i").itemsize), socket.MSG_CMSG_CLOEXEC)
            for level, kind, payload in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(payload[: len(payload) - len(payload) % values.itemsize])
                    received.extend(values)
            if (ancillary and not allow_rights) or any((level, kind) != (socket.SOL_SOCKET, socket.SCM_RIGHTS) for level, kind, _ in ancillary) or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise PeerUnavailable("peer unexpectedly supplied descriptors or a truncated message")
            if len(received) > MAX_RESOURCES:
                raise PeerUnavailable("peer supplied too many descriptors")
            if not data:
                raise PeerUnavailable("peer closed before complete custody evidence")
            content.extend(data)
            if len(content) > MAX_MESSAGE_BYTES:
                raise PeerUnavailable("peer message exceeds its bound")
        line, remainder = bytes(content).split(b"\n", 1)
        if remainder:
            raise PeerUnavailable("peer supplied an unsolicited additional message")
        value = json.loads(line, object_pairs_hook=unique_pairs, parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise PeerUnavailable("peer message is not an object")
        return value, tuple(received)
    except BaseException:
        for descriptor in received:
            os.close(descriptor)
        raise


class PeerSession:
    """One challenge and independently observed custody, never a launch grant."""

    def __init__(self, connection: socket.socket, expected: ExpectedPeer, *, timeout: float = 10) -> None:
        self.connection = connection
        self.expected = expected
        self._pidfd = -1
        self._closed = False
        self._offered = False
        self._held: tuple[HeldFence | HeldResource, ...] = ()
        self._identities: tuple[ResourceIdentity, ...] = ()
        self._custody: PeerCustody | None = None
        self._timeout = timeout
        self.challenge = ""
        self.receipt_nonce = secrets.token_hex(32)
        try:
            if connection.family != socket.AF_UNIX or connection.type not in (socket.SOCK_STREAM, socket.SOCK_SEQPACKET):
                raise PeerUnavailable("recovery custody requires an authenticated local socket")
            if type(timeout) not in (int, float) or not 0 < timeout <= 60:
                raise PeerUnavailable("peer timeout must be positive and at most sixty seconds")
            if type(expected.pid) is not int or expected.pid <= 0 or type(expected.uid) is not int or expected.uid < 0:
                raise PeerUnavailable("expected peer identity is invalid")
            if not expected.start_token.isascii() or not expected.start_token.isdecimal() or str(int(expected.start_token)) != expected.start_token or int(expected.start_token) <= 0:
                raise PeerUnavailable("expected peer lifetime is invalid")
            if str(uuid.UUID(expected.boot_id)) != expected.boot_id or not all(_name(value) for value in (expected.request_id, expected.authorization_id, expected.target_id)):
                raise PeerUnavailable("expected peer binding is invalid")
            connection.settimeout(timeout)
            self._pidfd = os.pidfd_open(expected.pid, 0)
            self._check_lifetime()
            message = self._read_message()
            if set(message) != {"schema", "request_id", "authorization_id", "target_id", "challenge"} or message.get("schema") != CHALLENGE_SCHEMA:
                raise PeerUnavailable("peer challenge schema differs")
            if any(message[name] != getattr(expected, name) for name in ("request_id", "authorization_id", "target_id")) or not _token(message["challenge"]):
                raise PeerUnavailable("peer challenge binding differs")
            self.challenge = str(message["challenge"])
            self._check_lifetime()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> PeerSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.connection.close()
            if self._pidfd >= 0:
                os.close(self._pidfd)
                self._pidfd = -1

    def _check_lifetime(self) -> None:
        if self._closed or self._pidfd < 0:
            raise PeerUnavailable("peer session is closed")
        pid, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if (pid, uid) != (self.expected.pid, self.expected.uid):
            raise PeerUnavailable("local peer credentials differ from the expected target")
        poll = select.poll()
        poll.register(self._pidfd, select.POLLIN)
        prefix, separator, tail = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii").rpartition(")")
        fields = tail.split()
        if not separator or prefix.partition(" (")[0] != str(pid) or len(fields) < 20 or fields[0] in ("Z", "X", "x") or fields[19] != self.expected.start_token or Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() != self.expected.boot_id or poll.poll(0):
            raise PeerUnavailable("exact target peer lifetime exited or changed")

    def _set_remaining_timeout(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PeerUnavailable("peer exchange exceeded its bounded deadline")
        self.connection.settimeout(remaining)

    def _read_message(self, *, deadline: float | None = None) -> dict[str, object]:
        value, _ = receive_peer_message(self.connection, timeout=self._timeout, deadline=deadline)
        return value

    def offer_and_verify_custody(self, fences: tuple[HeldFence, ...], resources: tuple[HeldResource, ...] = ()) -> PeerCustody:
        """Transfer declared host resources, then require physical receipt proof."""
        try:
            if self._offered:
                raise PeerUnavailable("this peer session already offered custody")
            self._offered = True
            self._check_lifetime()
            held: tuple[HeldFence | HeldResource, ...] = (*fences, *resources)
            if not 3 <= len(held) <= MAX_RESOURCES or not any(fence.kind == "original-sh" for fence in fences) or sum(fence.kind == "target-ex" for fence in fences) != 1 or not any(fence.kind == "transferred-ex" for fence in fences):
                raise PeerUnavailable("complete original, target and deployment fence classes are required")
            identities = tuple(inspect_held_resource(resource) for resource in held)
            if len({item.name for item in identities}) != len(identities) or len({(item.device, item.inode) for item in identities}) != len(identities):
                raise PeerUnavailable("resource names or kernel objects are duplicated")
            transfer = array.array("i")
            offered: list[dict[str, object]] = []
            for resource, identity in zip(held, identities):
                transfer_index = None
                if identity.kind in ("transferred-ex", "network-namespace"):
                    transfer_index = len(transfer)
                    transfer.append(resource.descriptor)
                offered.append({"name": identity.name, "kind": identity.kind, "device": identity.device, "inode": identity.inode, "transfer_index": transfer_index})
            binding = {name: getattr(self.expected, name) for name in ("request_id", "authorization_id", "target_id")}
            binding.update(challenge=self.challenge, receipt_nonce=self.receipt_nonce)
            packet = encode_peer_message({"schema": OFFER_SCHEMA, **binding, "resources": offered})
            deadline = time.monotonic() + self._timeout
            self._set_remaining_timeout(deadline)
            sent = self.connection.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, transfer)])
            if sent <= 0:
                raise PeerUnavailable("custody offer was not sent")
            if sent < len(packet):
                self._set_remaining_timeout(deadline)
                self.connection.sendall(packet[sent:])
            receipt = self._read_message(deadline=deadline)
            if set(receipt) != {"schema", *binding, "descriptors"} or receipt.get("schema") != RECEIPT_SCHEMA or any(receipt[name] != value for name, value in binding.items()):
                raise PeerUnavailable("custody receipt is stale or bound to another challenge")
            descriptors = receipt["descriptors"]
            if not isinstance(descriptors, list) or len(descriptors) != len(held):
                raise PeerUnavailable("custody receipt omits required descriptors")
            peer_descriptors: dict[str, int] = {}
            for item in descriptors:
                if not isinstance(item, dict) or set(item) != {"name", "fd"} or not _name(item.get("name")) or type(item.get("fd")) is not int:
                    raise PeerUnavailable("custody receipt descriptor is malformed")
                name, descriptor = str(item["name"]), int(item["fd"])
                _fd_path(self.expected.pid, descriptor)
                if name in peer_descriptors or descriptor in peer_descriptors.values():
                    raise PeerUnavailable("custody receipt duplicates a descriptor")
                peer_descriptors[name] = descriptor
            if set(peer_descriptors) != {item.name for item in identities}:
                raise PeerUnavailable("custody receipt names another resource set")
            self._held, self._identities = held, identities
            self._check_lifetime()
            observed = tuple(_received(identity, self.expected, peer_descriptors[identity.name]) for identity in identities)
            result = PeerCustody(self.expected, self.challenge, self.receipt_nonce, observed, time.monotonic_ns())
            self._custody = result
            self.check(result)
            return result
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, ValueError, TypeError, KeyError)) and not isinstance(error, PeerUnavailable):
                raise PeerUnavailable("current peer custody could not be verified") from error
            raise

    def check(self, custody: PeerCustody) -> None:
        """Repeat actual custody checks; the caller still owns grant ordering."""
        try:
            if self._custody is None or custody != self._custody:
                raise PeerUnavailable("custody belongs to another session or was not verified")
            self._check_lifetime()
            if tuple(inspect_held_resource(resource) for resource in self._held) != self._identities:
                raise PeerUnavailable("issuer resource identity or lock changed after receipt")
            observed = tuple(_received(item.identity, self.expected, item.peer_descriptor) for item in self._custody.resources)
            if observed != self._custody.resources:
                raise PeerUnavailable("consumer resource custody changed after receipt")
            self._check_lifetime()
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, ValueError, TypeError, KeyError)) and not isinstance(error, PeerUnavailable):
                raise PeerUnavailable("current peer custody could not be verified") from error
            raise
