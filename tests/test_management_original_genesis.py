"""Original genesis filesystem and native-store components.

Docker observations and container bind mapping are explicit component substitutes.
These tests do not claim real-container, first-child or recovery-epoch acceptance.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management import original_genesis as producer
from auto_coder.management.bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor
from auto_coder.management.containment_observation import DockerObservation
from auto_coder.management.fresh_initialization import claim_fresh_initialization
from auto_coder.management.original_attempt_evidence import derive_lineage, observe, verify_lineage
from auto_coder.management.original_attempts import OriginalEvidenceUnavailable, canonical, read_frontier, source_identity
from tests.test_management_original_attempts import _covered, _fingerprint


@pytest.fixture
def _use_real_commands():
    pass


@pytest.fixture
def _use_real_sleep():
    pass


def _write(path, value):
    path.write_bytes(canonical(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def root_authorization(tmp_path, monkeypatch):
    # Only the host privilege capability is substituted in this same-UID
    # component. Every original file, inode, exclusive create and fsync is real.
    monkeypatch.setattr(producer, "_require_host_root", lambda: None)
    tmp_path.chmod(0o700)
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir(mode=0o700)
    runtime = {"reference": "bootstrap", "receipt": "sha256:" + "a" * 64, "executable": "/opt/auto-coder/.venv/bin/auto-coder", "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"]}
    _write(
        bootstrap / "config.json",
        {
            "instance_id": "genesis-instance",
            "state_root": "/management-child-state",
            "journal_root": "/management-operations",
            "oauth_root": "/management-oauth",
            "listener_url": "http://127.0.0.1:8081/mcp",
            "initial_runtime": "bootstrap",
            "runtimes": [runtime],
            "original_genesis": "/bootstrap/original-genesis.json",
        },
    )
    _write(
        bootstrap / "management.yaml",
        {
            "state_root": "/management-oauth",
            "operation_state_root": "/management-operations",
            "bootstrap_config_path": "/bootstrap/config.json",
            "public_origin": "https://management.example.test",
            "firebase_project_id": "component-project",
            "clients": [{"client_id": "component-client", "redirect_uris": ["https://client.example.test/callback"]}],
            "bind_host": "127.0.0.1",
            "port": 8081,
        },
    )
    proxy = _write(tmp_path / "Caddyfile", {"component": "protected-proxy-config"})
    request = producer.GenesisRootAuthorization(
        producer.ROOTS_PROFILE, "separately-authorized-new-deployment", "component-deployment", str(uuid.uuid4()), str(bootstrap / "config.json"), str(bootstrap / "management.yaml"), str(proxy), *(str(tmp_path / role) for role in ("state", "journal", "oauth", "history"))
    )
    path = _write(tmp_path / "root-authorization.json", asdict(request))
    return request, path


def _capture_component(root_authorization, monkeypatch, tmp_path):
    request, authorization = root_authorization
    roots_path = producer.prepare_original_genesis_roots(authorization)
    selection = producer.GenesisSelection(producer.PROFILE, request.purpose, request.deployment_id, request.authorization_id, "a" * 64, "b" * 64, request.history_root, request.bootstrap_path, request.management_path, request.proxy_config_path, str(roots_path))
    final_authorization = _write(tmp_path / "capture-authorization.json", asdict(selection))
    manager = DockerObservation("component-engine", selection.management_id, "sha256:" + "c" * 64, "d" * 64, "no", 0, False, "created", 0, "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", 0, "e" * 64)
    proxy = replace(manager, container_id=selection.proxy_id, image_id="sha256:" + "f" * 64)
    mounts = [{"Type": "bind", "Source": str(Path(request.bootstrap_path).parent), "Destination": "/bootstrap", "RW": False}]
    mounts += [
        {"Type": "bind", "Source": getattr(request, role + "_root"), "Destination": destination, "RW": role != "history"}
        for role, destination in (("state", "/management-child-state"), ("journal", "/management-operations"), ("oauth", "/management-oauth"), ("history", "/management-original-history"))
    ]
    monkeypatch.setattr(producer, "_created_pair", lambda _selection: ((manager, proxy), {"Mounts": mounts}, source_identity()))
    anchor = producer.capture_original_genesis(final_authorization)
    return anchor, final_authorization


@pytest.fixture
def captured(root_authorization, monkeypatch, tmp_path):
    return _capture_component(root_authorization, monkeypatch, tmp_path)


def _native_supervisor(anchor, monkeypatch):
    """Map the tested container-bind boundary onto real isolated local roots."""
    from auto_coder.management import oauth_config

    mapped = replace(anchor, roots=tuple(replace(root, container_path=root.host_path) for root in anchor.roots))
    original_file = producer._file
    original_settings = oauth_config.load_management_settings
    original_history = producer.validate_history_genesis
    bootstrap = Path(anchor.file("bootstrap").path).parent

    def mapped_file(role, path):
        if path.parent == Path("/bootstrap"):
            path = bootstrap / path.name
        return original_file(role, path)

    def mapped_history(root, *, mounted_genesis=None):
        if mounted_genesis is not None:
            return mapped
        with monkeypatch.context() as current:
            current.setattr(producer, "HISTORY_MOUNT", Path("/management-original-history"))
            return original_history(root)

    def mapped_settings(path):
        settings = original_settings(path)
        oauth_root = Path(anchor.root("oauth").host_path)
        return replace(settings, state_root=oauth_root, oauth=replace(settings.oauth, database_path=oauth_root / "oauth.sqlite3"), operation_state_root=Path(anchor.root("journal").host_path), bootstrap_config_path=bootstrap / "config.json")

    monkeypatch.setattr(oauth_config, "load_management_settings", mapped_settings)
    monkeypatch.setattr(producer, "_file", mapped_file)
    monkeypatch.setattr(producer, "HISTORY_MOUNT", Path(anchor.binding.root))
    monkeypatch.setattr(producer, "validate_history_genesis", mapped_history)
    config = BootstrapConfig.read(bootstrap / "config.json")
    # Map only the executable pathname; the actual receipt and wrapper parser
    # still verify this real console file before any native store is created.
    launcher = bootstrap / "component-console.py"
    launcher.write_text(
        "#!/opt/auto-coder/.venv/bin/python\n"
        "import sys\n"
        "from auto_coder.cli import main\n"
        "if __name__ == '__main__':\n"
        "    if sys.argv[0].endswith('-script.pyw'):\n"
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        "    elif sys.argv[0].endswith('.exe'):\n"
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        "    sys.exit(main())\n"
    )
    runtime = replace(config.runtimes[0], executable=launcher, receipt="sha256:" + hashlib.sha256(launcher.read_bytes()).hexdigest())
    config = replace(config, state_root=Path(anchor.root("state").host_path), journal_root=Path(anchor.root("journal").host_path), oauth_root=Path(anchor.root("oauth").host_path), runtimes=(runtime,))
    return ManagementBootstrapSupervisor(config, bootstrap_config_path=bootstrap / "config.json")


def _native_component(anchor, monkeypatch):
    supervisor = _native_supervisor(anchor, monkeypatch)
    with supervisor._owner_file() as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = producer.prepare_original_genesis(supervisor, supervisor._await_owner_start_token())
        assert result is not None
        assert producer.prepare_original_genesis(supervisor, supervisor._await_owner_start_token()) is None
    # The native creators use WAL. This component explicitly settles its own
    # stopped test stores before an immutable reader; the verifier never writes.
    for path in producer.native_paths(anchor, host=True).values():
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return supervisor, result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state_root", "/wrong-oauth"),
        ("operation_state_root", "/wrong-operations"),
        ("operation_state_root", None),
        ("bootstrap_config_path", "/wrong-bootstrap/config.json"),
        ("bootstrap_config_path", None),
        ("bind_host", "0.0.0.0"),
        ("port", 8082),
    ],
)
def test_genesis_capture_refuses_service_using_unrecorded_stores_or_listener(root_authorization, monkeypatch, tmp_path, field, value):
    request, _ = root_authorization
    management = Path(request.management_path)
    settings = json.loads(management.read_bytes())
    settings[field] = value
    _write(management, settings)
    with pytest.raises(OriginalEvidenceUnavailable, match="effective management"):
        _capture_component(root_authorization, monkeypatch, tmp_path)
    assert not (management.parent / producer.GENESIS_NAME).exists()
    assert all(list(Path(getattr(request, role + "_root")).iterdir()) == [] for role in ("state", "journal", "oauth", "history"))


@pytest.mark.parametrize("change", ["receipt", "python-hook", "loader-hook"])
def test_native_genesis_refuses_unbound_execution_before_original_store_creation(captured, monkeypatch, change):
    anchor, _ = captured
    supervisor = _native_supervisor(anchor, monkeypatch)
    runtime = supervisor.config.runtimes[0]
    if change == "receipt":
        runtime = replace(runtime, receipt="sha256:" + "f" * 64)
    else:
        runtime = replace(runtime, environment=(("PYTHONPATH" if change == "python-hook" else "LD_PRELOAD", "/unselected"),))
    supervisor.config = replace(supervisor.config, runtimes=(runtime,))
    with supervisor._owner_file() as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="console receipt differs|environment contains code hooks"):
            producer.prepare_original_genesis(supervisor, supervisor._await_owner_start_token())
    assert {path.name for path in supervisor.config.state_root.iterdir()} == {".supervisor-owner.lock"}
    assert list(supervisor.config.journal_root.iterdir()) == []
    assert list(supervisor.config.oauth_root.iterdir()) == []


def test_original_roots_are_created_not_inferred_from_emptiness(root_authorization, tmp_path):
    request, authorization = root_authorization
    Path(request.state_root).mkdir(mode=0o700)
    before = _fingerprint(tmp_path)
    with pytest.raises(OriginalEvidenceUnavailable, match="no empty-root conversion"):
        producer.prepare_original_genesis_roots(authorization)
    assert _fingerprint(tmp_path) == before
    assert not (Path(request.bootstrap_path).parent / producer.ROOT_CLAIM_NAME).exists()
    assert not Path(request.history_root).exists()


@pytest.mark.parametrize("boundary", [producer.ROOT_CLAIM_NAME, producer.ROOTS_NAME])
def test_interrupted_original_root_publication_is_never_reenrolled(root_authorization, tmp_path, monkeypatch, boundary):
    request, authorization = root_authorization
    original_create = producer._create

    def interrupted(root, name, value):
        original_create(root, name, value)
        if name == boundary:
            raise OSError("interrupted original root publication")

    monkeypatch.setattr(producer, "_create", interrupted)
    with pytest.raises(OSError, match="interrupted original"):
        producer.prepare_original_genesis_roots(authorization)
    monkeypatch.setattr(producer, "_create", original_create)
    before = _fingerprint(tmp_path)
    with pytest.raises((FileExistsError, OriginalEvidenceUnavailable)):
        producer.prepare_original_genesis_roots(authorization)
    assert _fingerprint(tmp_path) == before


def test_captured_genesis_pins_affirmative_roots_exact_empty_history_and_configs(captured, tmp_path):
    anchor, authorization = captured
    authority, events = read_frontier(Path(anchor.binding.root), anchor.binding)
    assert authority.profile == producer.HISTORY_PROFILE
    assert authority.genesis_path == str(Path(anchor.file("bootstrap").path).parent / producer.GENESIS_NAME)
    assert events == []
    assert producer.validate_history_genesis(Path(anchor.binding.root)) == anchor
    assert {root.role for root in anchor.roots} == {"bootstrap", "state", "journal", "oauth", "history"}
    assert {item.role for item in anchor.files} == {"authorization", "root-authorization", "root-claim", "roots", "bootstrap", "management", "proxy"}
    before = _fingerprint(tmp_path)
    with pytest.raises(OriginalEvidenceUnavailable, match="must be empty|already captured"):
        producer.capture_original_genesis(authorization)
    assert _fingerprint(tmp_path) == before
    for role in ("state", "journal", "oauth"):
        assert list(Path(anchor.root(role).host_path).iterdir()) == []


@pytest.mark.parametrize("role", ["bootstrap", "management", "proxy", "root-authorization", "root-claim", "roots", "authorization"])
def test_genesis_refuses_changed_original_file_without_repair(captured, tmp_path, role):
    anchor, _ = captured
    original = Path(anchor.file(role).path)
    original.write_bytes(original.read_bytes() + b" ")
    before = _fingerprint(tmp_path)
    with pytest.raises((OriginalEvidenceUnavailable, ValueError)):
        read_frontier(Path(anchor.binding.root), anchor.binding)
    assert _fingerprint(tmp_path) == before


def test_genesis_first_successor_cannot_select_unrelated_baseline(captured, tmp_path):
    anchor, _ = captured
    intent = _covered(tmp_path, anchor.binding, "foreign-child")
    before = _fingerprint(tmp_path)
    with pytest.raises(OriginalEvidenceUnavailable, match="uncovered, refused or cyclic"):
        claim_fresh_initialization(Path(intent.host_state_root), intent)
    assert _fingerprint(tmp_path) == before
    assert read_frontier(Path(anchor.binding.root), anchor.binding)[1] == []


def test_native_genesis_unknown_is_complete_evidence_but_missing_store_is_not(captured, tmp_path, monkeypatch):
    anchor, _ = captured
    _, launch = _native_component(anchor, monkeypatch)
    native, first = producer.validate_native_genesis(anchor, Path(anchor.file("bootstrap").path).parent / producer.GENESIS_NAME, host=True)
    assert (first["operation_id"], first["attempt_id"]) == launch
    assert set(native["stores"]) == {"child", "handoff", "recovery", "operations"}
    owner_path = Path(anchor.root("state").host_path) / ".supervisor-owner.lock"
    with owner_path.open("rb") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        with observe(anchor.binding) as history:
            evidence = derive_lineage(anchor.binding, history, (owner.fileno(),))
            assert evidence.unavailable == ()
            assert len(evidence.generations) == 1
            assert evidence.generations[0].outcome == "unknown"
            assert verify_lineage(evidence, anchor.binding, history, (owner.fileno(),)) == evidence
            before = _fingerprint(tmp_path)
            with pytest.raises(OriginalEvidenceUnavailable, match="forged, stale, truncated"):
                verify_lineage(replace(evidence, generations=()), anchor.binding, history, (owner.fileno(),))
            assert _fingerprint(tmp_path) == before
            database = Path(anchor.root("journal").host_path) / "management-operations.sqlite3"
            hidden = database.with_name("removed-original")
            database.rename(hidden)
            missing = derive_lineage(anchor.binding, history, (owner.fileno(),))
            assert missing.unavailable and any("management-operations.sqlite3" in reason for reason in missing.unavailable)
            assert hidden.read_bytes() == before[str(database.relative_to(tmp_path))][2]


def test_genesis_full_frontier_retains_original_failed_branch(captured, tmp_path, monkeypatch):
    anchor, _ = captured
    _native_component(anchor, monkeypatch)
    intent = _covered(tmp_path, anchor.binding, "refused-successor")
    intent = replace(intent, old_instance=anchor.instance_id, old_container_id=anchor.management_id, oauth_inode=intent.oauth_inode + 1)
    with pytest.raises(ValueError, match="retained OAuth root identity differs"):
        claim_fresh_initialization(Path(intent.host_state_root), intent)
    owner_path = Path(anchor.root("state").host_path) / ".supervisor-owner.lock"
    with owner_path.open("rb") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        with observe(anchor.binding) as history:
            evidence = derive_lineage(anchor.binding, history, (owner.fileno(),))
            assert evidence.unavailable == ()
            assert [(item.instance_id, item.outcome, item.owner_inode > 0) for item in evidence.generations] == [(anchor.instance_id, "unknown", True), (intent.new_instance, "never-created", False)]
            assert evidence.attempts[0].request_id == intent.request_id
            assert evidence.attempts[0].state == "never-created"
            assert verify_lineage(evidence, anchor.binding, history, (owner.fileno(),)) == evidence


@pytest.mark.parametrize("missing", [producer.NATIVE_NAME, producer.LAUNCH_NAME])
def test_interrupted_genesis_native_boundary_cannot_reopen_first_owner(captured, monkeypatch, tmp_path, missing):
    anchor, _ = captured
    supervisor, _ = _native_component(anchor, monkeypatch)
    path = supervisor.config.state_root / missing
    path.rename(path.with_suffix(".missing"))
    before = _fingerprint(tmp_path)
    with pytest.raises(FileExistsError):
        with supervisor._owner_file():
            pytest.fail("an incomplete native boundary reopened the original owner")
    assert _fingerprint(tmp_path) == before


def test_original_genesis_selector_does_not_enable_fresh_or_new(captured):
    anchor, _ = captured
    config = BootstrapConfig.read(Path(anchor.file("bootstrap").path))
    for changed in (replace(config, new_instance_initialization=True), replace(config, fresh_guard_path=Path("/guard"), fresh_prior_lock_path=Path("/prior"))):
        with pytest.raises(ValueError, match="explicitly selected ordinary"):
            changed.validate()
    assert replace(config, original_genesis=None).original_genesis is None


def test_complete_unknown_successor_is_distinct_from_missing_original_claim(captured, tmp_path, monkeypatch):
    """Actual original writers/stores; only the host grant transport is substituted."""
    from auto_coder.management import fresh_initialization
    from auto_coder.management.bootstrap_store import BootstrapStore
    from auto_coder.management.operation_store import ManagementOperationStore
    from auto_coder.management.original_attempts import grant_boundary, record_original_stores
    from auto_coder.management.recovery_mailbox import RecoveryMailbox
    from auto_coder.management.self_update_handoff import HandoffStore

    anchor, _ = captured
    _native_component(anchor, monkeypatch)
    initial = _covered(tmp_path, anchor.binding, "unknown-successor")
    prior_owner_path = Path(anchor.root("state").host_path) / ".supervisor-owner.lock"
    prior_info = prior_owner_path.stat()
    oauth = anchor.root("oauth")
    intent = replace(
        initial,
        old_instance=anchor.instance_id,
        old_container_id=anchor.management_id,
        state_root=initial.host_state_root,
        journal_root=initial.host_journal_root,
        oauth_root=oauth.host_path,
        host_oauth_root=oauth.host_path,
        oauth_device=oauth.device,
        oauth_inode=oauth.inode,
        owner_lock_device=prior_info.st_dev,
        owner_lock_inode=prior_info.st_ino,
    )
    state, journal = Path(intent.host_state_root), Path(intent.host_journal_root)
    claim_fresh_initialization(state, intent)

    def transport(_socket, **request):
        request.pop("trusted_uid", None)
        return {**request, "challenge": "a" * 64}

    monkeypatch.setattr(fresh_initialization, "request_fresh_host_grant", transport)
    # This is the production original-admission boundary, not an assertion that
    # a real host grant or child was delivered in this component test.
    with grant_boundary(intent):
        fresh_initialization.request_and_claim_first_launch(state, tmp_path / "unused.sock", owner_incarnation=str(uuid.uuid4()))
    owner_path = state / ".supervisor-owner.lock"
    owner_path.touch(mode=0o600)
    with owner_path.open("rb") as native_owner:
        fcntl.flock(native_owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous_umask = os.umask(0o077)
        try:
            with closing(BootstrapStore(state, intent.new_instance)._connect()):
                pass
            with closing(HandoffStore(state, intent.new_instance)._connect()):
                pass
            with closing(ManagementOperationStore(journal)._connect()):
                pass
            RecoveryMailbox(state, intent.new_instance).initialize()
            record_original_stores(state, intent)
        finally:
            os.umask(previous_umask)
    for database in (*state.glob("*.sqlite3"), *journal.glob("*.sqlite3")):
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with prior_owner_path.open("rb") as prior_owner, owner_path.open("rb") as owner:
        for stream in (prior_owner, owner):
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        fences = (prior_owner.fileno(), owner.fileno())
        with observe(anchor.binding) as history:
            evidence = derive_lineage(anchor.binding, history, fences)
            assert evidence.unavailable == ()
            assert [(item.instance_id, item.outcome) for item in evidence.generations] == [(anchor.instance_id, "unknown"), (intent.new_instance, "unknown")]
            assert verify_lineage(evidence, anchor.binding, history, fences) == evidence
            original = _fingerprint(tmp_path)
            claim = state / "fresh-launch-claimed.json"
            claim.rename(state / "removed-original-claim")
            missing = derive_lineage(anchor.binding, history, fences)
            assert missing.unavailable and any("fresh-launch-claimed.json" in reason for reason in missing.unavailable)
            assert (state / "removed-original-claim").read_bytes() == original[str(claim.relative_to(tmp_path))][2]
            with pytest.raises(OriginalEvidenceUnavailable):
                verify_lineage(missing, anchor.binding, history, fences)
