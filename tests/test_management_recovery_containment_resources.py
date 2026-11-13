"""Real kernel resource observations, not a complete Docker/host acceptance.

The typed enrollment is a component selector. Its files and actual holders are
real; no kernel, process, ancestor-protection or flock observer is replaced.
Full production enrollment/profile/controller closure belongs to other tests.
"""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import os
import pwd
import select
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Callable, Iterator, ParamSpec

import pytest

from auto_coder.management.containment_enrollment import OriginalNetworkPin
from auto_coder.management.recovery_containment_model import NamedDescriptor, RecoveryContainmentUnavailable, RecoveryEnrollment, RecoveryFile, RecoveryLock, RecoveryOriginal, RecoveryResources, RecoveryTarget, RecoveryWatcher
from auto_coder.management.recovery_containment_resources import hold_observed_resources

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")

_PARAMETERS = ParamSpec("_PARAMETERS")
_CASE_PARAMETER_SETS: dict[str, tuple[tuple[tuple[str, str], ...], ...]] = {
    "test_real_independent_resource_holder_is_observed_without_observer_flock_authority": ((),),
    "test_inherited_open_file_description_is_not_independent_issuer_acquisition": tuple((("mode", value),) for value in ("inherited-original", "inherited-controller")),
    "test_real_resource_change_at_observed_barrier_refuses_and_closes_observer_references": tuple(
        (("change", value),) for value in ("unlock-original", "unlock-controller", "close-original", "close-controller", "close-namespace", "replace-original", "replace-controller", "replace-namespace", "holder-death", "original-pin-closed", "owner-path-replaced")
    ),
    "test_required_resource_group_cannot_be_omitted": tuple((("field", value),) for value in ("original_owner_fds", "controller_fds", "namespace_fds")),
    "test_holder_lifetime_binding_cannot_name_another_process_instance": ((("field", "holder_start_token"), ("value", "1")), (("field", "holder_boot_id"), ("value", "00000000-0000-0000-0000-000000000000"))),
}
_RESOURCE_ASSERTIONS: dict[str, Callable[..., None]] = {}
_CASE_RECEIPT = "RECOVERY_RESOURCE_CASE_COMPLETED "


def _selected_resource_case(raw: object) -> tuple[str, dict[str, str]]:
    if not isinstance(raw, dict) or set(raw) != {"case", "parameters"}:
        raise ValueError("resource component invocation is malformed")
    name, parameters = raw["case"], raw["parameters"]
    if not isinstance(name, str) or name not in _CASE_PARAMETER_SETS or not isinstance(parameters, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in parameters.items()):
        raise ValueError("resource component invocation is not selected")
    if tuple(sorted(parameters.items())) not in _CASE_PARAMETER_SETS[name]:
        raise ValueError("resource component parameters are not selected")
    return name, parameters


def _read_host_namespaces() -> None:
    for namespace in ("pid", "user", "mnt", "net"):
        os.readlink("/proc/1/ns/" + namespace)


def _native_resource_case(function: Callable[_PARAMETERS, None]) -> Callable[_PARAMETERS, None]:
    """Run the identical real assertions with CI's explicitly available sudo.

    The parent pytest node remains responsible for its result and source receipt.
    Local test execution never escalates automatically, and an unavailable host
    component is a failure rather than evidence of native VM acceptance.
    """
    _RESOURCE_ASSERTIONS[function.__name__] = function

    @wraps(function)
    def run(*args: _PARAMETERS.args, **kwargs: _PARAMETERS.kwargs) -> None:
        try:
            _read_host_namespaces()
        except OSError as exc:
            if os.environ.get("GITHUB_ACTIONS") != "true":
                pytest.fail("Actual host namespace access is unavailable for this component test; run it explicitly with the required host permissions. This is not native VM acceptance: " + str(exc))
            parameters = inspect.signature(function).bind(*args, **kwargs).arguments
            name, selected = _selected_resource_case({"case": function.__name__, "parameters": parameters})
            invocation = json.dumps({"case": name, "parameters": selected}, sort_keys=True)
            root = Path(__file__).resolve().parents[1]
            command = ["sudo", "-n", sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--resource-case", invocation]
            try:
                completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60, check=False)
            except (OSError, subprocess.TimeoutExpired) as failure:
                pytest.fail("Genuine privileged resource component execution is unavailable: " + str(failure))
            assert completed.returncode == 0, "Genuine privileged resource assertions did not complete:\n" + completed.stdout + completed.stderr
            receipts = [json.loads(line.removeprefix(_CASE_RECEIPT)) for line in completed.stdout.splitlines() if line.startswith(_CASE_RECEIPT)]
            assert len(receipts) == 1, "The selected resource assertions did not produce their completion receipt"
            receipt = receipts[0]
            assert receipt == {"case": name, "parameters": selected, "uid": 0, "source": str(Path(__file__).resolve())}
            return
        function(*args, **kwargs)

    return run


