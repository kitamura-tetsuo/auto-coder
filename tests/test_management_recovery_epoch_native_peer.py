"""Real SCM_RIGHTS ABI components, separate from native VM peer acceptance.

The message reader inspects actual kernel-populated msghdr/iovec/control memory
from a local socketpair. No production issuer, containment or grant is asserted.
The disposable VM independently exercises the actual ptrace/process barriers.
"""

from __future__ import annotations

import array
import ctypes
import os
import socket
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from scripts.recovery_epoch_native_peer import (
    LATE_MUTATION_SCENARIOS,
    REQUIRED_PEER_CASES,
    KernelPeerMessageBarrier,
    _container_ids,
    _ControlHeader,
    _decode_rights,
    _descriptor_snapshot,
    _failed_terminal,
    _Iovec,
    _local_socket_peer,
    _MessageHeader,
    _peek_queued_packet,
    _stopped_receiver,
    _verify_target_loss,
    mutate_after_intent,
)

from auto_coder.management.original_attempt_evidence import OriginalFile, OriginalGeneration, OriginalLineageEvidence
from auto_coder.management.original_attempts import HistoryBinding, canonical
from auto_coder.management.recovery_epoch_peer import MAX_MESSAGE_BYTES, MAX_RESOURCES, OFFER_SCHEMA, encode_peer_message

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@dataclass
class _OwnMemory:
    pid: int
    returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        raise AssertionError("this parser component cannot own a tracee")


