"""Real journals/grants with controlled physical adapters and mount boundary.

These component tests drive the actual durable consumer/reconstruction entry
points. They do not claim to be Docker or physical process replacement tests.
"""

import fcntl
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor, Observation, ResultStatus
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import EffectReceipt, OperationPhase
from auto_coder.management.self_update_handoff import HandoffPhase, SelfUpdateHandoffConsumer
from tests.test_management_archived_exclusions import binding, generation, reader
from tests.test_management_exclusion_host import host_case  # noqa: F401
from tests.test_management_self_update_policy import INSTANCE, _accepted_record, _policy, boundary, production_boundary  # noqa: F401
from tests.test_management_self_update_supervisor import _handoff


@pytest.mark.parametrize("moment", ["before-stop", "before-forward-start"])
@pytest.mark.parametrize("fault", ["valid", "missing", "corrupt", "incomplete", "active", "key-collision", "direct-key-collision", "grant-during-read", "policy-during-read", "journal-during-read", "missing-factory", "redirected-bootstrap"])
def test_durable_forward_effect_rechecks_real_ancestors(boundary, tmp_path: Path, monkeypatch, moment, fault):
    grants, _, policy, bootstrap, *_, identity = boundary
    current, operation, _token, accepted = _accepted_record(boundary, tmp_path)
    sent = current.begin_effect(operation, 0, owner_attempt_id="manager", attempt_id="attempt", correlation_identity="correlation").record
    assert sent is not None
    source = _handoff(operation)
    previous = replace(source.previous, instance_id=INSTANCE)
    handoff = replace(source, expected_child=replace(source.expected_child, instance_id=INSTANCE), candidate=replace(source.candidate, instance_id=INSTANCE, source_sha="a" * 40), previous=previous, policy_revision=accepted.policy_revision, effect_generation=sent.effect.owner_generation)
    settings = N(bootstrap_config_path=bootstrap, operating_policy_path=policy, operation_state_root=tmp_path / "operations", state_root=grants.settings.database_path.parent, oauth=grants.settings)
    config = N(instance_id=INSTANCE, state_root=tmp_path / "consumer", journal_root=settings.operation_state_root, oauth_root=settings.state_root, management_config=tmp_path / "management.yaml", archived_exclusions=())
    calls = {"stop": [], "start": [], "restore": []}
    stopped = replace(handoff.expected_child, effect_operation_id=operation + ":stop")

    def stop(*args):
        calls["stop"].append(args)
        return Observation(ResultStatus.STOPPED, stopped)

    def start(*args):
        calls["start"].append(args)
        return Observation(ResultStatus.UNKNOWN, stopped)

    def restore(*args):
        calls["restore"].append(args)
        return Observation(ResultStatus.UNKNOWN, stopped)

    supervisor = N(
        config=config,
        bootstrap_config_path=bootstrap,
        incarnation=handoff.expected_supervisor,
        inspect=lambda: Observation(ResultStatus.OBSERVED_RUNNING, handoff.expected_child),
        reconcile=lambda: Observation(ResultStatus.STOPPED, stopped),
        request_graceful_stop=stop,
        start_selected=start,
        restore_stopped_original=restore,
        _identity=ManagementBootstrapSupervisor._identity,
    )
    consumer = SelfUpdateHandoffConsumer(supervisor)
    consumer.store.submit(handoff)
    # Keep real original grant and policy checks; substitute only transport/clock
    # setup and the container mount/provenance boundary with real A/B readers.
    monkeypatch.setattr("auto_coder.management.oauth_config.load_management_settings", lambda _: settings)
    monkeypatch.setattr("auto_coder.management.identity.FirebaseOperatorIdentity", lambda _: identity)
    monkeypatch.setattr("auto_coder.management.oauth_grants.OAuthGrantService", lambda *_: grants)
    monkeypatch.setattr("auto_coder.management.self_update_handoff.runtime_for_receipt", lambda *_: N(verify=lambda: True))
    a, b = tmp_path / "a", tmp_path / "b"
    generation(a, "manager-a", management=True)
    generation(b, "manager-b", management=True)
    config.archived_exclusions = (binding(a, "manager-a"),)
    db = a / "management-operations.sqlite3"

    armed = [False]

    def retained(_settings):
        if armed[0]:
            armed[0] = False
            if fault == "missing-factory":
                return None
            if fault == "grant-during-read":
                grants.revoke("client", _token.refresh_token)
            elif fault == "policy-during-read":
                _policy(policy, enabled=False)
            elif fault == "journal-during-read":
                assert current.transition(operation, sent.revision, OperationPhase.NEEDS_ATTENTION, safe_error_category="synthetic-evidence-change").record is not None
        return reader(b, "manager-b", archives=(reader(a, "manager-a"),))

    monkeypatch.setattr("auto_coder.management.retained_startup.retained_from_fresh_intent", retained)
    assert consumer._authority_refusal(handoff) is None
    if moment == "before-forward-start":
        consumer.tick()
        assert len(calls["stop"]) == 1
        assert consumer.store.read(operation).phase is HandoffPhase.STOPPED
    if fault == "missing":
        db.rename(a / "preserved-original.sqlite3")
    elif fault in {"corrupt", "incomplete"}:
        with sqlite3.connect(db) as connection:
            if fault == "corrupt":
                connection.execute("UPDATE metadata SET value='unsupported' WHERE key='schema_version'")
            else:
                connection.execute("DELETE FROM operation_history")
    elif fault == "active":
        generation(a, "manager-a-extra", active=True, management=True)
    elif fault in {"key-collision", "direct-key-collision"}:
        root, name = (a, "manager-a") if fault == "key-collision" else (b, "manager-b")
        prior = ManagementOperationStore(root)
        intent = replace(sent.intent, target=replace(sent.intent.target, instance_id=name))
        collision = prior.admit(intent, sent.authority).record
        assert collision is not None
        assert prior.transition(collision.operation_id, 0, OperationPhase.FAILED, evidence=EffectReceipt(collision.operation_id, "synthetic-no-effect", 1, "no_effect_sent")).record is not None
    prior_paths = (db, b / "management-operations.sqlite3")
    if fault == "redirected-bootstrap":
        settings.bootstrap_config_path = tmp_path / "unowned-bootstrap.json"
    original_bytes = {path: path.read_bytes() if path.exists() else None for path in prior_paths}
    before_current = current.get(operation).record
    armed[0] = True
    consumer.tick()
    if fault == "valid":
        assert len(calls["stop"]) == 1
        assert len(calls["start"]) == (moment == "before-forward-start")
    elif moment == "before-stop":
        assert calls == {"stop": [], "start": [], "restore": []}
        expected = HandoffPhase.DENIED if fault in {"grant-during-read", "policy-during-read"} else HandoffPhase.UNAVAILABLE
        assert consumer.store.read(operation).phase is expected
    else:
        assert len(calls["stop"]) == 1 and calls["start"] == []
        assert calls["restore"] == [(operation + ":restore", handoff.attempt_id, supervisor._identity(stopped), previous.management_runtime_id)]
    before_calls = {name: list(values) for name, values in calls.items()}
    if fault != "valid":
        consumer.tick()
        assert calls == before_calls  # no retry or second safety return
    if fault == "journal-during-read":
        assert current.get(operation).record.phase is OperationPhase.NEEDS_ATTENTION
        assert current.get(operation).record.revision == before_current.revision + 1
    else:
        assert current.get(operation).record == before_current
    assert {path: path.read_bytes() if path.exists() else None for path in prior_paths} == original_bytes