def _run_resource_case(arguments: list[str]) -> None:
    if len(arguments) != 2 or arguments[0] != "--resource-case":
        raise ValueError("only an explicitly selected resource component case can run")
    name, parameters = _selected_resource_case(json.loads(arguments[1]))
    if os.geteuid() != 0:
        raise RuntimeError("the privileged resource component child did not obtain root")
    expected_source = Path(__file__).resolve().parents[1] / "src/auto_coder/management/recovery_containment_resources.py"
    if Path(inspect.getsourcefile(inspect.unwrap(hold_observed_resources)) or "").resolve() != expected_source:
        raise RuntimeError("the resource component child imported another checkout")
    _read_host_namespaces()
    _RESOURCE_ASSERTIONS[name](**parameters)
    print(_CASE_RECEIPT + json.dumps({"case": name, "parameters": parameters, "uid": os.geteuid(), "source": str(Path(__file__).resolve())}, sort_keys=True), flush=True)


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
            unit = root / "recovery-watch.service"
            unit.write_bytes(b"# Resource-observation fixture; no executable controller.\n")
            unit.chmod(0o600)
            watcher = RecoveryWatcher("recovery-watch.service", configuration, configuration, bound(unit), selected_lock(paths["controller"]))
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


@_native_resource_case
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
@_native_resource_case
def test_inherited_open_file_description_is_not_independent_issuer_acquisition(mode: str) -> None:
    with resource_case(mode) as case:
        before = set(os.listdir("/proc/self/fd"))
        with pytest.raises(RecoveryContainmentUnavailable, match="independently acquired"):
            with hold_observed_resources(case.enrollment, case.resources, expected_uid=os.geteuid()):
                pytest.fail("an inherited lock was accepted as independent custody")
        assert set(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("change", ["unlock-original", "unlock-controller", "close-original", "close-controller", "close-namespace", "replace-original", "replace-controller", "replace-namespace", "holder-death", "original-pin-closed", "owner-path-replaced"])
@_native_resource_case
def test_real_resource_change_at_observed_barrier_refuses_and_closes_observer_references(change: str) -> None:
    with resource_case() as case:
        before = len(os.listdir("/proc/self/fd"))
        entered = injected = False
        with pytest.raises((ValueError, OSError)):
            with hold_observed_resources(case.enrollment, case.resources, expected_uid=os.geteuid()):
                entered = True
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
                injected = True
        assert entered, "the resource observation failed before the real mutation barrier"
        assert injected, "the intended real resource mutation did not complete"
        expected = before - int(change == "original-pin-closed")
        assert len(os.listdir("/proc/self/fd")) == expected


@pytest.mark.parametrize("field", ["original_owner_fds", "controller_fds", "namespace_fds"])
@_native_resource_case
def test_required_resource_group_cannot_be_omitted(field: str) -> None:
    with resource_case() as case:
        if field == "original_owner_fds":
            changed = replace(case.resources, original_owner_fds=())
        elif field == "controller_fds":
            changed = replace(case.resources, controller_fds=())
        else:
            assert field == "namespace_fds"
            changed = replace(case.resources, namespace_fds=())
        with pytest.raises(RecoveryContainmentUnavailable, match="membership"):
            with hold_observed_resources(case.enrollment, changed, expected_uid=os.geteuid()):
                pytest.fail("missing required resource group was accepted")


@pytest.mark.parametrize("field,value", [("holder_start_token", "1"), ("holder_boot_id", "00000000-0000-0000-0000-000000000000")])
@_native_resource_case
def test_holder_lifetime_binding_cannot_name_another_process_instance(field: str, value: str) -> None:
    with resource_case() as case:
        if field == "holder_start_token":
            changed = replace(case.resources, holder_start_token=value)
        else:
            assert field == "holder_boot_id"
            changed = replace(case.resources, holder_boot_id=value)
        with pytest.raises(RecoveryContainmentUnavailable, match="changed or exited"):
            with hold_observed_resources(case.enrollment, changed, expected_uid=os.geteuid()):
                pytest.fail("another lifetime was accepted")


@pytest.mark.parametrize(
    "invocation",
    [
        {},
        {"case": "arbitrary", "parameters": {}},
        {"case": "test_real_independent_resource_holder_is_observed_without_observer_flock_authority", "parameters": {"unexpected": "value"}},
        {"case": "test_required_resource_group_cannot_be_omitted", "parameters": {"field": "arbitrary"}},
        {"case": "test_holder_lifetime_binding_cannot_name_another_process_instance", "parameters": {"field": "holder_start_token", "value": "00000000-0000-0000-0000-000000000000"}},
    ],
)
def test_privileged_component_dispatch_rejects_unselected_case_or_parameters(invocation: object) -> None:
    with pytest.raises(ValueError, match="resource component"):
        _selected_resource_case(invocation)


def test_privileged_component_dispatch_covers_every_real_assertion() -> None:
    assert set(_RESOURCE_ASSERTIONS) == set(_CASE_PARAMETER_SETS)
    for name, selections in _CASE_PARAMETER_SETS.items():
        for parameters in selections:
            assert _selected_resource_case({"case": name, "parameters": dict(parameters)}) == (name, dict(parameters))


if __name__ == "__main__":
    _run_resource_case(sys.argv[1:])
