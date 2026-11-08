"""Real A/B/C journals and dispatcher; installed/process adapters are controlled.

The production self-update handler reaches an observed compatibility preflight
and a real durable handoff on the positive controls. This is a component test,
not installed-code, physical Supervisor or container conformance evidence.
"""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from auto_coder.management.operation_executor import ManagementOperationDispatcher, OperationHandler
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import ActionParameter, AdmissionStatus, EffectReceipt, EffectState, OperationPhase
from auto_coder.management.retained_journal import RetainedGuardedOperationStore
from auto_coder.management.self_update_handoff import HandoffPhase, HandoffStore
from auto_coder.management.self_update_operation import ManagementSelfUpdateHandler
from auto_coder.management.self_update_preflight import ManagementPreflightReceipt
from tests.test_management_archived_exclusions import generation, reader
from tests.test_management_self_update_policy import INSTANCE, _accepted_record, boundary, production_boundary  # noqa: F401
from tests.test_management_self_update_supervisor import _handoff


@pytest.mark.parametrize("prior", ["archive", "direct"])
@pytest.mark.parametrize("separation", ["none", "key", "actor", "client"])
def test_preexisting_management_dispatch_checks_original_namespace(boundary, tmp_path: Path, monkeypatch, prior, separation):
    grants, _, policy, bootstrap, *_ = boundary
    plain, operation, _, _ = _accepted_record(boundary, tmp_path)
    original = plain.get(operation).record
    assert original is not None and original.intent.target.instance_id == INSTANCE
    a, b = tmp_path / "a", tmp_path / "b"
    generation(a, "manager-a", management=True)
    generation(b, "manager-b", management=True)
    root, instance = (a, "manager-a") if prior == "archive" else (b, "manager-b")
    old_intent = replace(original.intent, target=replace(original.intent.target, instance_id=instance))
    old_authority = original.authority
    if separation == "key":
        old_intent = replace(old_intent, idempotency_key="unrelated-key")
    elif separation == "actor":
        old_authority = replace(old_authority, uid="another-operator")
    elif separation == "client":
        old_authority = replace(old_authority, oauth_client_id="another-client")
    old_store = ManagementOperationStore(root)
    old = old_store.admit(old_intent, old_authority).record
    assert old is not None
    assert old_store.transition(old.operation_id, 0, OperationPhase.FAILED, evidence=EffectReceipt(old.operation_id, "synthetic-no-effect", 1, "no_effect_sent")).record is not None
    paths = (a / "management-operations.sqlite3", b / "management-operations.sqlite3")
    prior_bytes = {path: path.read_bytes() for path in paths}
    source = _handoff(operation)
    previous = replace(source.previous, instance_id=INSTANCE, environment_path=str(tmp_path / "old" / "venv"))
    candidate = replace(source.candidate, instance_id=INSTANCE, source_sha="a" * 40, environment_path=str(tmp_path / "candidate" / "venv"))
    child = replace(source.expected_child, instance_id=INSTANCE, source_receipt=previous.receipt_identity, runtime_receipt="sha256:" + previous.python_executable_sha256)
    config = N(instance_id=INSTANCE, state_root=tmp_path / "handoffs", management_config=tmp_path / "management.yaml", runtimes=(N(reference=previous.management_runtime_id, ready_receipt=tmp_path / "old" / "MANAGEMENT_READY.json"),))
    settings = N(operating_policy_path=policy, bootstrap_config_path=bootstrap)
    preflights = []

    def preflight(_self, selected, original_runtime):
        preflights.append((selected, original_runtime))
        return ManagementPreflightReceipt(selected.receipt_identity, original_runtime.receipt_identity, "configuration", "oauth", "journal", 1)

    # Keep original grant/policy rechecks, dispatcher, journal claims and handoff
    # persistence real. Only the installed/runtime/physical adapters are supplied.
    monkeypatch.setattr("auto_coder.management.self_update_operation.observe_supervisor", lambda _: (source.expected_supervisor, child))
    monkeypatch.setattr("auto_coder.management.self_update_operation.runtime_for_receipt", lambda *_: N(verify=lambda: True))
    monkeypatch.setattr("auto_coder.management.self_update_operation.ManagementSelfUpdatePreflight.verify", preflight)
    with reader(b, "manager-b", archives=(reader(a, "manager-a"),)) as retained:
        current = RetainedGuardedOperationStore(tmp_path / "operations", retained)
        handler = ManagementSelfUpdateHandler(settings, grants, current)
        monkeypatch.setattr(handler, "_configuration", lambda: config)
        monkeypatch.setattr(handler, "_stager", lambda _: N(lookup=lambda path: candidate if path.parent == tmp_path / "candidate" else previous))
        monkeypatch.setattr(handler, "_ready_for_source", lambda *_: candidate)
        monkeypatch.setattr(handler, "_disk_preflight", lambda _: None)
        monkeypatch.setattr(handler, "reconcile", lambda record: record)
        dispatcher = ManagementOperationDispatcher(current, effect_guard=current.effect_permitted)
        dispatcher.register(original.intent.action, OperationHandler(handler.forward, handler.reconcile))

        async def dispatch():
            await dispatcher.start()
            try:
                await dispatcher.drain()  # real startup scan of accepted C row
                await dispatcher.submit(operation)  # repeated delivery cannot bypass
            finally:
                await dispatcher.stop()

        asyncio.run(dispatch())
        record = current.get(operation).record
        handoff = HandoffStore(config.state_root, INSTANCE).read(operation)
        if separation == "none":
            assert preflights == [], "archived/direct key reached candidate compatibility execution"
            assert handoff is None, "excluded key persisted a durable handoff"
            assert record == original and record.effect.state is EffectState.NOT_SENT
        else:
            assert preflights == [(candidate, previous)]
            assert handoff is not None and handoff.phase is HandoffPhase.SUBMITTED
            assert handoff.handoff.operation_id == operation
            assert record is not None and record.effect.state is EffectState.SENT_UNKNOWN
    assert {path: path.read_bytes() for path in paths} == prior_bytes