@contextmanager
def _kernel_message(tmp_path: Path, count: int):
    """Populate the actual Linux ABI by invoking libc recvmsg, without ptrace."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.recvmsg.argtypes = [ctypes.c_int, ctypes.POINTER(_MessageHeader), ctypes.c_int]
    libc.recvmsg.restype = ctypes.c_ssize_t
    packet = encode_peer_message({"schema": OFFER_SCHEMA, "request_id": "request", "authorization_id": "authorization", "target_id": "target", "resources": []})
    data = ctypes.create_string_buffer(MAX_MESSAGE_BYTES + 1)
    control = ctypes.create_string_buffer(socket.CMSG_SPACE(MAX_RESOURCES * array.array("i").itemsize))
    vector = _Iovec(ctypes.addressof(data), len(data))
    header = _MessageHeader(None, 0, ctypes.addressof(vector), 1, ctypes.addressof(control), len(control), 0)
    receiver_fds: tuple[int, ...] = ()
    with ExitStack() as held:
        sender, receiver = socket.socketpair()
        held.enter_context(sender)
        held.enter_context(receiver)
        sources = tuple(held.enter_context((tmp_path / str(index)).open("w+b")).fileno() for index in range(count))
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", sources))] if count else []
        assert sender.sendmsg([packet], ancillary) == len(packet)
        result = libc.recvmsg(receiver.fileno(), ctypes.byref(header), socket.MSG_CMSG_CLOEXEC)
        assert result == len(packet), os.strerror(ctypes.get_errno())
        if count:
            # Capture only the actual expected received integers for cleanup,
            # independently of the parser under test's handling of cmsghdr.
            values = array.array("i")
            values.frombytes(control.raw[ctypes.sizeof(_ControlHeader) : ctypes.sizeof(_ControlHeader) + count * values.itemsize])
            receiver_fds = tuple(values)
        reader = object.__new__(KernelPeerMessageBarrier)
        reader.issuer = _OwnMemory(os.getpid())
        reader.binding = {"request_id": "request", "authorization_id": "authorization", "target_id": "target"}
        reader.schema = OFFER_SCHEMA
        try:
            yield reader, header, result, packet, sources, receiver_fds
        finally:
            for descriptor in receiver_fds:
                os.close(descriptor)


@pytest.mark.parametrize("count", [0, 1, 2, MAX_RESOURCES])
def test_real_recvmsg_memory_retains_exact_bytes_and_received_kernel_objects(tmp_path: Path, count: int) -> None:
    with _kernel_message(tmp_path, count) as (reader, header, size, packet, originals, received):
        encoded, descriptors = reader._message(ctypes.addressof(header), received_length=size)
        assert encoded == packet and descriptors == received
        assert reader._selected(encoded)
        for original, actual in zip(originals, received):
            assert original != actual
            assert (os.fstat(original).st_dev, os.fstat(original).st_ino) == (os.fstat(actual).st_dev, os.fstat(actual).st_ino)
            assert os.get_inheritable(actual) is False


@pytest.mark.parametrize("field,value", [("vector_count", 0), ("vector_count", 5), ("control_length", 4096), ("flags", socket.MSG_CTRUNC), ("flags", socket.MSG_TRUNC)])
def test_actual_message_reader_refuses_incomplete_or_unbounded_abi_observations(tmp_path: Path, field: str, value: int) -> None:
    with _kernel_message(tmp_path, 1) as (reader, header, size, _packet, _originals, _received):
        setattr(header, field, value)
        with pytest.raises(AssertionError, match="vectors or ancillary"):
            reader._message(ctypes.addressof(header), received_length=size)


def test_actual_message_reader_refuses_a_return_larger_than_receiver_capacity(tmp_path: Path) -> None:
    with _kernel_message(tmp_path, 1) as (reader, header, _size, _packet, _originals, _received):
        with pytest.raises(AssertionError, match="iovec capacity"):
            reader._message(ctypes.addressof(header), received_length=MAX_MESSAGE_BYTES + 2)


@pytest.mark.parametrize("scenario", ["", "late", "late-arbitrary-file", "late-../epoch", "after-intent-head"])
def test_late_mutation_selector_refuses_unreviewed_actions_before_access(scenario: str) -> None:
    with pytest.raises(ValueError, match="reviewed late mutation"):
        with mutate_after_intent(None, scenario, os.getpid()):
            pytest.fail("an unreviewed mutation was admitted")


def test_native_case_sets_are_explicit_and_distinct() -> None:
    assert len(REQUIRED_PEER_CASES) == 4
    assert len(LATE_MUTATION_SCENARIOS) == 11 and len(set(LATE_MUTATION_SCENARIOS)) == 11
    assert not REQUIRED_PEER_CASES.intersection(LATE_MUTATION_SCENARIOS)


@pytest.mark.parametrize("malformation", ["header", "length", "type", "integer", "duplicate", "negative", "too-many"])
def test_native_control_parser_refuses_nonexact_descriptor_records(malformation: str) -> None:
    descriptor_data = array.array("i", [3]).tobytes()
    header = _ControlHeader(ctypes.sizeof(_ControlHeader) + len(descriptor_data), socket.SOL_SOCKET, socket.SCM_RIGHTS)
    if malformation == "header":
        encoded = b"\x00"
    else:
        if malformation == "length":
            header.length += 32
        elif malformation == "type":
            header.kind = socket.SCM_CREDENTIALS
        elif malformation == "integer":
            descriptor_data += b"\x00"
            header.length += 1
        elif malformation in {"duplicate", "negative", "too-many"}:
            descriptor_data = array.array("i", [3, 3] if malformation == "duplicate" else [-1] if malformation == "negative" else range(MAX_RESOURCES + 1)).tobytes()
            header.length = ctypes.sizeof(_ControlHeader) + len(descriptor_data)
        encoded = bytes(header) + descriptor_data
    with pytest.raises(AssertionError, match="native"):
        _decode_rights(encoded)


def test_real_shared_ofd_reacquisition_changes_kernel_acquirer_visible_to_original_process(tmp_path: Path) -> None:
    """Check the kernel premise of the VM fault, without substituting pidfd_getfd."""
    import fcntl
    import select
    import subprocess
    import sys

    from auto_coder.management.recovery_epoch_peer import inspect_kernel_lock

    script = "import fcntl,sys; fd=int(sys.argv[1]); fcntl.flock(fd, fcntl.LOCK_SH); print('original-acquired',flush=True); sys.stdin.read(1)"
    with (tmp_path / "original-owner.lock").open("w+b") as original:
        os.fchmod(original.fileno(), 0o600)
        info = os.fstat(original.fileno())
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", script, str(original.fileno())], pass_fds=(original.fileno(),), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert select.select((process.stdout,), (), (), 10)[0]
            assert process.stdout.readline() == "original-acquired\n"
            before = inspect_kernel_lock(process.pid, original.fileno(), info.st_dev, info.st_ino)
            assert before is not None and (before.mode, before.pid) == ("READ", process.pid)
            fcntl.flock(original.fileno(), fcntl.LOCK_UN)
            fcntl.flock(original.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            after = inspect_kernel_lock(process.pid, original.fileno(), info.st_dev, info.st_ino)
            assert after is not None and (after.mode, after.pid) == ("READ", os.getpid())
            assert after != before and after == inspect_kernel_lock(os.getpid(), original.fileno(), info.st_dev, info.st_ino)
            process.communicate("x", timeout=10)
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


@pytest.mark.parametrize("encoded", ["a" * 12, "A" * 64, "a" * 64 + "\n" + "a" * 64, "name " + "a" * 64])
def test_target_loss_inventory_rejects_truncated_alias_or_duplicate_identities(encoded: str) -> None:
    with pytest.raises(AssertionError, match="incomplete or conflicting"):
        _container_ids(encoded)


def test_target_loss_inventory_preserves_complete_ids_and_accepts_measured_empty_output() -> None:
    assert _container_ids("b" * 64 + "\n" + "a" * 64 + "\n") == ("a" * 64, "b" * 64)
    assert _container_ids("") == ()


def test_target_loss_oracle_requires_real_pidfd_death_and_exact_object_absence() -> None:
    """Exercise kernel lifetime proof; synthetic IDs are not Docker acceptance."""
    import select
    import signal
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import signal; print('alive', flush=True); signal.pause()"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    descriptor = os.pidfd_open(process.pid, 0)
    try:
        assert select.select((process.stdout,), (), (), 10)[0]
        assert process.stdout.readline() == "alive\n"
        identifier, other = "a" * 64, "b" * 64
        with pytest.raises(AssertionError, match="pidfd has not independently"):
            _verify_target_loss(descriptor, identifier, (identifier, other), (other,))
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        poll = select.poll()
        poll.register(descriptor, select.POLLIN)
        assert poll.poll(10000), "the actual subprocess death was not observed"
        _verify_target_loss(descriptor, identifier, (identifier, other), (other,))
        for after in ((identifier, other), (), ("c" * 64,)):
            with pytest.raises(AssertionError, match="exact target was not removed"):
                _verify_target_loss(descriptor, identifier, (identifier, other), after)
        assert process.wait(timeout=10) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        process.communicate(timeout=10)
        os.close(descriptor)


def _component_failed_lineage(root: Path) -> tuple[OriginalLineageEvidence, Path]:
    """Use explicit synthetic records only for the selector's component oracle."""
    import hashlib

    request = "67bb23bb-35e4-45dd-b9dc-40ba21f912fb"
    terminal = root / "00000007.json"
    values = (
        (root / "00000000.json", canonical({"kind": "anchor", "attempt": "other-original-attempt"})),
        (terminal, canonical({"kind": "never-created", "attempt": request, "value": {}})),
        (root / "history.lock", b""),
    )
    originals = []
    for path, encoded in values:
        path.write_bytes(encoded)
        path.chmod(0o600)
        info = path.stat()
        originals.append(OriginalFile(str(path), info.st_dev, info.st_ino, hashlib.sha256(encoded).hexdigest()))
    branch = OriginalGeneration(
        request_id=request,
        instance_id="D",
        management_id="d" * 64,
        image_id="sha256:" + "e" * 64,
        bootstrap_config_sha256="b" * 64,
        management_config_sha256="c" * 64,
        state_root=str(root / "state"),
        state_device=1,
        state_inode=2,
        journal_root=str(root / "journal"),
        journal_device=1,
        journal_inode=3,
        oauth_root=str(root / "oauth"),
        oauth_device=1,
        oauth_inode=4,
        owner_path="",
        owner_device=0,
        owner_inode=0,
        outcome="never-created",
        expected_stores=(),
    )
    return OriginalLineageEvidence(HistoryBinding(str(root), "component-deployment", "component-history", "sha256:" + "a" * 64), frontier="f" * 64, generations=(branch,), originals=tuple(originals)), terminal


