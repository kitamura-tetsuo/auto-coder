"""Synthetic close failures with real journals, owner fences and owned children.

Container paths/RO mount provenance come from the component fixture. Application
argv is replaced by a real waiting Python child; no Docker/installed READY proof
is claimed. Local recovery admission is controlled, while its launch/store run.
"""

import fcntl
import hashlib
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from types import SimpleNamespace as N

import pytest

from auto_coder.management import bootstrap_supervisor, recovery_supervisor, retained_startup
from auto_coder.management.bootstrap_store import ChildPhase, ChildRecord
from auto_coder.management.bootstrap_supervisor import ResultStatus
from auto_coder.management.recovery_mailbox import RecoveryIntent
from auto_coder.management.self_update_handoff import HandoffPhase, SelfUpdateHandoffConsumer
from tests.test_management_fresh_launch_exclusions import first_launch_case  # noqa: F401
from tests.test_management_self_update_supervisor import _handoff


class FailingCloseReader:
    def __init__(self, reader, *, read_failure=False):
        self.reader = reader
        self.read_failure = read_failure
        self.closes = 0

    def read(self):
        if self.read_failure:
            raise OSError("synthetic evidence read failure")
        return self.reader.read()

    def close(self):
        self.closes += 1
        self.reader.close()
        raise OSError("synthetic close failure after real close")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def hold_and_record(case):
    supervisor = case.supervisor
    supervisor._acquire_prior_fences(case.intent)
    supervisor.store.publish_supervisor(supervisor.incarnation, os.getpid(), supervisor._start_token(os.getpid()))
    runtime = supervisor._runtime("bootstrap")
    record = ChildRecord(case.intent.new_instance, ChildPhase.STOPPED, "bootstrap", child_generation=1, supervisor_incarnation=supervisor.incarnation, intended_stop=True, runtime_receipt=runtime.receipt)
    supervisor.store.save(record)
    return record, runtime


def assert_owner_fences(case):
    # Temporary readers open separate descriptions; closing them must not drop
    # either the owner's EX or its persistent ancestor SH locks.
    for descriptor in (case.owner.fileno(), *case.supervisor._archived_owner_fences, case.supervisor._prior_owner_fence):
        assert os.fstat(descriptor)
    for path in (case.state / ".supervisor-owner.lock", case.a / "owner.lock", case.b / "owner.lock"):
        with path.open("rb") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)


def snapshot(case):
    paths = (*case.paths, *(p for root in (case.a, case.b) for p in root.glob("*.sqlite3")))
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_read_and_cleanup_failure_never_enters_physical_fallback(first_launch_case, monkeypatch):
    case = first_launch_case
    original, _ = hold_and_record(case)
    before = snapshot(case)
    actual = retained_startup.retained_from_fresh_intent
    readers, launches = [], []

    def broken(*args, **kwargs):
        wrapper = FailingCloseReader(actual(*args, **kwargs), read_failure=True)
        readers.append(wrapper)
        return wrapper

    monkeypatch.setattr(retained_startup, "retained_from_fresh_intent", broken)
    monkeypatch.setattr(bootstrap_supervisor, "subprocess", N(Popen=lambda *_a, **_k: launches.append(True)))
    result = case.supervisor.start_selected("effect", "attempt", "bootstrap", case.supervisor._identity(original))
    assert result.status is ResultStatus.NEEDS_ATTENTION
    assert not result.record.recovery_used and launches == []
    assert len(readers) == 1 and readers[0].closes == 1
    assert readers[0].reader._fd == -1
    assert_owner_fences(case)
    assert snapshot(case) == before


