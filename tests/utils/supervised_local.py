"""Deterministic finite-writer supervision for local-backend tests."""

import os
import shutil
import signal
import tempfile
from pathlib import Path

from src.auto_coder.invocation_process_supervisor import InstallationContext, InvocationProcessSupervisor, PolicyInstallation


class ProcessGroupOwner:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="auto-coder-test-owners-"))

    def prepare(self, invocation_id: str) -> Path:
        group = self.root / invocation_id
        group.mkdir()
        return group

    @staticmethod
    def child_joiner(group: Path):
        def join() -> None:
            os.setsid()
            (group / "pid").write_text(str(os.getpid()))

        return join

    @staticmethod
    def stop_and_confirm(group: Path, deadline: float) -> None:
        try:
            os.killpg(int((group / "pid").read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def discard(group: Path) -> None:
        shutil.rmtree(group, ignore_errors=True)


class TestFilesystemPolicy:
    __test__ = False

    def install(self, context: InstallationContext) -> PolicyInstallation:
        return PolicyInstallation(True, establishes_filesystem_enforcement=True)

    def close(self) -> None:
        pass


def make_test_supervisor() -> InvocationProcessSupervisor:
    return InvocationProcessSupervisor(owner=ProcessGroupOwner(), filesystem_policy_factory=TestFilesystemPolicy)  # type: ignore[arg-type]


def install_test_supervisor(manager):
    manager._local_supervisor_factory = make_test_supervisor
    return manager
