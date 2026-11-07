"""Real retained DBs/fences at physical launch boundaries.

Container paths and read-only mounts use the first-launch component fixture.
The Popen observer launches a synthetic waiting child instead of application
code. Recovery mailbox admission/config ports are controlled; its launch method
and durable store are real. These are not container or installed-READY tests.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace as N

import pytest

from auto_coder.management import bootstrap_supervisor, recovery_supervisor
from auto_coder.management.bootstrap_store import ChildPhase, ChildRecord
from auto_coder.management.bootstrap_supervisor import ResultStatus
from auto_coder.management.recovery_mailbox import RecoveryIntent
from tests.test_management_archived_exclusions import generation
from tests.test_management_fresh_launch_exclusions import first_launch_case  # noqa: F401
from tests.test_management_self_update_policy import boundary, production_boundary  # noqa: F401


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
@pytest.mark.parametrize("boundary", ["start-selected", "restore-once", "restore-stopped", "local-recovery"])
@pytest.mark.parametrize("fault", ["valid", "missing", "corrupt", "incomplete", "active", "archive-sh-lost", "direct-sh-lost", "owner-ex-lost"])
def test_every_physical_launch_requires_live_exclusion_evidence(first_launch_case, monkeypatch, boundary, fault):
    case = first_launch_case
    supervisor = case.supervisor
    supervisor._acquire_prior_fences(case.intent)
    supervisor.store.publish_supervisor(supervisor.incarnation, os.getpid(), supervisor._start_token(os.getpid()))
    runtime = supervisor._runtime("bootstrap")
    assert runtime is not None and runtime.verify(initial=True)
    original = ChildRecord(case.intent.new_instance, ChildPhase.STOPPED, "bootstrap", child_generation=1, supervisor_incarnation=supervisor.incarnation, intended_stop=True, runtime_receipt=runtime.receipt)
    supervisor.store.save(original)
    if boundary == "restore-once":
        original = replace(original, phase=ChildPhase.NEEDS_ATTENTION, previous_runtime="bootstrap", selected_runtime="failed-candidate", intended_stop=False, effect_operation_id="effect", effect_attempt_id="attempt")
        supervisor.store.save(original)
        supervisor.store.begin_effect("effect", "attempt", "START_SELECTED", "failed-candidate", "")
    consumer = None
    intent = None
    if boundary == "local-recovery":
        consumer = recovery_supervisor.RecoveryConsumer(supervisor, supervisor.bootstrap_config_path)
        config = supervisor.config.management_config
        intent = RecoveryIntent(str(uuid.uuid4()), case.intent.new_instance, supervisor.incarnation, 1, "bootstrap", runtime.receipt, runtime.source_receipt, str(config), hashlib.sha256(config.read_bytes()).hexdigest())
        consumer.mailbox.submit(intent)
        assert consumer._set(intent.request_id, "pending", "claimed", "synthetic admitted local recovery")
        # Isolate the physical boundary from local request admission only.
        monkeypatch.setattr(consumer, "_settings", lambda *_: N())
    database = case.a / "management-operations.sqlite3"
    if fault == "missing":
        database.rename(case.a / "preserved-original.sqlite3")
    elif fault in {"corrupt", "incomplete"}:
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute("DELETE FROM operation_history" if fault == "incomplete" else "UPDATE metadata SET value='unsupported' WHERE key='schema_version'")
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
    elif fault == "active":
        generation(case.a, "manager-extra", active=True, management=True)
    elif fault == "archive-sh-lost":
        os.close(supervisor._archived_owner_fences[0])
        supervisor._archived_owner_fences = ()
    elif fault == "direct-sh-lost":
        os.close(supervisor._prior_owner_fence)
        supervisor._prior_owner_fence = None
    elif fault == "owner-ex-lost":
        case.owner.close()
    # Finish synthetic WAL publication before taking the non-mutation oracle.
    for root in (case.a, case.b):
        for path in root.glob("*.sqlite3"):
            with closing(sqlite3.connect(path)) as connection:
                assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
    files = {path: hashlib.sha256(path.read_bytes()).hexdigest() for root in (case.a, case.b) for path in root.glob("*.sqlite3")}
    sealed = {path: path.read_bytes() for path in case.paths}
    launches = []
    real_popen = subprocess.Popen

    def observed_popen(*_args, **_kwargs):
        child = real_popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"], close_fds=True)
        launches.append(child)
        return child

    port = N(Popen=observed_popen, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError)
    monkeypatch.setattr(bootstrap_supervisor, "subprocess", port)
    monkeypatch.setattr(recovery_supervisor, "subprocess", port)
    try:
        if boundary == "start-selected":
            result = supervisor.start_selected("effect", "attempt", "bootstrap", supervisor._identity(original))
        elif boundary == "restore-once":
            result = supervisor._restore_once("effect", original)
        elif boundary == "restore-stopped":
            result = supervisor.restore_stopped_original("effect", "attempt", supervisor._identity(original), "bootstrap")
        else:
            consumer._launch(intent, original, runtime)
            result = None
        assert len(launches) == (1 if fault == "valid" else 0)
        if fault != "valid":
            if result is not None:
                assert result.status in {ResultStatus.NEEDS_ATTENTION, ResultStatus.UNKNOWN}
            state = supervisor.store.load()
            assert not state.pid
            # Replaying the already claimed effect never supplies another send.
            if boundary == "restore-once":
                supervisor._restore_once("effect", state)
            elif boundary == "start-selected":
                supervisor.start_selected("effect", "attempt", "bootstrap", supervisor._identity(original))
            elif boundary == "restore-stopped":
                supervisor.restore_stopped_original("effect", "attempt", supervisor._identity(original), "bootstrap")
            else:
                assert consumer.mailbox.get(intent.request_id).status in {"unknown", "refused"}
            assert launches == []
        assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for root in (case.a, case.b) for path in root.glob("*.sqlite3")} == files
        assert {path: path.read_bytes() for path in case.paths} == sealed
    finally:
        for child in launches:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


@pytest.fixture
def isolated_launch_case(tmp_path, monkeypatch):
    root = tmp_path / "physical-fixture"
    root.mkdir(mode=0o700)
    yield from first_launch_case.__wrapped__(root, monkeypatch, N(param=None))


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
@pytest.mark.parametrize("fault", ["valid", "grant", "policy", "journal", "key"])
@pytest.mark.parametrize("callback_enabled", [True, False])
def test_last_reader_authority_controls_real_candidate_popen(isolated_launch_case, boundary, tmp_path, monkeypatch, fault, callback_enabled):
    """Real authority state and physical method, with independent unsafe control.

    The authority consumer's config/provenance port is separate from the sealed
    physical fixture. Neither bootstrap nor intent is rewritten to join them.
    Suppressing the trusted callback deliberately demonstrates the old gap.
    """
    from auto_coder.management import oauth_config, retained_startup
    from auto_coder.management.operation_store import ManagementOperationStore
    from auto_coder.management.operations import EffectReceipt, OperationPhase
    from auto_coder.management.self_update_handoff import SelfUpdateHandoffConsumer
    from tests.test_management_self_update_policy import INSTANCE, _accepted_record, _policy
    from tests.test_management_self_update_supervisor import _handoff

    case, supervisor = isolated_launch_case, isolated_launch_case.supervisor
    supervisor._acquire_prior_fences(case.intent)
    grants, _, policy, bootstrap, *_, identity = boundary
    current, operation, token, accepted = _accepted_record(boundary, tmp_path)
    sent = current.begin_effect(operation, 0, owner_attempt_id="manager", attempt_id="attempt", correlation_identity="correlation").record
    source = _handoff(operation)
    handoff = replace(
        source,
        expected_child=replace(source.expected_child, instance_id=INSTANCE),
        candidate=replace(source.candidate, instance_id=INSTANCE, source_sha="a" * 40),
        previous=replace(source.previous, instance_id=INSTANCE),
        policy_revision=accepted.policy_revision,
        effect_generation=sent.effect.owner_generation,
    )
    settings = N(bootstrap_config_path=bootstrap, operating_policy_path=policy, operation_state_root=tmp_path / "operations", state_root=grants.settings.database_path.parent, oauth=grants.settings)
    auth_path = tmp_path / "authority-port.yaml"
    config = N(instance_id=INSTANCE, state_root=tmp_path / "authority-consumer", journal_root=settings.operation_state_root, oauth_root=settings.state_root, management_config=auth_path, archived_exclusions=supervisor.config.archived_exclusions)
    consumer = SelfUpdateHandoffConsumer(N(config=config, bootstrap_config_path=bootstrap, source_authority=None))
    original_settings = oauth_config.load_management_settings
    monkeypatch.setattr(oauth_config, "load_management_settings", lambda path: settings if path == auth_path else original_settings(path))
    monkeypatch.setattr("auto_coder.management.identity.FirebaseOperatorIdentity", lambda _: identity)
    monkeypatch.setattr("auto_coder.management.oauth_grants.OAuthGrantService", lambda *_: grants)
    actual_reader = retained_startup.retained_from_fresh_intent
    scans, checks, launches = [], [], []
    sealed = {path: path.read_bytes() for path in case.paths}
    snapshots = {}

    def last_reader(*args, **kwargs):
        result = actual_reader(*args, **kwargs)
        scans.append(True)
        if len(scans) == 1:
            if fault == "grant":
                grants.revoke("client", token.refresh_token)
            elif fault == "policy":
                _policy(policy, enabled=False)
            elif fault == "journal":
                assert current.transition(operation, sent.revision, OperationPhase.NEEDS_ATTENTION, safe_error_category="synthetic-last-scan-change").record is not None
            elif fault == "key":
                prior = ManagementOperationStore(case.a)
                intent = replace(sent.intent, target=replace(sent.intent.target, instance_id="manager-a"))
                collision = prior.admit(intent, sent.authority).record
                assert collision is not None
                assert prior.transition(collision.operation_id, 0, OperationPhase.FAILED, evidence=EffectReceipt(collision.operation_id, "synthetic-no-effect", 1, "no_effect_sent")).record is not None
            for root in (case.a, case.b):
                for path in root.glob("*.sqlite3"):
                    with closing(sqlite3.connect(path)) as connection:
                        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
                    snapshots[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    monkeypatch.setattr(retained_startup, "retained_from_fresh_intent", last_reader)
    runtime = supervisor._runtime("bootstrap")
    original = ChildRecord(case.intent.new_instance, ChildPhase.STOPPED, "bootstrap", child_generation=1, supervisor_incarnation=supervisor.incarnation, intended_stop=True, runtime_receipt=runtime.receipt)
    supervisor.store.save(original)
    real_popen = subprocess.Popen

    def observed_popen(*args, **kwargs):
        child = real_popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"], close_fds=True)
        launches.append((dict(kwargs["env"]), child))
        return child

    monkeypatch.setattr(bootstrap_supervisor, "subprocess", N(Popen=observed_popen, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError))

    def authority(evidence):
        assert len(scans) == 1  # No new source/ancestor scan inside the callback.
        decision = consumer._check_authority(handoff, retained=evidence, reuse_retained=True)
        checks.append(decision)
        return decision is None

    try:
        options = {"forward_authority": authority} if callback_enabled else {}
        result = supervisor.start_selected("physical-start", "attempt", "bootstrap", supervisor._identity(original), **options)
        assert len(launches) == 1
        if callback_enabled and fault != "valid":
            assert checks and checks[0] is not None
            assert result.status is ResultStatus.PROCESS_RESTORED
            # The sole Popen belongs to the bounded exact-original return.
            assert supervisor.store.load().recovery_used
            assert launches[0][0]["AUTO_CODER_CHILD_GENERATION"] == "3"
            assert len(scans) == 2
        else:
            assert result.status is ResultStatus.OBSERVED_RUNNING
            assert not supervisor.store.load().recovery_used
            assert launches[0][0]["AUTO_CODER_CHILD_GENERATION"] == "2"
            assert len(scans) == 1
            assert checks == ([None] if callback_enabled else [])
        assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in snapshots} == snapshots
        assert {path: path.read_bytes() for path in case.paths} == sealed
    finally:
        for _, child in launches:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
