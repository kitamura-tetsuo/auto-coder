"""Real local processes and kernel custody, not Docker/recovery grant evidence."""

from __future__ import annotations

import array
import fcntl
import json
import os
import select
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

import pytest

from auto_coder.management.recovery_epoch_peer import ExpectedPeer, HeldFence, HeldResource, PeerCustody, PeerSession, PeerUnavailable, inspect_held_resource

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")

# The consumer speaks the public wire schema with real independent opens and
# flocks. No product observer or kernel authority is replaced by this fixture.
_CONSUMER = r"""
import array, fcntl, json, os, secrets, select, socket, sys, time
mailbox_fd, paths_json, mode, inherited_json = sys.argv[1:]
paths, inherited = json.loads(paths_json), json.loads(inherited_json)
owned = {}
for name, operation in (('original', fcntl.LOCK_SH), ('target', fcntl.LOCK_EX)):
    if (name == 'original' and mode == 'inherited-original') or (name == 'target' and mode == 'inherited-target'):
        owned[name] = inherited[name]
    else:
        owned[name] = os.open(paths[name], os.O_RDONLY | os.O_NOFOLLOW)
        if not (name == 'original' and mode == 'unlocked-original'):
            fcntl.flock(owned[name], operation | fcntl.LOCK_NB)
mailbox = socket.socket(fileno=int(mailbox_fd))
connection, issuer_endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
mailbox.sendmsg([b'p'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [issuer_endpoint.fileno()]))])
issuer_endpoint.close()
mailbox.close()
connection.settimeout(10)
request = {
    'schema': 'management-recovery-peer-challenge-v1',
    'request_id': 'request-one', 'authorization_id': 'authorization-one',
    'target_id': 'target-one', 'challenge': secrets.token_hex(32),
}
if mode == 'wrong-target': request['target_id'] = 'target-two'
if mode == 'bad-challenge': request['challenge'] = 'old'
wire = json.dumps(request).encode() + b'\n'
if mode == 'oversized-challenge': wire = b' ' * 65537 + wire
if mode == 'duplicate-challenge': wire = wire.rstrip()[:-1] + b',"challenge":"' + b'b' * 64 + b'"}\n'
if mode == 'rights-challenge':
    connection.sendmsg([wire], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [owned['original']]))])
else:
    connection.sendall(wire)
print('challenge-sent', flush=True)
if mode in ('wrong-target', 'bad-challenge', 'oversized-challenge', 'duplicate-challenge', 'rights-challenge'):
    sys.stdin.readline()
    raise SystemExit(0)
if mode == 'queued-only':
    if not select.select([connection], [], [], 10)[0]: raise RuntimeError('offer was never queued')
    print('offer-queued', flush=True)
    sys.stdin.readline()
    raise SystemExit(0)
received = []
content = bytearray()
while b'\n' not in content:
    data, ancillary, flags, _ = connection.recvmsg(65537, socket.CMSG_SPACE(256), socket.MSG_CMSG_CLOEXEC)
    if not data or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC): raise RuntimeError('incomplete offer')
    content.extend(data)
    for level, kind, payload in ancillary:
        if (level, kind) != (socket.SOL_SOCKET, socket.SCM_RIGHTS): raise RuntimeError('unexpected control')
        values = array.array('i'); values.frombytes(payload); received.extend(values)
offer = json.loads(content)
descriptors = []
for resource in offer['resources']:
    index = resource['transfer_index']
    descriptor = received[index] if index is not None else owned[resource['name']]
    owned[resource['name']] = descriptor
    descriptors.append({'name': resource['name'], 'fd': descriptor})
print('offer-received', flush=True)
if sys.stdin.readline().strip() != 'receipt': raise SystemExit(0)
receipt = {key: offer[key] for key in ('request_id', 'authorization_id', 'target_id', 'challenge', 'receipt_nonce')}
receipt.update(schema='management-recovery-custody-receipt-v1', descriptors=descriptors)
if mode == 'omitted': descriptors.pop()
if mode == 'duplicate-fd': descriptors[-1]['fd'] = descriptors[0]['fd']
if mode == 'duplicate-name': descriptors[-1]['name'] = descriptors[0]['name']
if mode == 'unknown-name': descriptors[-1]['name'] = 'undeclared'
if mode == 'stale-nonce': receipt['receipt_nonce'] = '0' * 64
if mode == 'wrong-fd': descriptors[2]['fd'] = os.open(paths['other'], os.O_RDONLY)
if mode == 'reopened-controller': descriptors[2]['fd'] = os.open(paths['controller'], os.O_RDONLY)
if mode == 'closed-controller': os.close(owned['controller'])
if mode == 'boolean-fd': descriptors[0]['fd'] = True
wire = json.dumps(receipt).encode() + b'\n'
if mode == 'rights-receipt':
    connection.sendmsg([wire], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [owned['original']]))])
elif mode == 'drip-receipt':
    for byte in wire:
        try: connection.sendall(bytes([byte]))
        except BrokenPipeError: break
        time.sleep(0.04)
else:
    connection.sendall(wire)
print('receipt-sent', flush=True)
for command in sys.stdin:
    command = command.strip()
    if command == 'exit': break
    if command.startswith('close-'):
        os.close(owned[command.removeprefix('close-')])
    elif command.startswith('unlock-'):
        fcntl.flock(owned[command.removeprefix('unlock-')], fcntl.LOCK_UN)
    else: raise RuntimeError('unsupported fixture command')
    print(command, flush=True)
"""


