"""Host-boundary unit controls use real private provenance files and real locks."""

import fcntl
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management.archived_exclusions import parse_exclusions
from auto_coder.management.bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor
from auto_coder.management.exclusion_host import check_host_archives, inherited_support, required_exclusions, validate_successor
from auto_coder.management.fresh_initialization import claim_fresh_initialization
from tests.test_management_archived_exclusions import generation, identity
from tests.test_management_fresh_initialization import _intent


def write(path: Path, value: object, mode: int = 0o600) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    path.chmod(mode)


@pytest.fixture
def host_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    a = tmp_path / "a"
    generation(a, "manager-a")
    b = tmp_path / "b"
    b.mkdir()
    original = _intent(b)
    bootstrap = b / "bootstrap"
    bootstrap.mkdir(mode=0o700)
    config = {"instance_id": "manager-b", "state_root": "/management-child-state", "journal_root": "/management-operations", "oauth_root": "/management-oauth", "fresh_prior_lock_path": "/management-prior-owner.lock"}
    write(bootstrap / "config.json", config)
    intent_b = replace(
        original,
        old_instance="manager-a",
        new_instance="manager-b",
        state_root=config["state_root"],
        journal_root=config["journal_root"],
        oauth_root=config["oauth_root"],
        bootstrap_config_sha256=hashlib.sha256((bootstrap / "config.json").read_bytes()).hexdigest(),
        host_prior_journal_root=str(a),
        prior_journal_root="/management-prior-operations",
        prior_journal_device=identity(a / "management-operations.sqlite3")[0],
        prior_journal_inode=identity(a / "management-operations.sqlite3")[1],
        owner_lock_device=identity(a / "owner.lock")[0],
        owner_lock_inode=identity(a / "owner.lock")[1],
    )
    state = b / "state"
    digest = claim_fresh_initialization(state, intent_b)
    incarnation = str(uuid.uuid4())
    write(state / "fresh-launch-claimed.json", {"request_id": intent_b.request_id, "intent_sha256": digest, "owner_incarnation": incarnation, "new_container_id": intent_b.new_container_id, "challenge": "a" * 64})
    launched = {"request_id": intent_b.request_id, "owner_incarnation": incarnation, "child_identity": "1:2:1:bootstrap:owner", "observation_id": "a" * 32, "started": 1, "deadline": 31}
    write(state / "fresh-launched.json", launched)
    write(state / "fresh-outcome.json", {"request_id": intent_b.request_id, "observation_id": launched["observation_id"], "status": "succeeded"})
    generation(b / "journal", "manager-b")
    (state / ".supervisor-owner.lock").touch(mode=0o600)
    rows = [
        (bootstrap, "/bootstrap", False),
        (state, "/management-child-state", True),
        (b / "journal", "/management-operations", True),
        (b / "oauth", "/management-oauth", True),
        (b / "guard", "/management-initialization", False),
        (a, "/management-prior-operations", False),
        (a / "owner.lock", "/management-prior-owner.lock", False),
    ]
    mounts = [{"Type": "bind", "Source": str(source), "Destination": destination, "RW": rw} for source, destination, rw in rows]
    observed = tuple(sorted((str(source), destination, rw) for source, destination, rw in rows))
    prior = SimpleNamespace(
        bootstrap_source=str(bootstrap / "config.json"),
        bootstrap_sha256=intent_b.bootstrap_config_sha256,
        instance_id="manager-b",
        container_id="b" * 64,
        mounts_sha256=hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest(),
        image_id="sha256:" + "b" * 64,
        pid=os.getpid(),
        started_at="before",
        restart_count=0,
        published_port=8080,
        owner_lock_device=identity(state / ".supervisor-owner.lock")[0],
        owner_lock_inode=identity(state / ".supervisor-owner.lock")[1],
        net_namespace="net:[123]",
    )
    values = [None] * 18
    values[1], values[2], values[3], values[11], values[13], values[14] = prior.image_id, True, prior.pid, mounts, prior.started_at, 0
    monkeypatch.setattr("auto_coder.management.fresh_host_observation._inspect", lambda _container: values)
    required = required_exclusions(prior)
    c = tmp_path / "c"
    c.mkdir()
    base_c = _intent(c)
    successor = {
        "instance_id": "manager-c",
        "state_root": "/management-child-state",
        "oauth_root": "/management-oauth",
        "journal_root": "/management-operations",
        "listener_url": "http://127.0.0.1:8080/mcp",
        "initial_runtime": "bootstrap",
        "runtimes": [{"reference": "bootstrap", "receipt": "sha256:" + "a" * 64, "executable": "/bin/true"}],
        "fresh_guard_path": "/management-initialization/fresh-guard.sock",
        "fresh_prior_lock_path": "/management-prior-owner.lock",
        "archived_exclusions": [asdict(item) for item in required],
    }
    successor_path = c / "bootstrap.json"
    write(successor_path, successor)
    intent_c = replace(
        base_c,
        old_instance=prior.instance_id,
        old_container_id=prior.container_id,
        new_instance="manager-c",
        bootstrap_config_sha256=hashlib.sha256(successor_path.read_bytes()).hexdigest(),
        owner_lock_device=prior.owner_lock_device,
        owner_lock_inode=prior.owner_lock_inode,
        host_prior_journal_root=str(b / "journal"),
        prior_journal_root="/management-prior-operations",
        prior_journal_device=identity(b / "journal" / "management-operations.sqlite3")[0],
        prior_journal_inode=identity(b / "journal" / "management-operations.sqlite3")[1],
    )
    return SimpleNamespace(prior=prior, intent=intent_c, intent_b=intent_b, bootstrap=successor_path, config=successor, mounts=mounts, values=values, bindings=required, a=a, b=b, c=c)


