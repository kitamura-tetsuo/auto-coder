"""Real kernel resource observations, not a complete Docker/host acceptance.

The typed enrollment is a component selector. Its files and actual holders are
real; no kernel, process, ancestor-protection or flock observer is replaced.
Full production enrollment/profile/controller closure belongs to other tests.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import select
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

import pytest

from auto_coder.management.containment_enrollment import OriginalNetworkPin
from auto_coder.management.recovery_containment_model import NamedDescriptor, RecoveryContainmentUnavailable, RecoveryEnrollment, RecoveryFile, RecoveryLock, RecoveryOriginal, RecoveryResources, RecoveryTarget, RecoveryWatcher
from auto_coder.management.recovery_containment_resources import hold_observed_resources

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")

_HOLDER = r"""
import fcntl,json,os,sys
from pathlib import Path
paths, inherited = map(json.loads,sys.argv[1:])
held={}
for name,operation in (('original',fcntl.LOCK_SH),('controller',fcntl.LOCK_EX)):
    if name in inherited:
        held[name]=inherited[name]
    else:
        held[name]=os.open(paths[name],os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        fcntl.flock(held[name],operation|fcntl.LOCK_NB)
held['namespace']=os.open('/proc/self/ns/net',os.O_RDONLY|os.O_CLOEXEC)
print(json.dumps({'pid':os.getpid(),'start':Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'descriptors':held}),flush=True)
for command in sys.stdin:
    command=command.strip()
    if command=='exit': break
    if command.startswith('unlock-'):
        fcntl.flock(held[command.removeprefix('unlock-')],fcntl.LOCK_UN)
    elif command.startswith('close-'):
        os.close(held[command.removeprefix('close-')])
    elif command.startswith('replace-'):
        name=command.removeprefix('replace-')
        previous=held[name]
        os.close(previous)
        replacement=os.open(paths['other'],os.O_RDONLY|os.O_NOFOLLOW)
        if replacement!=previous:
            os.dup2(replacement,previous)
            os.close(replacement)
    else: raise RuntimeError('unsupported holder barrier')
    print(command,flush=True)
"""


@dataclass
class ResourceCase:
    root: Path
    process: subprocess.Popen[str]
    enrollment: RecoveryEnrollment
    resources: RecoveryResources
    original_namespace: int
    paths: dict[str, Path]
    parent_descriptors: list[int]

    def command(self, value: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()
        assert line(self.process) == value


def line(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 10)[0], "real resource holder did not reach its barrier"
    result = process.stdout.readline().strip()
    assert result, (process.poll(), process.stderr.read() if process.poll() is not None and process.stderr is not None else "")
    return result


def bound(path: Path) -> RecoveryFile:
    info = path.stat()
    return RecoveryFile(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), info.st_dev, info.st_ino)


def selected_lock(path: Path) -> RecoveryLock:
    info = path.stat()
    return RecoveryLock(str(path), info.st_dev, info.st_ino)


@contextmanager
def resource_case(mode: str = "independent") -> Iterator[ResourceCase]:
    # Existing containment tests use the real account home to keep every actual
    # ancestor protected; pytest's temporary root may live below writable /tmp.
    with tempfile.TemporaryDirectory(prefix="ac-recovery-resources-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
        root = Path(directory)
        paths = {name: root / name for name in ("original", "controller", "target", "other", "configuration")}
        parent_fds: list[int] = []
        process = None
        try:
            for path in paths.values():
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                os.close(descriptor)
            inherited = {}
            if mode.startswith("inherited-"):
                name = mode.removeprefix("inherited-")
                descriptor = os.open(paths[name], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                parent_fds.append(descriptor)
                fcntl.flock(descriptor, (fcntl.LOCK_SH if name == "original" else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                inherited[name] = descriptor
            namespace = os.open("/proc/self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
            parent_fds.append(namespace)
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", _HOLDER, json.dumps({name: str(path) for name, path in paths.items()}), json.dumps(inherited)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=tuple(inherited.values()))
            observed = json.loads(line(process))
            descriptors = observed["descriptors"]
            resources = RecoveryResources(observed["pid"], observed["start"], observed["boot"], (NamedDescriptor("generation-one", descriptors["original"]),), (NamedDescriptor("recovery-watch.service", descriptors["controller"]),), (NamedDescriptor("generation-one", descriptors["namespace"]),))
            namespace_info = os.fstat(namespace)
            parent_start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
            pin = OriginalNetworkPin(holder_pid=os.getpid(), holder_start_token=parent_start, holder_boot_id=observed["boot"], holder_fd=namespace, namespace=os.readlink(f"/proc/self/fd/{namespace}"), device=namespace_info.st_dev, inode=namespace_info.st_ino)
            configuration = bound(paths["configuration"])
            original = RecoveryOriginal("generation-one", "original-one", "a" * 64, "sha256:" + "b" * 64, "c" * 64, "sha256:" + "d" * 64, "started", observed["boot"], configuration, configuration, configuration, (), selected_lock(paths["original"]), configuration, pin)
            target = RecoveryTarget("target-one", "e" * 64, "sha256:" + "f" * 64, configuration, configuration, (), selected_lock(paths["target"]), 18080)
            watcher = RecoveryWatcher("recovery-watch.service", configuration, configuration, selected_lock(paths["controller"]))
            enrollment = RecoveryEnrollment("approved-management-recovery-containment-v1", "deployment-one", "enrollment-one", "authorization-one", "ordinary-recovery-v1", "engine-one", "sha256:" + "0" * 64, (original,), target, (watcher,), True)
            yield ResourceCase(root, process, enrollment, resources, namespace, paths, parent_fds)
        finally:
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
            for descriptor in parent_fds:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def test_real_independent_resource_holder_is_observed_without_observer_flock_authority() -> None:
    with resource_case() as case:
        before = set(os.listdir("/proc/self/fd"))
        with hold_observed_resources(case.enrollment, case.resources, expected_uid=os.geteuid()) as observation:
            assert observation.holder_pid == case.process.pid
            assert [(item.name, item.kind) for item in observation.resources] == [("generation-one", "original-sh"), ("recovery-watch.service", "controller-ex"), ("generation-one", "network-namespace")]
            locks = [item.kernel_lock for item in observation.resources if item.kernel_lock is not None]
            assert [(lock.pid, lock.mode) for lock in locks] == [(case.process.pid, "READ"), (case.process.pid, "WRITE")]
            contenders = [os.open(case.paths[name], os.O_RDONLY | os.O_NOFOLLOW) for name in ("original", "controller")]
            try:
                for descriptor in contenders:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                for descriptor in contenders:
                    os.close(descriptor)
        assert set(os.listdir("/proc/self/fd")) == before
        # The observer did not acquire an independent lock or release the owner.
        case.command("unlock-original")
        case.command("unlock-controller")
        for name in ("original", "controller"):
            descriptor = os.open(case.paths[name], os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)


@pytest.mark.parametrize("mode", ["inherited-original", "inherited-controller"])
def test_inherited_open_file_description_is_not_independent_issuer_acquisition(mode: str) -> None:
    with resource_case(mode) as case:
        before = set(os.listdir("/proc/self/fd"))
        with pytest.raises(RecoveryContainmentUnavailable, match="independently acquired"):
            with hold_observed_resources(case.enrollment, case.resources, expected_uid=os.geteuid()):
                pytest.fail("an inherited lock was accepted as independent custody")
        assert set(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("change", ["unlock-original", "unlock-controller", "close-original", "close-controller", "close-namespace", "replace-original", "replace-controller", "replace-namespace", "holder-death", "original-pin-closed", "owner-path-replaced"])
def test_real_resource_change_at_observed_barrier_refuses_and_closes_observer_references(change: str) -> None:
    with resource_case() as case:
        before = len(os.listdir("/proc/self/fd"))
        with pytest.raises((ValueError, OSError)):
            with hold_observed_resources(case.enrollment, case.resources, expected_uid=os.geteuid()):
                if change == "holder-death":
                    case.process.kill()
                    case.process.wait(timeout=5)
                elif change == "original-pin-closed":
                    os.close(case.original_namespace)
                    case.parent_descriptors.remove(case.original_namespace)
                elif change == "owner-path-replaced":
                    case.paths["original"].rename(case.root / "retained-owner")
                    descriptor = os.open(case.paths["original"], os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(descriptor)
                else:
                    case.command(change)
        expected = before - int(change == "original-pin-closed")
        assert len(os.listdir("/proc/self/fd")) == expected


@pytest.mark.parametrize("field", ["original_owner_fds", "controller_fds", "namespace_fds"])
def test_required_resource_group_cannot_be_omitted(field: str) -> None:
    with resource_case() as case:
        changed = replace(case.resources, **{field: ()})
        with pytest.raises(RecoveryContainmentUnavailable, match="membership"):
            with hold_observed_resources(case.enrollment, changed, expected_uid=os.geteuid()):
                pytest.fail("missing required resource group was accepted")


@pytest.mark.parametrize("field,value", [("holder_start_token", "1"), ("holder_boot_id", "00000000-0000-0000-0000-000000000000")])
def test_holder_lifetime_binding_cannot_name_another_process_instance(field: str, value: str) -> None:
    with resource_case() as case:
        changed = replace(case.resources, **{field: value})
        with pytest.raises(RecoveryContainmentUnavailable, match="changed or exited"):
            with hold_observed_resources(case.enrollment, changed, expected_uid=os.geteuid()):
                pytest.fail("another lifetime was accepted")