@dataclass
class PeerCase:
    process: subprocess.Popen[str]
    connection: socket.socket
    expected: ExpectedPeer
    fences: tuple[HeldFence, ...]
    resources: tuple[HeldResource, ...]
    paths: dict[str, str]

    def line(self, expected: str) -> None:
        assert self.process.stdout is not None
        assert select.select([self.process.stdout], [], [], 10)[0], "peer did not reach the observable protocol barrier"
        actual = self.process.stdout.readline().strip()
        assert actual == expected, (actual, expected, self.process.poll())

    def command(self, value: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()


@contextmanager
def peer_case(tmp_path: Path, mode: str = "valid", *, namespace: bool = False) -> Iterator[PeerCase]:
    paths = {name: str(tmp_path / name) for name in ("original", "target", "controller", "other")}
    descriptors: dict[str, int] = {}
    process = None
    connection = None
    mailbox, child_mailbox = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    mailbox.settimeout(10)
    try:
        for name, path in paths.items():
            descriptors[name] = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        fcntl.flock(descriptors["original"], fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(descriptors["controller"], fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited: dict[str, int] = {}
        if mode == "inherited-original":
            inherited["original"] = descriptors["original"]
        elif mode == "inherited-target":
            fcntl.flock(descriptors["target"], fcntl.LOCK_EX | fcntl.LOCK_NB)
            inherited["target"] = descriptors["target"]
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", _CONSUMER, str(child_mailbox.fileno()), json.dumps(paths), mode, json.dumps(inherited)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(*inherited.values(), child_mailbox.fileno()),
        )
        child_mailbox.close()
        # The independent consumer creates the session pair, so SO_PEERCRED is
        # its real PID/UID. The inherited pair only transports the issuer end.
        # This needs no listening socket, proc substitution or credential mock.
        payload, ancillary, flags, _ = mailbox.recvmsg(1, socket.CMSG_SPACE(array.array("i").itemsize), socket.MSG_CMSG_CLOEXEC)
        assert payload == b"p" and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) and len(ancillary) == 1
        level, kind, data = ancillary[0]
        assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
        received = array.array("i")
        received.frombytes(data)
        assert len(received) == 1
        connection = socket.socket(fileno=received[0])
        start = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        expected = ExpectedPeer(process.pid, start, Path("/proc/sys/kernel/random/boot_id").read_text().strip(), os.geteuid(), "request-one", "authorization-one", "target-one")
        fences = tuple(HeldFence(name, descriptors[name], kind) for name, kind in (("original", "original-sh"), ("target", "target-ex"), ("controller", "transferred-ex")))
        resources = ()
        if namespace:
            descriptors["namespace"] = os.open("/proc/self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
            resources = (HeldResource("namespace", descriptors["namespace"]),)
        case = PeerCase(process, connection, expected, fences, resources, paths)
        case.line("challenge-sent")
        yield case
    finally:
        if connection is not None:
            connection.close()
        mailbox.close()
        child_mailbox.close()
        if process is not None:
            if process.poll() is None:
                try:
                    assert process.stdin is not None
                    process.stdin.write("exit\n")
                    process.stdin.flush()
                except BrokenPipeError:
                    pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        for descriptor in descriptors.values():
            os.close(descriptor)


def receive_custody(case: PeerCase, session: PeerSession) -> PeerCustody:
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(session.offer_and_verify_custody, case.fences, case.resources)
        case.line("offer-received")
        assert not future.done(), "received descriptors alone must not establish acknowledged custody"
        case.command("receipt")
        case.line("receipt-sent")
        return future.result(timeout=10)


@pytest.mark.parametrize("namespace", [False, True])
def test_real_peer_independently_owns_fences_and_receives_exclusive_custody(tmp_path: Path, namespace: bool) -> None:
    with peer_case(tmp_path, namespace=namespace) as case, PeerSession(case.connection, case.expected) as session:
        custody = receive_custody(case, session)
        assert custody.peer == case.expected
        assert len(custody.challenge) == len(custody.receipt_nonce) == 64
        assert len(custody.resources) == 3 + int(namespace)
        original, target, controller = custody.resources[:3]
        assert original.peer_lock is not None and original.peer_lock.pid == case.process.pid and original.peer_lock.mode == "READ"
        assert original.identity.lock is not None and original.identity.lock.pid == os.getpid()
        assert target.peer_lock is not None and target.peer_lock.pid == case.process.pid and target.peer_lock.mode == "WRITE"
        assert controller.peer_lock == controller.identity.lock
        assert controller.peer_lock is not None and controller.peer_lock.pid == os.getpid()
        if namespace:
            assert custody.resources[-1].identity.kind == "network-namespace" and custody.resources[-1].peer_lock is None
        session.check(custody)
        assert not hasattr(session, "send_grant")


@pytest.mark.parametrize("mode,barrier", [("queued-only", "offer-queued"), ("valid", "offer-received")])
def test_queued_or_received_descriptors_without_acknowledgment_never_complete(tmp_path: Path, mode: str, barrier: str) -> None:
    with peer_case(tmp_path, mode) as case, PeerSession(case.connection, case.expected, timeout=0.5) as session:
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(session.offer_and_verify_custody, case.fences)
            case.line(barrier)
            assert not future.done(), "the producer must wait for the actual challenge-bound receipt"
            with pytest.raises(PeerUnavailable, match="could not be verified"):
                future.result(timeout=5)
        assert case.connection.fileno() == -1
        for fence in case.fences:
            os.fstat(fence.descriptor)


@pytest.mark.parametrize(
    "mode",
    ["omitted", "duplicate-fd", "duplicate-name", "unknown-name", "stale-nonce", "wrong-fd", "reopened-controller", "closed-controller", "boolean-fd", "rights-receipt", "inherited-original", "inherited-target", "unlocked-original"],
)
def test_actual_bad_or_unheld_custody_refuses(tmp_path: Path, mode: str) -> None:
    with peer_case(tmp_path, mode) as case, PeerSession(case.connection, case.expected) as session:
        with pytest.raises(PeerUnavailable):
            receive_custody(case, session)
        assert case.connection.fileno() == -1


@pytest.mark.parametrize("mode", ["wrong-target", "bad-challenge", "oversized-challenge", "duplicate-challenge", "rights-challenge"])
def test_challenge_is_bounded_exact_and_cannot_supply_descriptors(tmp_path: Path, mode: str) -> None:
    with peer_case(tmp_path, mode) as case:
        before = len(tuple(Path("/proc/self/fd").iterdir()))
        with pytest.raises((PeerUnavailable, ValueError)):
            PeerSession(case.connection, case.expected)
        assert case.connection.fileno() == -1
        assert len(tuple(Path("/proc/self/fd").iterdir())) <= before - 1


@pytest.mark.parametrize("change", ["pid", "uid", "start_token", "boot_id"])
def test_socket_credentials_and_current_process_lifetime_are_independent(tmp_path: Path, change: str) -> None:
    with peer_case(tmp_path) as case:
        values = {"pid": os.getpid(), "uid": os.geteuid() + 1, "start_token": str(int(case.expected.start_token) + 1), "boot_id": "00000000-0000-4000-8000-000000000001"}
        with pytest.raises(PeerUnavailable):
            PeerSession(case.connection, replace(case.expected, **{change: values[change]}))
        assert case.connection.fileno() == -1


@pytest.mark.parametrize("command", ["close-original", "unlock-original", "unlock-target", "close-controller", "unlock-controller", "close-namespace"])
def test_custody_is_rechecked_after_receipt(tmp_path: Path, command: str) -> None:
    with peer_case(tmp_path, namespace=True) as case, PeerSession(case.connection, case.expected) as session:
        custody = receive_custody(case, session)
        case.command(command)
        case.line(command)
        with pytest.raises(PeerUnavailable):
            session.check(custody)
        assert case.connection.fileno() == -1


def test_exited_peer_never_remains_current(tmp_path: Path) -> None:
    with peer_case(tmp_path) as case, PeerSession(case.connection, case.expected) as session:
        custody = receive_custody(case, session)
        case.command("exit")
        case.process.wait(timeout=5)
        with pytest.raises(PeerUnavailable):
            session.check(custody)


@pytest.mark.parametrize("change", ["replacement", "missing", "protection", "host-unlock"])
def test_changed_issuer_fence_refuses_after_receipt(tmp_path: Path, change: str) -> None:
    with peer_case(tmp_path) as case, PeerSession(case.connection, case.expected) as session:
        custody = receive_custody(case, session)
        path = Path(case.paths["original"])
        if change in ("replacement", "missing"):
            path.rename(path.with_name("preserved-original"))
            if change == "replacement":
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
        elif change == "protection":
            path.chmod(0o644)
        else:
            fcntl.flock(case.fences[0].descriptor, fcntl.LOCK_UN)
        with pytest.raises(PeerUnavailable):
            session.check(custody)


def test_duplicate_held_resource_cannot_be_disguised_with_another_name(tmp_path: Path) -> None:
    with peer_case(tmp_path) as case, PeerSession(case.connection, case.expected) as session:
        duplicate = replace(case.fences[0], name="another-original")
        with pytest.raises(PeerUnavailable, match="duplicated"):
            session.offer_and_verify_custody((*case.fences, duplicate))


def test_custody_from_a_different_session_is_not_accepted(tmp_path: Path) -> None:
    with peer_case(tmp_path) as case, PeerSession(case.connection, case.expected) as session:
        custody = receive_custody(case, session)
        with pytest.raises(PeerUnavailable, match="another session"):
            session.check(replace(custody, receipt_nonce="0" * 64))


def test_session_can_offer_only_once(tmp_path: Path) -> None:
    with peer_case(tmp_path) as case, PeerSession(case.connection, case.expected) as session:
        receive_custody(case, session)
        with pytest.raises(PeerUnavailable, match="already offered"):
            session.offer_and_verify_custody(case.fences)


def test_regular_file_cannot_be_claimed_as_namespace_resource(tmp_path: Path) -> None:
    with peer_case(tmp_path) as case:
        with pytest.raises(PeerUnavailable, match="network namespace"):
            inspect_held_resource(HeldResource("pretend-namespace", case.fences[0].descriptor))


def test_partial_receipt_does_not_extend_the_total_exchange_deadline(tmp_path: Path) -> None:
    with peer_case(tmp_path, "drip-receipt") as case, PeerSession(case.connection, case.expected, timeout=0.2) as session:
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(session.offer_and_verify_custody, case.fences)
            case.line("offer-received")
            case.command("receipt")
            with pytest.raises(PeerUnavailable):
                future.result(timeout=2)


def test_peer_exit_before_receipt_never_establishes_custody(tmp_path: Path) -> None:
    with peer_case(tmp_path) as case, PeerSession(case.connection, case.expected) as session:
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(session.offer_and_verify_custody, case.fences)
            case.line("offer-received")
            case.command("exit")
            case.process.wait(timeout=5)
            with pytest.raises(PeerUnavailable):
                future.result(timeout=2)