def test_failed_branch_selector_uses_matched_terminal_original_not_first_history_path(tmp_path: Path) -> None:
    lineage, terminal = _component_failed_lineage(tmp_path)
    selected, observation = _failed_terminal(lineage)
    assert selected == terminal and selected.name != "00000000.json"
    assert observation["instance_id"] == "D" and observation["outcome"] == "never-created"
    assert observation["request_id"] == lineage.generations[0].request_id
    assert observation["frontier"] == lineage.frontier
    assert observation["original_file"]["inode"] == terminal.stat().st_ino
    assert "independent_lineage_verified" not in observation, "a pure selector must never claim that the producer ran"


@pytest.mark.parametrize("fault", ["absent", "changed-bytes", "wrong-request", "duplicate-record", "duplicate-branch", "unknown", "incomplete"])
def test_failed_branch_selector_refuses_missing_changed_or_ambiguous_originals(tmp_path: Path, fault: str) -> None:
    import hashlib
    import json

    lineage, terminal = _component_failed_lineage(tmp_path)
    selected = next(original for original in lineage.originals if original.path == str(terminal))
    if fault == "absent":
        lineage = replace(lineage, originals=tuple(original for original in lineage.originals if original != selected))
    elif fault == "changed-bytes":
        terminal.write_bytes(terminal.read_bytes() + b"\x00")
    elif fault == "wrong-request":
        value = json.loads(terminal.read_bytes())
        value["attempt"] = "different-failed-request"
        encoded = canonical(value)
        terminal.write_bytes(encoded)
        lineage = replace(lineage, originals=tuple(replace(original, sha256=hashlib.sha256(encoded).hexdigest()) if original == selected else original for original in lineage.originals))
    elif fault == "duplicate-record":
        duplicate = terminal.with_name("00000008.json")
        duplicate.write_bytes(terminal.read_bytes())
        duplicate.chmod(0o600)
        info = duplicate.stat()
        lineage = replace(lineage, originals=(*lineage.originals, replace(selected, path=str(duplicate), device=info.st_dev, inode=info.st_ino)))
    elif fault == "duplicate-branch":
        lineage = replace(lineage, generations=(*lineage.generations, replace(lineage.generations[0], instance_id="another-failure")))
    elif fault == "unknown":
        lineage = replace(lineage, generations=(replace(lineage.generations[0], outcome="unknown"),))
    else:
        lineage = replace(lineage, unavailable=("an expected original is missing",))
    with pytest.raises(AssertionError, match="failed.branch|terminal record"):
        _failed_terminal(lineage)


