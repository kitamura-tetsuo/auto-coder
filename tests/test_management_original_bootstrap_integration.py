"""Native original bootstrap-store integration; not a real-host grant/child oracle."""

import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from auto_coder.management.bootstrap_store import ChildPhase, ChildRecord
from auto_coder.management.bootstrap_supervisor import Observation, ResultStatus
from auto_coder.management.original_attempt_evidence import derive, observe
from auto_coder.management.original_attempts import read_frontier
from tests.test_management_fresh_launch_exclusions import first_launch_case


def _original_bytes(root: Path) -> dict[str, bytes]:
    # SQLite read-only observers can leave zero-length WAL and shared-memory
    # sidecars. Compare original database/owner records, not transient sidecars.
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file() and (path.suffix == ".sqlite3" or path.name == "owner.lock")}


# The separate consumer reopens the actual original receipt and SQLite files.
# It cannot issue a grant and cannot infer full bootstrap readiness.
_NATIVE_CONSUMER = """
import json
import sqlite3
import sys
from pathlib import Path
from auto_coder.management.original_attempts import identity, read_original

root = Path(sys.argv[1])
record = json.loads(read_original(root / "fresh-original-stores.json")[0])
verified = {}
for role, expected in record["stores"].items():
    path = (
        Path(sys.argv[2]) / "management-operations.sqlite3"
        if role == "operations"
        else root / {
            "child": "management-child.sqlite3",
            "handoff": "management-self-update.sqlite3",
            "recovery": "management-recovery.sqlite3",
        }[role]
    )
    if list(identity(path)) != expected:
        raise ValueError("native original-store identity differs")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("native original store corrupt")
        tables = sorted(row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type=?", ("table",)))
        if "metadata" not in tables:
            raise ValueError("native original schema is missing")
        verified[role] = [path.name, len(tables)]
    finally:
        connection.close()
print(json.dumps(verified, sort_keys=True))
"""


@pytest.mark.parametrize("first_launch_case", ["original-history"], indirect=True)
def test_bootstrap_producer_generates_original_store_receipt_and_native_databases(first_launch_case, monkeypatch):
    """The real Supervisor native-store path runs; host guard and child are substituted."""
    case = first_launch_case
    assert case.original_binding is not None
    binding = case.original_binding
    os.fchmod(case.owner.fileno(), 0o600)
    source_bytes = {str(path): path.read_bytes() for path in case.paths}
    retained = _original_bytes(case.oauth)
    prior = {str(root): _original_bytes(root) for root in (case.a, case.b)}
    dispatches = []

    def accepted_child(*args):
        dispatches.append(args)
        return Observation(
            ResultStatus.OBSERVED_RUNNING,
            ChildRecord(case.intent.new_instance, ChildPhase.RUNNING, "bootstrap", pid=123, start_token="456", child_generation=1),
        )

    monkeypatch.setattr(case.supervisor, "start_selected", accepted_child)
    assert case.supervisor._start_fresh_initialization()
    assert len(case.grants) == 1 and len(dispatches) == 1

    # This original receipt was produced by the real native Supervisor path.
    receipt_path = case.state / "fresh-original-stores.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["request_id"] == case.intent.request_id
    assert receipt["history"] == binding.history
    assert receipt["source"] == binding.source
    assert receipt["intent_sha256"] == hashlib.sha256((case.state / "fresh-initialization.json").read_bytes()).hexdigest()
    assert set(receipt["stores"]) == {"child", "operations", "handoff", "recovery"}
    assert set(receipt["lifetime"]) == {"pid", "start", "boot", "nonce"}
    assert receipt["lifetime"]["pid"] == os.getpid()
    owner_info = (case.state / ".supervisor-owner.lock").stat()
    assert receipt["owner_fence"] == [owner_info.st_dev, owner_info.st_ino]
    with (case.state / ".supervisor-owner.lock").open("rb") as contender:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    separate = subprocess.run(
        [sys.executable, "-B", "-c", _NATIVE_CONSUMER, str(case.state), str(case.operations)],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=False,
    )
    assert separate.returncode == 0, separate.stderr
    actual_roles = json.loads(separate.stdout)
    assert set(actual_roles) == set(receipt["stores"])
    assert all(value[1] >= 2 for value in actual_roles.values())

    authority, events = read_frontier(Path(binding.root), binding)
    assert authority.binding == binding
    assert [(event.kind, event.attempt) for event in events] == [
        ("anchor", case.intent.request_id),
        ("claimed", case.intent.request_id),
    ]
    with observe(binding) as fence:
        independent = derive(binding, fence)
        assert independent.attempts[0].state == "claimed"
        # The grant transport was substituted; no original host grant-possible
        # fact was produced, therefore this remains UNAVAILABLE for recovery.
        assert independent.unavailable == (case.intent.request_id + ": original grant boundary not crossed",)

    assert {str(path): path.read_bytes() for path in case.paths} == source_bytes
    assert _original_bytes(case.oauth) == retained
    assert {str(root): _original_bytes(root) for root in (case.a, case.b)} == prior
    # A successful stubbed launch is not an OS child, authenticated readback,
    # historical completeness or any authorization to merge or recover.
    assert case.supervisor._start_fresh_initialization() is False
    assert len(case.grants) == 1 and len(dispatches) == 1


@pytest.mark.parametrize("first_launch_case", ["original-history"], indirect=True)
def test_original_supervisor_owner_file_is_private_under_permissive_umask(first_launch_case):
    """The actual Supervisor owner opener, not the fixture opener, creates 0600."""
    case = first_launch_case
    path = case.state / ".supervisor-owner.lock"
    case.owner.close()
    path.unlink()
    previous = os.umask(0o002)
    try:
        with case.supervisor._owner_file() as ownership:
            fcntl.flock(ownership.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(ownership.fileno())
            assert info.st_mode & 0o777 == 0o600
            assert (info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino)
            with path.open("rb") as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.umask(previous)


@pytest.mark.parametrize("first_launch_case", ["original-history"], indirect=True)
@pytest.mark.parametrize("invalid", ["permissive", "symlink"])
def test_original_supervisor_owner_rejects_unprotected_existing_inode(first_launch_case, invalid):
    """Original evidence is never silently repaired or rebound to another inode."""
    case = first_launch_case
    path = case.state / ".supervisor-owner.lock"
    case.owner.close()
    if invalid == "permissive":
        path.chmod(0o664)
        original = path.read_bytes()
        with pytest.raises(PermissionError, match="original-lineage Supervisor owner lock must be protected"):
            with case.supervisor._owner_file():
                pytest.fail("unprotected owner file admitted")
        assert path.read_bytes() == original
        assert path.stat().st_mode & 0o777 == 0o664
    else:
        path.unlink()
        path.symlink_to(case.oauth / "oauth.sqlite3")
        original = (case.oauth / "oauth.sqlite3").read_bytes()
        with pytest.raises(OSError):
            with case.supervisor._owner_file():
                pytest.fail("symlinked owner file admitted")
        assert path.is_symlink()
        assert (case.oauth / "oauth.sqlite3").read_bytes() == original
    assert not (case.state / "fresh-launch-claimed.json").exists()
    assert not case.supervisor.store.path.exists()
