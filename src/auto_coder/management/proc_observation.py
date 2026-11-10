"""Bounded current Linux task facts, never deployment closure or launch authority.

Two reads detect observable changes; they do not make procfs atomic. Namespace
identities describe current tasks only, never historical namespace continuity.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .archived_exclusions import unique_pairs

_LIMIT = 1024 * 1024
_MAX_TASKS = 4096
_MAX_FDS = 8192


@dataclass(frozen=True)
class TaskFact:
    tid: int
    start_token: str
    state: str
    namespaces: tuple[tuple[str, str], ...] = ()
    cgroups: tuple[str, ...] = ()
    socket_inodes: tuple[int, ...] = ()


@dataclass(frozen=True)
class ListenerFact:
    transport: str
    inode: int
    local_address: str
    state: str


@dataclass(frozen=True)
class ProcessObservation:
    pid: int
    boot_id: str
    start_token: str
    tasks: tuple[TaskFact, ...]
    listeners: tuple[ListenerFact, ...]
    unavailable: tuple[str, ...]


@dataclass(frozen=True)
class ProcReport:
    schema: str
    status: str
    observation_id: str
    observer_pid: int
    started_monotonic_ns: int
    finished_monotonic_ns: int
    processes: tuple[ProcessObservation, ...]
    unavailable: tuple[str, ...]
    resource_limit: str = "Selected current tasks and their network tables only; no host-wide scan, namespace pin, cgroup closure, endpoint reservation or deployment authority. All descriptors released before return."


def _read(path: Path) -> str:
    with path.open("rb") as stream:
        value = stream.read(_LIMIT + 1)
    if len(value) > _LIMIT:
        raise ValueError("proc observation exceeds size limit")
    return value.decode("utf-8")


def _entries(path: Path, limit: int) -> tuple[str, ...]:
    names = []
    with os.scandir(path) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > limit:
                raise ValueError("proc directory exceeds entry limit")
    return tuple(sorted(names))


def _stat(path: Path, expected: int) -> tuple[str, str]:
    value = _read(path / "stat")
    prefix, separator, tail = value.rpartition(")")
    fields = tail.split()
    if not separator or prefix.split("(", 1)[0].strip() != str(expected) or len(fields) < 20 or not fields[19].isdecimal() or int(fields[19]) <= 0 or fields[0] not in ("R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I"):
        raise ValueError("invalid task lifetime stat")
    return fields[19], fields[0]


def _task(path: Path, tid: int) -> TaskFact:
    token, state = _stat(path, tid)
    names = _entries(path / "ns", 32)
    if not {"pid", "net", "mnt", "user"} <= set(names):
        raise ValueError("required task namespaces unavailable")
    namespaces = tuple((name, os.readlink(path / "ns" / name)) for name in names)
    if any(not re.fullmatch(r"[a-z]+:\[[0-9]+\]", target) for _, target in namespaces):
        raise ValueError("invalid namespace identity")
    cgroups = tuple(sorted(_read(path / "cgroup").splitlines()))
    if not cgroups or any(not re.fullmatch(r"[0-9]+:[^:]*:/.*", line) for line in cgroups):
        raise ValueError("task cgroup membership unavailable")
    sockets = []
    for fd in _entries(path / "fd", _MAX_FDS):
        target = os.readlink(path / "fd" / fd)
        match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
        if match:
            sockets.append(int(match[1]))
    if _stat(path, tid) != (token, state):
        raise ValueError("task lifetime or state changed during observation")
    return TaskFact(tid, token, state, namespaces, cgroups, tuple(sorted(set(sockets))))


def _listeners(path: Path) -> tuple[ListenerFact, ...]:
    result = []
    for transport in ("tcp", "tcp6", "udp", "udp6", "unix"):
        lines = _read(path / "net" / transport).splitlines()
        if not lines or ("Num" if transport == "unix" else "local_address") not in lines[0]:
            raise ValueError("unsupported socket table header: " + transport)
        for line in lines[1:]:
            fields = line.split()
            if transport == "unix":
                if len(fields) < 7:
                    raise ValueError("invalid Unix socket row")
                flags, inode = int(fields[3], 16), int(fields[6])
                if flags & 0x10000:
                    # Paths are not needed for identity, and may contain spaces.
                    result.append(ListenerFact(transport, inode, "", fields[5]))
            else:
                if len(fields) < 10:
                    raise ValueError("invalid Internet socket row")
                inode = int(fields[9])
                if not re.fullmatch(r"[0-9A-Fa-f]+:[0-9A-Fa-f]{4}", fields[1]):
                    raise ValueError("invalid socket address")
                if inode > 0 and (fields[3] == "0A" or transport.startswith("udp")):
                    result.append(ListenerFact(transport, inode, fields[1], fields[3]))
    return tuple(sorted(result, key=lambda item: (item.transport, item.inode, item.local_address, item.state)))


def observe_process(pid: int) -> ProcessObservation:
    """Read all currently listed threads of one selected process, with no absence verdict."""
    boot, token = "", ""
    tasks: tuple[TaskFact, ...] = ()
    listeners: tuple[ListenerFact, ...] = ()
    issues: list[str] = []
    descriptor = None
    try:
        if type(pid) is not int or not 0 < pid <= 2147483647:
            raise ValueError("positive bounded process ID required")
        boot = str(uuid.UUID(_read(Path("/proc/sys/kernel/random/boot_id")).strip()))
        path = Path("/proc") / str(pid)
        token, state = _stat(path, pid)
        # Preserve a zombie/dead lifetime even when its namespace links are gone.
        tasks = (TaskFact(pid, token, state),)
        if state in ("Z", "X", "x"):
            raise ValueError("zombie/dead task observed; not absence")
        descriptor = os.pidfd_open(pid, 0)
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        if poller.poll(0):
            raise ValueError("selected lifetime exited before observation")
        names = _entries(path / "task", _MAX_TASKS)
        if str(pid) not in names or any(not name.isdecimal() for name in names):
            raise ValueError("selected PID is not an inspectable thread-group leader")
        tasks = tuple(_task(path / "task" / name, int(name)) for name in names)
        # Each task can occupy a different network namespace. Without setns or
        # retained pins this component cannot inspect those tables independently.
        if len({dict(task.namespaces)["net"] for task in tasks}) != 1:
            raise ValueError("task network namespaces differ; independent tables unavailable")
        listeners = _listeners(path)
        repeated = tuple(_task(path / "task" / name, int(name)) for name in names)
        if repeated != tasks or _entries(path / "task", _MAX_TASKS) != names or _listeners(path) != listeners or _stat(path, pid) != (token, state) or poller.poll(0) or _read(Path("/proc/sys/kernel/random/boot_id")).strip() != boot:
            raise ValueError("process/task/namespace/socket observation changed")
    except (OSError, ValueError, UnicodeError, AttributeError) as exc:
        # ENOENT and EPERM both mean unavailable, never proof of task absence.
        issues.append(type(exc).__name__ + ": " + str(exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return ProcessObservation(pid, boot, token, tasks, listeners, tuple(issues))


def observe(pids: tuple[int, ...]) -> ProcReport:
    if not pids or len(pids) > 32 or len(set(pids)) != len(pids):
        raise ValueError("select 1 to 32 distinct process IDs")
    started = time.monotonic_ns()
    processes = tuple(observe_process(pid) for pid in sorted(pids))
    return ProcReport(
        "current-proc-facts-v1", "unavailable", str(uuid.uuid4()), os.getpid(), started, time.monotonic_ns(), processes, ("Selected process facts do not establish host authority, complete deployment membership, cgroup task closure, inhibited restarters or historical namespace continuity.",)
    )


def verify(pids: tuple[int, ...], previous: object) -> ProcReport:
    """Always observe anew; copied facts and stable PIDs confer no authority."""
    current = observe(pids)
    issues = list(current.unavailable)
    if not isinstance(previous, dict) or set(previous) != set(ProcReport.__dataclass_fields__) or previous.get("schema") != current.schema or previous.get("status") != "unavailable":
        issues.append("Unsupported or authorizing saved process report")
    else:
        value = json.loads(json.dumps(asdict(current)))
        for field in ("processes", "resource_limit"):
            if previous[field] != value[field]:
                issues.append("Independent current observation differs: " + field)
    return replace(current, unavailable=tuple(issues))


def main() -> int:
    parser = argparse.ArgumentParser(description="Non-authorizing current proc facts; always unavailable for deployment containment")
    parser.add_argument("command", choices=("observe", "verify"))
    parser.add_argument("--pid", type=int, action="append", required=True)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    if (args.command == "verify") != (args.previous is not None):
        parser.error("verify requires --previous; observe does not accept it")
    try:
        previous = json.loads(_read(args.previous), object_pairs_hook=unique_pairs) if args.previous is not None else None
        result = verify(tuple(args.pid), previous) if args.command == "verify" else observe(tuple(args.pid))
        print(json.dumps(asdict(result), sort_keys=True))
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