@pytest.mark.parametrize("gate", ["pre-authority", "post-authority"])
def test_handoff_gate_close_failure_preserves_stopped_lane(first_launch_case, monkeypatch, gate):
    case = first_launch_case
    original, _ = hold_and_record(case)
    source = _handoff("operation")
    expected = replace(original, effect_operation_id="operation:stop")
    case.supervisor.store.save(expected)
    handoff = replace(source, expected_supervisor=case.supervisor.incarnation, expected_child=expected, candidate=replace(source.candidate, instance_id=case.intent.new_instance), previous=replace(source.previous, instance_id=case.intent.new_instance, management_runtime_id="bootstrap"))
    consumer = SelfUpdateHandoffConsumer(case.supervisor)
    submitted = consumer.store.submit(handoff)
    stopped = consumer.store.change(submitted, phase=HandoffPhase.STOPPED)
    assert stopped is not None
    monkeypatch.setattr(consumer, "_authority_refusal", lambda _: None)
    actual = case.supervisor._exclusion_evidence
    calls, opens = [], []

    def evidence():
        reader = actual()
        opens.append(reader)
        failing = gate == "pre-authority" or len(opens) % 2 == 0
        return FailingCloseReader(reader) if failing else reader

    monkeypatch.setattr(case.supervisor, "_exclusion_evidence", evidence)
    monkeypatch.setattr(case.supervisor, "start_selected", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(case.supervisor, "restore_stopped_original", lambda *a, **k: calls.append((a, k)))
    before = snapshot(case)
    for _ in range(2):
        consumer.tick()
        assert consumer.store.read("operation") == stopped
        assert case.supervisor.store.load() == expected
        assert calls == []
        assert_owner_fences(case)
        assert snapshot(case) == before


def test_normal_temporary_reader_close_is_independent_of_owner_locks(first_launch_case):
    case = first_launch_case
    hold_and_record(case)
    before = snapshot(case)
    with case.supervisor._exclusion_evidence() as reader:
        temporary = (reader, *reader.exclusions)
        descriptors = tuple(item._fd for item in temporary)
        for descriptor in descriptors:
            assert os.fstat(descriptor)
            assert descriptor not in (*case.supervisor._archived_owner_fences, case.supervisor._prior_owner_fence, case.owner.fileno())
    assert all(item._fd == -1 for item in temporary)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert_owner_fences(case)
    assert snapshot(case) == before


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
@pytest.mark.parametrize("boundary", ["start", "restore", "recovery"])
def test_post_popen_close_failure_keeps_owned_handle_without_resend(first_launch_case, monkeypatch, boundary):
    case = first_launch_case
    original, runtime = hold_and_record(case)
    supervisor = case.supervisor
    if boundary == "restore":
        original = replace(original, phase=ChildPhase.NEEDS_ATTENTION, selected_runtime="failed", previous_runtime="bootstrap", intended_stop=False, effect_operation_id="effect", effect_attempt_id="attempt")
        supervisor.store.save(original)
        supervisor.store.begin_effect("effect", "attempt", "START_SELECTED", "failed", "")
    consumer = intent = None
    if boundary == "recovery":
        consumer = recovery_supervisor.RecoveryConsumer(supervisor, supervisor.bootstrap_config_path)
        path = supervisor.config.management_config
        intent = RecoveryIntent(str(uuid.uuid4()), case.intent.new_instance, supervisor.incarnation, 1, "bootstrap", runtime.receipt, runtime.source_receipt, str(path), hashlib.sha256(path.read_bytes()).hexdigest())
        consumer.mailbox.submit(intent)
        assert consumer._set(intent.request_id, "pending", "claimed", "synthetic admitted recovery")
        monkeypatch.setattr(consumer, "_settings", lambda *_: N())
    before = snapshot(case)
    actual = supervisor._exclusion_evidence
    monkeypatch.setattr(supervisor, "_exclusion_evidence", lambda: FailingCloseReader(actual()))
    real_popen = subprocess.Popen
    launches = []

    def popen(*_args, **_kwargs):
        child = real_popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"], close_fds=True)
        launches.append(child)
        return child

    port = N(Popen=popen, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError)
    monkeypatch.setattr(bootstrap_supervisor, "subprocess", port)
    monkeypatch.setattr(recovery_supervisor, "subprocess", port)
    try:
        if boundary == "start":
            result = supervisor.start_selected("effect", "attempt", "bootstrap", supervisor._identity(original))
            assert result.status is ResultStatus.UNKNOWN
        elif boundary == "restore":
            result = supervisor._restore_once("effect", original)
            assert result.status is ResultStatus.UNKNOWN
        else:
            consumer._launch(intent, original, runtime)
            assert consumer.mailbox.get(intent.request_id).status == "unknown"
        assert len(launches) == 1 and launches[0].poll() is None
        assert supervisor._children[launches[0].pid] is launches[0]
        assert supervisor.inspect().status is ResultStatus.UNKNOWN
        assert_owner_fences(case)
        if boundary == "start":
            supervisor.start_selected("effect", "attempt", "bootstrap", supervisor._identity(original))
        elif boundary == "restore":
            supervisor._restore_once("effect", supervisor.store.load())
        else:
            consumer.tick()
        assert len(launches) == 1 and launches[0].poll() is None
        assert_owner_fences(case)
        assert snapshot(case) == before
    finally:
        for child in launches:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