@pytest.mark.parametrize("restart", [False, None, True])
@pytest.mark.parametrize("fault", ["valid", "missing", "changed", "contended", "direct-contended", "database-missing", "database-corrupt", "database-active"])
def test_successful_reconstruction_holds_all_fences_before_publication(host_case, monkeypatch, restart, fault):
    from auto_coder.management.bootstrap_supervisor import BootstrapConfig

    case = host_case
    config = BootstrapConfig.read(case.bootstrap)
    config = replace(
        config,
        state_root=case.c / "state",
        journal_root=case.c / "journal",
        oauth_root=case.c / "oauth",
        archived_exclusions=tuple(replace(item, fence=item.host_fence) for item in config.archived_exclusions),
        fresh_prior_lock_path=Path("/management-prior-owner.lock"),
        restart_after_clean_shutdown=False if restart is None else restart,
        runtimes=tuple(replace(item, drain_ack=case.c / "state" / "drain.json", arguments=("management", "serve", "--config", str(case.c / "management.yaml"))) for item in config.runtimes),
    )
    supervisor = ManagementBootstrapSupervisor(config, bootstrap_config_path=case.bootstrap)
    stopped_child = replace(supervisor.store.load(), child_generation=1, intended_stop=True)
    supervisor.store.save(stopped_child)
    (config.state_root / "fresh-initialization.json").write_text("fixture presence marker")
    intent = replace(case.intent, new_instance=config.instance_id, journal_root=str(config.journal_root), oauth_root=str(config.oauth_root))
    # Container mount/input validation is covered separately. Exercise the real
    # reconstruction entry point here with actual independent kernel locks.
    monkeypatch.setattr("auto_coder.management.fresh_initialization.read_fresh_status", lambda _: {"status": "succeeded"})
    monkeypatch.setattr("auto_coder.management.fresh_initialization.load_fresh_initialization", lambda _: (intent, "digest"))
    monkeypatch.setattr("auto_coder.management.fresh_initialization.verify_initialization_inputs", lambda *args, **kwargs: None)
    from auto_coder.management.fresh_initialization import acquire_prior_owner_fence

    direct = case.b / "state" / ".supervisor-owner.lock"

    def mapped_fence(path, **kwargs):
        return acquire_prior_owner_fence(direct if path == Path("/management-prior-owner.lock") else path, **kwargs)

    monkeypatch.setattr("auto_coder.management.fresh_initialization.acquire_prior_owner_fence", mapped_fence)
    monkeypatch.setattr("auto_coder.management.oauth_config.load_management_settings", lambda _: N())
    monkeypatch.setattr("auto_coder.management.retained_startup.retained_from_fresh_intent", lambda _: reader(case.b / "journal", "manager-b", archives=(reader(case.a, "manager-a"),)))
    archive = case.a / "owner.lock"
    held = None
    if fault == "missing":
        archive.rename(case.a / "preserved-owner.lock")
    elif fault == "changed":
        archive.rename(case.a / "preserved-owner.lock")
        archive.touch(mode=0o600)
    elif fault in {"contended", "direct-contended"}:
        held = (archive if fault == "contended" else direct).open("r")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if fault == "database-missing":
        (case.a / "management-operations.sqlite3").rename(case.a / "preserved-original.sqlite3")
    elif fault == "database-corrupt":
        with sqlite3.connect(case.a / "management-operations.sqlite3") as db:
            db.execute("DELETE FROM operation_history")
    elif fault == "database-active":
        generation(case.a, "manager-a-extra", active=True, management=True)
    published = []

    class Inspected(Exception):
        pass

    original_publication = supervisor.store.publish_supervisor

    def publication(*args):
        published.append(True)
        for fence in (archive, direct):
            with fence.open("r") as contender:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if restart is True:
            raise Inspected
        return original_publication(*args)

    def service_loop_boundary(_seconds):
        assert len(published) >= 2
        assert supervisor.store.load() == stopped_child
        raise Inspected

    monkeypatch.setattr(supervisor.store, "publish_supervisor", publication)
    monkeypatch.setattr("auto_coder.management.bootstrap_supervisor.time.sleep", service_loop_boundary)
    monkeypatch.setattr(supervisor, "start_selected", lambda *args: pytest.fail("reconstruction validation must precede any child start"))
    try:
        if fault == "valid":
            with pytest.raises(Inspected):
                supervisor.run()
            assert len(published) == (1 if restart is True else 2)
        else:
            assert supervisor.run() == 1
            assert published == []
            assert supervisor._archived_owner_fences == () and supervisor._prior_owner_fence is None
            if fault == "direct-contended":
                with archive.open("r") as contender:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        supervisor._release_prior_fences()
        if held is not None:
            held.close()
    assert not (config.journal_root / "management-operations.sqlite3").exists()