def test_complete_ancestry_and_legacy_bytes_are_preserved(host_case):
    case = host_case
    intent_file = case.b / "state" / "fresh-initialization.json"
    original = intent_file.read_bytes()
    assert len(case.bindings) == 1 and case.bindings[0].instance_id == "manager-a"
    assert validate_successor(case.prior, case.intent, case.bootstrap) == case.bindings
    assert inherited_support(case.mounts)[1] == {"/management-prior-owner.lock", "/management-prior-operations", "/management-initialization"}
    assert intent_file.read_bytes() == original


@pytest.mark.parametrize("fault", ["omitted", "changed", "symlink", "shadow", "changed_config", "partial_fresh"])
def test_ancestor_provenance_failure_refuses(host_case, fault: str):
    case = host_case
    intent = case.intent
    if fault in {"omitted", "changed"}:
        config = case.config
        if fault == "omitted":
            config["archived_exclusions"] = []
        else:
            config["archived_exclusions"][0]["database_inode"] += 1
        write(case.bootstrap, config)
        intent = replace(intent, bootstrap_config_sha256=hashlib.sha256(case.bootstrap.read_bytes()).hexdigest())
    elif fault == "symlink":
        root = case.a / "management-operations.sqlite3"
        root.rename(case.a / "original.sqlite3")
        root.symlink_to(case.a / "original.sqlite3")
    elif fault == "shadow":
        case.mounts.append({"Type": "bind", "Source": str(case.a), "Destination": "/management-prior-operations/other", "RW": False})
    elif fault == "changed_config":
        Path(case.prior.bootstrap_source).write_text("{}")
    else:
        (case.b / "state" / "fresh-outcome.json").unlink()
    with pytest.raises((OSError, ValueError)):
        validate_successor(case.prior, intent, case.bootstrap)


def test_legacy_bootstrap_readonly_mode_is_not_newly_restricted(tmp_path: Path):
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    write(bootstrap / "config.json", {"instance_id": "legacy", "fresh_prior_lock_path": None}, mode=0o644)
    assert inherited_support([{"Type": "bind", "Source": str(bootstrap), "Destination": "/bootstrap", "RW": False}]) == ((), set())