@pytest.mark.parametrize("management", [True, False], ids=["management", "worker"])
@pytest.mark.parametrize("prior", ["archive", "direct"])
@pytest.mark.parametrize("separation", ["none", "source", "actor", "client", "key"])
def test_admit_and_preexisting_effect_share_key_exclusion(tmp_path: Path, management, prior, separation):
    a, b = tmp_path / "a", tmp_path / "b"
    _, old_a, _ = generation(a, "manager-a", management=management)
    _, old_b, _ = generation(b, "manager-b", management=management)
    old = old_a if prior == "archive" else old_b
    intent = replace(old.intent, target=replace(old.intent.target, instance_id="manager-c")) if management else old.intent
    authority = old.authority
    if separation == "source":
        intent = replace(intent, parameters=tuple(part for part in intent.parameters if part.name != "source_sha") + (ActionParameter("source_sha", "b" * 40),))
    elif separation == "actor":
        authority = replace(authority, uid="different-actor")
    elif separation == "client":
        authority = replace(authority, oauth_client_id="different-client")
    elif separation == "key":
        intent = replace(intent, idempotency_key="unrelated-key")
    paths = (a / "management-operations.sqlite3", b / "management-operations.sqlite3")
    prior_bytes = {path: path.read_bytes() for path in paths}
    with reader(b, "manager-b", archives=(reader(a, "manager-a"),)) as retained:
        guarded = RetainedGuardedOperationStore(tmp_path / "admit", retained)
        snapshot = None if management else {"schema_version": 1}
        admitted = guarded.admit(intent, authority, lifecycle_snapshot=snapshot)
        # A different payload is still the same key. Actor, client and key
        # separation are controls for both management and unchanged worker paths.
        allowed = separation in {"actor", "client", "key"}
        if allowed:
            assert admitted.status is AdmissionStatus.CREATED and admitted.record is not None
        else:
            assert admitted.status in {AdmissionStatus.BUSY, AdmissionStatus.CONFLICT}
            assert admitted.status is (AdmissionStatus.CONFLICT if prior == "archive" or separation == "source" else AdmissionStatus.BUSY)
            assert admitted.record is None if prior == "archive" else admitted.record == old
            assert guarded.list_operations().records == ()
        preexisting = ManagementOperationStore(tmp_path / "preexisting").admit(intent, authority, lifecycle_snapshot=snapshot).record
        assert preexisting is not None
        current = RetainedGuardedOperationStore(tmp_path / "preexisting", retained)
        assert current.effect_permitted(preexisting) is allowed
    assert {path: path.read_bytes() for path in paths} == prior_bytes


@pytest.mark.parametrize("fault", ["closed", "missing", "history"])
def test_management_namespace_lookup_fails_closed_before_admit_or_dispatch(tmp_path: Path, fault):
    import sqlite3

    a, b = tmp_path / "a", tmp_path / "b"
    _, old, _ = generation(a, "manager-a", management=True)
    generation(b, "manager-b", management=True)
    intent = replace(old.intent, target=replace(old.intent.target, instance_id="manager-c"), idempotency_key="unrelated-key")
    existing = ManagementOperationStore(tmp_path / "current").admit(intent, old.authority).record
    assert existing is not None
    with reader(b, "manager-b", archives=(reader(a, "manager-a"),)) as retained:
        current = RetainedGuardedOperationStore(tmp_path / "current", retained)
        assert current.effect_permitted(existing)
        if fault == "closed":
            retained.exclusions[0].close()
        elif fault == "missing":
            (a / "management-operations.sqlite3").rename(a / "preserved-original.sqlite3")
        else:
            with sqlite3.connect(a / "management-operations.sqlite3") as connection:
                connection.execute("DELETE FROM operation_history")
        assert not current.effect_permitted(existing)
        assert current.admit(intent, old.authority).status is AdmissionStatus.UNAVAILABLE
        assert current.get(existing.operation_id).record == existing
