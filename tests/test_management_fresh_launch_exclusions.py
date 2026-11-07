"""First-launch component boundary with real journals, claims and kernel locks.

Only the container path/mount namespace, host grant transport and physical child
launch are controlled. No real installed-runtime or Docker execution is claimed.
"""

import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace as N

import pytest
import yaml

import auto_coder
from auto_coder.management import bootstrap_supervisor, fresh_initialization, retained_startup
from auto_coder.management.archived_exclusions import acquire_exclusion_fences
from auto_coder.management.bootstrap_store import ChildPhase, ChildRecord
from auto_coder.management.bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor, Observation, ResultStatus
from auto_coder.management.fresh_initialization import application_tree_sha256, claim_fresh_initialization, read_fresh_status
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.oauth_store import OAuthStore
from tests.test_management_archived_exclusions import binding, generation, reader
from tests.test_management_fresh_initialization import _intent


@pytest.fixture
def first_launch_case(tmp_path: Path, monkeypatch, request):
    legacy = getattr(request, "param", None) == "legacy"
    intent = _intent(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    generation(a, "manager-a", management=True)
    _, _, direct_lock = generation(b, "manager-b", management=True)
    archive = binding(a, "manager-a")
    state, operations, oauth, guard = (tmp_path / name for name in ("state", "journal", "oauth", "guard"))
    config_path, management = tmp_path / "bootstrap.json", tmp_path / "management.yaml"
    executable = tmp_path / "auto-coder"
    executable.write_text(
        "#!/opt/auto-coder/.venv/bin/python\n"
        "import sys\nfrom auto_coder.cli import main\n"
        'if __name__ == "__main__":\n'
        '    if sys.argv[0].endswith("-script.pyw"):\n'
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        '    elif sys.argv[0].endswith(".exe"):\n'
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        "    sys.exit(main())\n"
    )
    executable.chmod(0o700)
    management.write_text(
        yaml.safe_dump(
            dict(
                state_root=str(oauth),
                operation_state_root=str(operations),
                bootstrap_config_path=str(config_path),
                public_origin="http://127.0.0.1:8081",
                firebase_project_id="fixture",
                clients=[dict(client_id="fixture-client", redirect_uris=["http://127.0.0.1:8081/callback"])],
                bind_host="127.0.0.1",
                port=8081,
                local_development=True,
            )
        )
    )
    OAuthStore(load_management_settings(management).oauth)
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    config_path.write_text(
        json.dumps(
            dict(
                instance_id=intent.new_instance,
                state_root=str(state),
                journal_root=str(operations),
                oauth_root=str(oauth),
                management_config=str(management),
                management_version_root=str(tmp_path / "versions"),
                listener_url="http://127.0.0.1:8081/mcp",
                initial_runtime="bootstrap",
                runtimes=[dict(reference="bootstrap", receipt="sha256:" + executable_hash, executable=str(executable), arguments=["management", "serve", "--config", str(management)])],
                fresh_guard_path=str(guard / "fresh-guard.sock"),
                fresh_prior_lock_path=str(direct_lock),
                archived_exclusions=[] if legacy else [asdict(archive)],
            )
        )
    )
    lock = direct_lock.stat()
    database = (b / "management-operations.sqlite3").stat()
    intent = replace(
        intent,
        old_instance="manager-b",
        state_root=str(state),
        journal_root=str(operations),
        oauth_root=str(oauth),
        owner_lock_device=lock.st_dev,
        owner_lock_inode=lock.st_ino,
        bootstrap_config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        management_config_sha256=hashlib.sha256(management.read_bytes()).hexdigest(),
        executable_sha256=executable_hash,
        application_sha256=application_tree_sha256(Path(auto_coder.__file__).resolve().parent),
        host_prior_journal_root="" if legacy else str(b),
        prior_journal_root="" if legacy else "/management-prior-operations",
        prior_journal_device=0 if legacy else database.st_dev,
        prior_journal_inode=0 if legacy else database.st_ino,
    )
    claim_fresh_initialization(state, intent)
    fixed = {
        "/management-initialization/fresh-guard.sock": guard / "fresh-guard.sock",
        "/management-prior-owner.lock": direct_lock,
        "/management-prior-operations": b,
        archive.journal_root: a,
        archive.fence: a / "owner.lock",
        "/opt/auto-coder/.venv/bin/auto-coder": executable,
    }

    def container_path(value):
        return fixed.get(str(value), Path(value))

    monkeypatch.setattr(bootstrap_supervisor, "Path", container_path)
    monkeypatch.setattr(retained_startup, "Path", container_path)
    monkeypatch.setattr(bootstrap_supervisor, "acquire_exclusion_fences", lambda bindings: acquire_exclusion_fences(bindings, host=True))
    statvfs = os.statvfs
    monkeypatch.setattr(retained_startup.os, "statvfs", lambda p: N(f_flag=os.ST_RDONLY) if Path(p) in {a, b} else statvfs(p))
    grants = []

    def host_grant(_socket, **request):
        # Positive pre-grant evidence exists. Only the transport is substituted.
        with reader(b, "manager-b", archives=(reader(a, "manager-a"),)) as evidence:
            evidence.read()
        request.pop("trusted_uid", None)
        grants.append(request)
        return {**request, "challenge": "c" * 64}

    monkeypatch.setattr(fresh_initialization, "request_fresh_host_grant", host_grant)
    listener_checks = []
    monkeypatch.setattr("auto_coder.management.fresh_host_observation.assert_host_listener_absent", lambda port: listener_checks.append(port))
    supervisor = ManagementBootstrapSupervisor(BootstrapConfig.read(config_path), bootstrap_config_path=config_path, fresh_initialization=True)
    # run() normally holds this exclusive lock before entering first launch.
    own = (state / ".supervisor-owner.lock").open("w")
    fcntl.flock(own, fcntl.LOCK_EX | fcntl.LOCK_NB)
    case = N(a=a, b=b, state=state, operations=operations, oauth=oauth, intent=intent, supervisor=supervisor, grants=grants, listener_checks=listener_checks, paths=(config_path, management, executable, state / "fresh-initialization.json", oauth / "oauth.sqlite3"))
    try:
        yield case
    finally:
        supervisor._release_prior_fences()
        own.close()


@pytest.mark.parametrize("fault", ["valid", "archive-missing", "archive-corrupt", "archive-history", "archive-active", "archive-database-identity", "archive-fence-identity", "direct-missing", "direct-corrupt", "direct-database-identity", "direct-fence-identity", "reader-unavailable"])
def test_first_launch_revalidates_real_evidence_after_durable_host_grant(first_launch_case, monkeypatch, fault):
    case = first_launch_case
    original_claim = fresh_initialization.request_and_claim_first_launch
    preserved = {path: path.read_bytes() for path in case.paths}
    fault_injected = []
    launches = []
    prior_files = {}

    def grant_then_fault(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        assert (case.state / "fresh-launch-claimed.json").is_file()
        fault_injected.append(True)
        if fault == "reader-unavailable":
            monkeypatch.setattr(retained_startup, "retained_from_fresh_intent", lambda *a, **k: None)
        elif fault != "valid":
            root = case.a if fault.startswith("archive-") else case.b
            database = root / "management-operations.sqlite3"
            if fault.endswith("missing"):
                database.rename(root / "preserved-original.sqlite3")
            elif fault.endswith("database-identity"):
                database.rename(root / "preserved-original.sqlite3")
                database.write_bytes((root / "preserved-original.sqlite3").read_bytes())
            elif fault.endswith("fence-identity"):
                (root / "owner.lock").rename(root / "preserved-owner.lock")
                (root / "owner.lock").touch(mode=0o600)
            elif fault.endswith("active"):
                generation(root, "manager-a-extra", active=True, management=True)
            else:
                with closing(sqlite3.connect(database)) as connection:
                    with connection:
                        connection.execute("DELETE FROM operation_history" if fault.endswith("history") else "UPDATE metadata SET value='unsupported' WHERE key='schema_version'")
                    assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        prior_files.update({path: hashlib.sha256(path.read_bytes()).hexdigest() for root in (case.a, case.b) for path in root.glob("*.sqlite3")})
        return result

    def launch(*args):
        launches.append(args)
        if fault == "valid":
            for path in (case.a / "owner.lock", case.b / "owner.lock"):
                with path.open("r") as contender:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return Observation(ResultStatus.OBSERVED_RUNNING, ChildRecord(case.intent.new_instance, ChildPhase.RUNNING, "bootstrap", pid=123, start_token="456", child_generation=1))

    monkeypatch.setattr(fresh_initialization, "request_and_claim_first_launch", grant_then_fault)
    monkeypatch.setattr(case.supervisor, "start_selected", launch)
    assert case.supervisor._start_fresh_initialization() is (fault == "valid")
    assert len(case.grants) == 1 and fault_injected == [True]
    assert len(launches) == (1 if fault == "valid" else 0)
    assert read_fresh_status(case.state)["status"] == ("launched" if fault == "valid" else "unknown")
    assert {path: path.read_bytes() for path in case.paths} == preserved
    claim = (case.state / "fresh-launch-claimed.json").read_bytes()
    reconstructed = ManagementBootstrapSupervisor(case.supervisor.config, bootstrap_config_path=case.supervisor.bootstrap_config_path, fresh_initialization=True)
    assert not reconstructed._start_fresh_initialization()
    assert len(case.grants) == 1 and len(launches) == (1 if fault == "valid" else 0)
    assert (case.state / "fresh-launch-claimed.json").read_bytes() == claim
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for root in (case.a, case.b) for path in root.glob("*.sqlite3")} == prior_files
    for root in (case.a, case.b):
        path = root / "preserved-owner.lock" if (root / "preserved-owner.lock").exists() else root / "owner.lock"
        with path.open("r") as contender:
            if fault == "valid":
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if fault != "valid":
        assert case.supervisor._archived_owner_fences == () and case.supervisor._prior_owner_fence is None


@pytest.mark.parametrize("failure", ["unknown-start", "start-error", "launch-record-error"])
def test_possible_first_child_retains_all_fences_and_never_replays(first_launch_case, monkeypatch, failure):
    case = first_launch_case
    launches = []

    def launch(*args):
        launches.append(args)
        if failure == "start-error":
            raise OSError("physical adapter may have launched before losing acknowledgement")
        child = ChildRecord(case.intent.new_instance, ChildPhase.RUNNING, "bootstrap", pid=123, start_token="456", child_generation=1)
        return Observation(ResultStatus.UNKNOWN if failure == "unknown-start" else ResultStatus.OBSERVED_RUNNING, child)

    def lose_record(*_args, **_kwargs):
        raise OSError("launch persistence acknowledgement unavailable")

    monkeypatch.setattr(case.supervisor, "start_selected", launch)
    if failure == "launch-record-error":
        monkeypatch.setattr(fresh_initialization, "record_fresh_launch", lose_record)
    preserved = {path: path.read_bytes() for path in case.paths}
    assert not case.supervisor._start_fresh_initialization()
    assert len(launches) == len(case.grants) == 1
    assert case.supervisor._prior_owner_fence is not None and case.supervisor._archived_owner_fences
    claim = (case.state / "fresh-launch-claimed.json").read_bytes()
    reconstructed = ManagementBootstrapSupervisor(case.supervisor.config, bootstrap_config_path=case.supervisor.bootstrap_config_path, fresh_initialization=True)
    assert not reconstructed._start_fresh_initialization()
    assert len(launches) == len(case.grants) == 1
    assert read_fresh_status(case.state)["status"] == "unknown"
    assert (case.state / "fresh-launch-claimed.json").read_bytes() == claim
    for path in (case.a / "owner.lock", case.b / "owner.lock"):
        with path.open("r") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert {path: path.read_bytes() for path in case.paths} == preserved


@pytest.mark.parametrize("first_launch_case", ["legacy"], indirect=True)
def test_legacy_first_launch_without_retained_journal_keeps_original_contract(first_launch_case, monkeypatch):
    case = first_launch_case
    launches = []

    def unexpected_reader(*_args, **_kwargs):
        pytest.fail("legacy deployment has no retained-journal contract")

    def launch(*args):
        launches.append(args)
        return Observation(ResultStatus.OBSERVED_RUNNING, ChildRecord(case.intent.new_instance, ChildPhase.RUNNING, "bootstrap", pid=123, start_token="456", child_generation=1))

    monkeypatch.setattr(retained_startup, "retained_from_fresh_intent", unexpected_reader)
    monkeypatch.setattr(case.supervisor, "start_selected", launch)
    assert case.supervisor._start_fresh_initialization()
    assert len(launches) == len(case.grants) == 1
    assert read_fresh_status(case.state)["status"] == "launched"