def test_host_archives_reject_nonempty_wal(host_case):
    case = host_case
    (case.a / "management-operations.sqlite3-wal").write_bytes(b"unapplied")
    with pytest.raises(ValueError, match="WAL"):
        check_host_archives(case.bindings)


def test_supervisor_direct_fence_failure_releases_archive_fds(host_case):
    case = host_case
    supervisor = ManagementBootstrapSupervisor(BootstrapConfig.read(case.bootstrap))
    with (case.b / "state" / ".supervisor-owner.lock").open("r") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Host names stand in for the same readonly bind identity in this unit.
        supervisor.config = replace(supervisor.config, archived_exclusions=tuple(replace(item, fence=item.host_fence) for item in case.bindings), fresh_prior_lock_path=case.b / "state" / ".supervisor-owner.lock")
        with pytest.raises(BlockingIOError):
            supervisor._acquire_prior_fences(case.intent)
    assert supervisor._archived_owner_fences == () and supervisor._prior_owner_fence is None
    with (case.a / "owner.lock").open("r") as old_owner:
        fcntl.flock(old_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("interrupted", [False, True])
def test_one_host_session_holds_all_fences_across_stop_claim_and_guard(host_case, monkeypatch: pytest.MonkeyPatch, interrupted: bool):
    from auto_coder.management import exclusion_cutover as command

    case = host_case
    direct = (case.b / "state" / ".supervisor-owner.lock").open("r")
    fcntl.flock(direct, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(command, "_evidence", lambda path: (case.prior, None, case.intent.prior_evidence_sha256))
    for name in ("assert_retained_oauth_mount", "assert_prior_network_pin", "assert_known_prior_stack_stopped", "assert_retained_journal_source", "assert_prior_network_unreferenced", "assert_host_listener_absent"):
        monkeypatch.setattr(command, name, lambda *args: None)
    inspections = []

    def inspect(_container):
        with (case.a / "owner.lock").open("r") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inspections.append(True)
        values = list(case.values)
        if len(inspections) > 2:
            direct.close()  # controlled boundary: operator stopped B, never command.effect
            values[2] = False
        return values

    monkeypatch.setattr(command, "_inspect", inspect)
    marker = []

    def guard(*args, **kwargs):
        marker.append(True)
        assert len(kwargs["exclusion_fences"]) == 1
        assert (case.c / "state" / "fresh-initialization.json").exists()
        for lock in (case.a / "owner.lock", case.b / "state" / ".supervisor-owner.lock"):
            with lock.open("r") as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if interrupted:
            raise OSError("controlled guard interruption")

    monkeypatch.setattr(command, "serve_one_fresh_grant", guard)
    args = (case.intent, case.c / "evidence", case.bootstrap, case.b / "state" / ".supervisor-owner.lock", 123, case.c / "guard")
    try:
        if interrupted:
            with pytest.raises(OSError, match="interruption"):
                command.hold_claim_and_guard(*args)
        else:
            command.hold_claim_and_guard(*args)
    finally:
        direct.close()
    assert marker == [True]
    claim = (case.c / "state" / "fresh-initialization.json").read_bytes()
    with pytest.raises(ValueError, match="captured running"):
        command.hold_claim_and_guard(*args)
    assert (case.c / "state" / "fresh-initialization.json").read_bytes() == claim


def test_complete_recursive_ancestry_cannot_drop_oldest(host_case):
    from tests.test_management_archived_exclusions import binding

    case = host_case
    oldest = case.c.parent / "oldest"
    generation(oldest, "manager-oldest")
    inherited = binding(oldest, "manager-oldest")
    old_config = json.loads(Path(case.prior.bootstrap_source).read_text())
    old_config["archived_exclusions"] = [asdict(inherited)]
    write(Path(case.prior.bootstrap_source), old_config)
    digest = hashlib.sha256(Path(case.prior.bootstrap_source).read_bytes()).hexdigest()
    # Fixture sealing only: a genuine predecessor would have started with these bytes.
    intent = replace(case.intent_b, bootstrap_config_sha256=digest)
    state = case.b / "state"
    write(state / "fresh-initialization.json", asdict(intent))

    for filename in ("fresh-launch-claimed.json",):
        value = json.loads((state / filename).read_text())
        value["intent_sha256"] = hashlib.sha256((state / "fresh-initialization.json").read_bytes()).hexdigest()
        write(state / filename, value)
    case.prior.bootstrap_sha256 = digest
    case.mounts.extend(
        [
            {"Type": "bind", "Source": str(oldest), "Destination": inherited.journal_root, "RW": False},
            {"Type": "bind", "Source": str(oldest / "owner.lock"), "Destination": inherited.fence, "RW": False},
        ]
    )
    observed = tuple(sorted((row["Source"], row["Destination"], row["RW"]) for row in case.mounts))
    case.prior.mounts_sha256 = hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest()
    required = required_exclusions(case.prior)
    assert [item.instance_id for item in required] == ["manager-a", "manager-oldest"]
    assert required[1].journal_root == "/management-excluded/1/journal"
    with pytest.raises(ValueError, match="omit"):
        validate_successor(case.prior, case.intent, case.bootstrap)
    case.config["archived_exclusions"] = [asdict(item) for item in required]
    write(case.bootstrap, case.config)
    successor = replace(case.intent, bootstrap_config_sha256=hashlib.sha256(case.bootstrap.read_bytes()).hexdigest())
    assert validate_successor(case.prior, successor, case.bootstrap) == required


@pytest.mark.parametrize("fault", ["stopped", "pid", "restart", "owner_absent"])
def test_session_requires_captured_live_owner_before_claim(host_case, monkeypatch, fault):
    from auto_coder.management import exclusion_cutover as command

    case = host_case
    monkeypatch.setattr(command, "_evidence", lambda _: (case.prior, None, case.intent.prior_evidence_sha256))
    monkeypatch.setattr(command, "assert_retained_oauth_mount", lambda *args: None)
    monkeypatch.setattr(command, "assert_prior_network_pin", lambda *args: None)
    observed = list(case.values)
    if fault == "stopped":
        observed[2] = False
    elif fault == "pid":
        observed[3] += 1
    elif fault == "restart":
        observed[14] += 1
    monkeypatch.setattr(command, "_inspect", lambda _: observed)
    with pytest.raises((ValueError, command.HostObservationUnavailable)):
        command.hold_claim_and_guard(case.intent, case.c / "evidence", case.bootstrap, case.b / "state" / ".supervisor-owner.lock", 123, case.c / "guard")
    assert not (case.c / "state" / "fresh-initialization.json").exists()


@pytest.mark.parametrize("split", [False, True])
def test_fresh_command_requires_successful_sealed_provenance(host_case, split):
    from auto_coder.management.fresh_host_observation import _supported_prior_command

    case = host_case
    entry = ["python"] if split else ["python", "-m", "auto_coder.management.bootstrap_supervisor"]
    arguments = (["-m", "auto_coder.management.bootstrap_supervisor"] if split else []) + ["--config", "/bootstrap/config.json"]
    assert _supported_prior_command(entry, arguments)
    fresh = [*arguments, "--fresh-initialization"]
    assert not _supported_prior_command(entry, fresh)
    _, support = inherited_support(case.mounts)
    assert _supported_prior_command(entry, fresh, initialized_fresh="/management-initialization" in support)
    for invalid in ([*fresh, "--unexpected"], [*fresh, "--fresh-initialization"], [*arguments, "--config", "/other.json"]):
        assert not _supported_prior_command(entry, invalid, initialized_fresh=True)
    outcome = case.b / "state" / "fresh-outcome.json"
    original = outcome.read_bytes()
    data = json.loads(original)
    data["status"] = "unknown"
    write(outcome, data)
    with pytest.raises(ValueError, match="unresolved"):
        inherited_support(case.mounts)
    assert not _supported_prior_command(entry, fresh)