def test_live_stopped_receiver_oracle_cannot_count_running_or_dead_peer_as_unacknowledged_custody() -> None:
    """Observe an actual stopped process; this is not native Docker acceptance."""
    import select
    import signal
    import subprocess
    import sys
    import time

    from scripts.recovery_epoch_fault_tracer import _token

    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import signal; print('alive', flush=True); signal.pause()"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    descriptor = os.pidfd_open(process.pid, 0)
    try:
        assert select.select((process.stdout,), (), (), 10)[0]
        assert process.stdout.readline() == "alive\n"
        token = _token(process.pid)
        snapshot = _descriptor_snapshot(process.pid)
        with pytest.raises(AssertionError, match="not still stopped"):
            _stopped_receiver(process.pid, token, descriptor, snapshot)
        signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
        deadline = time.monotonic() + 10
        while Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in {"T", "t"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        observation = _stopped_receiver(process.pid, token, descriptor, snapshot)
        assert observation["pidfd_alive"] is True and observation["pid"] == process.pid and observation["start_token"] == token
        with pytest.raises(AssertionError, match="not still stopped"):
            _stopped_receiver(process.pid, token, descriptor, snapshot[:-1])
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        poll = select.poll()
        poll.register(descriptor, select.POLLIN)
        assert poll.poll(10000)
        with pytest.raises(AssertionError, match="lifetime ended"):
            _stopped_receiver(process.pid, token, descriptor, snapshot)
        assert process.wait(timeout=10) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        process.communicate(timeout=10)
        os.close(descriptor)


@pytest.mark.parametrize("blocking", [True, False])
def test_actual_queued_grant_peek_preserves_bytes_and_shared_socket_flags(blocking: bool) -> None:
    """Use a real duplicate OFD; native VM separately requires pidfd_getfd."""
    import fcntl

    expected = canonical({"schema": "management-recovery-epoch-grant-v1", "component": "queued-frame"}) + b"\n"
    with ExitStack() as held:
        sender, receiver = socket.socketpair()
        held.enter_context(sender)
        held.enter_context(receiver)
        receiver.setblocking(blocking)
        duplicate = os.dup(receiver.fileno())
        held.callback(os.close, duplicate)
        before = fcntl.fcntl(receiver.fileno(), fcntl.F_GETFL)
        sender.sendall(expected)
        encoded, observed_before, observed_after = _peek_queued_packet(duplicate, expected)
        assert encoded == expected and observed_before == observed_after == before
        assert fcntl.fcntl(receiver.fileno(), fcntl.F_GETFL) == before
        assert _local_socket_peer(duplicate) == {"pid": os.getpid(), "uid": os.geteuid(), "gid": os.getegid()}
        # This is the first consuming read. Both native peeks left the whole
        # original frame available to the intended receiver's own descriptor.
        assert receiver.recv(len(expected) + 1, socket.MSG_DONTWAIT) == expected
        with pytest.raises(BlockingIOError):
            receiver.recv(1, socket.MSG_DONTWAIT)


@pytest.mark.parametrize("fault", ["empty", "closed", "partial", "foreign", "additional", "oversized"])
def test_actual_queued_grant_peek_cannot_substitute_expected_records_for_received_bytes(fault: str) -> None:
    import fcntl

    expected = canonical({"schema": "management-recovery-epoch-grant-v1", "request_id": "component-request"}) + b"\n"
    with ExitStack() as held:
        sender, receiver = socket.socketpair()
        held.enter_context(sender)
        held.enter_context(receiver)
        before = fcntl.fcntl(receiver.fileno(), fcntl.F_GETFL)
        payload = b""
        if fault == "closed":
            sender.close()
        elif fault == "partial":
            payload = expected[:-1]
        elif fault == "foreign":
            payload = canonical({"schema": "management-recovery-epoch-grant-v1", "request_id": "foreign-request"}) + b"\n"
        elif fault == "additional":
            payload = expected + b"unexpected-second-frame\n"
        elif fault == "oversized":
            payload = b"x" * (MAX_MESSAGE_BYTES + 1)
        if payload:
            sender.sendall(payload)
        with pytest.raises(AssertionError, match="queued grant|consumed grant"):
            _peek_queued_packet(receiver.fileno(), expected)
        assert fcntl.fcntl(receiver.fileno(), fcntl.F_GETFL) == before
        if payload:
            assert receiver.recv(MAX_MESSAGE_BYTES + 2, socket.MSG_DONTWAIT) == payload


def test_actual_socket_peer_reader_does_not_accept_datagram_transport() -> None:
    with ExitStack() as held:
        sender, receiver = socket.socketpair(type=socket.SOCK_DGRAM)
        held.enter_context(sender)
        held.enter_context(receiver)
        assert _local_socket_peer(receiver.fileno()) is None
