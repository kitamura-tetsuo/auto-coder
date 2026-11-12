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
from dataclasses import dataclass
from pathlib import Path

import pytest
from scripts.recovery_epoch_native_peer import (
    LATE_MUTATION_SCENARIOS,
    REQUIRED_PEER_CASES,
    KernelPeerMessageBarrier,
    _ControlHeader,
    _decode_rights,
    _Iovec,
    _MessageHeader,
    mutate_after_intent,
)

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
    assert len(LATE_MUTATION_SCENARIOS) == 10 and len(set(LATE_MUTATION_SCENARIOS)) == 10
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
