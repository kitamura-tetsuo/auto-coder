"""Component contracts; substituted filesystem/authority boundaries are explicit.

These are not container or source-test provenance end-to-end execution claims.
"""

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from auto_coder.management import readiness_observation
from auto_coder.management.bootstrap_store import ChildPhase, ChildRecord
from auto_coder.management.bootstrap_supervisor import BootstrapConfig
from auto_coder.management.fresh_initialization import application_tree_sha256
from auto_coder.management.fresh_runtime_binding import FreshRuntimeBinding, _serving_live, fresh_management_path, parse_fresh_runtime_binding, verify_installed_fresh_binding
from auto_coder.management.self_update_handoff import runtime_for_receipt
from auto_coder.worker_versions import PreparationFailure, WorkerVersionError
from tests.test_management_fresh_initialization import _intent
from tests.test_management_self_update_supervisor import _receipt


@pytest.fixture
def binding_case(tmp_path, monkeypatch):
    import hashlib
    import os

    intent = _intent(tmp_path)
    anchor = tmp_path / "immutable-bootstrap"
    anchor.mkdir()
    (anchor / "__init__.py").write_text("bootstrap original")
    version_root = tmp_path / "versions"
    version_root.mkdir()
    config_path = tmp_path / "bootstrap.json"
    management = tmp_path / "management.yaml"
    management.write_text("component-settings")
    environment = version_root / "initial" / "venv"
    (environment / "bin").mkdir(parents=True)
    python = environment / "bin" / "python"
    python.write_bytes(b"component-python")
    sha = hashlib.sha256(python.read_bytes()).hexdigest()
    ready = replace(
        _receipt("initial"), instance_id=intent.new_instance, management_runtime_id="installed-initial", environment_path=str(environment), interpreter_path=str(python), python_executable_sha256=sha, configuration_sha256=hashlib.sha256(management.read_bytes()).hexdigest(), receipt_identity="a" * 64
    )
    config = BootstrapConfig(
        intent.new_instance,
        tmp_path / "state",
        tmp_path / "oauth",
        tmp_path / "journal",
        "http://127.0.0.1:8081/mcp",
        ready.management_runtime_id,
        (),
        management_version_root=version_root,
        management_config=management,
        fresh_guard_path=tmp_path / "guard" / "fresh-guard.sock",
        fresh_prior_lock_path=Path("/management-prior-owner.lock"),
        fresh_runtime_binding=FreshRuntimeBinding(2, str(anchor), str(environment.parent / "MANAGEMENT_READY.json"), ready.receipt_identity),
    )
    runtime = runtime_for_receipt(config, ready)
    config = replace(config, runtimes=(runtime,))
    config_path.write_text("component-bootstrap-bytes")
    intent = replace(
        intent,
        runtime_reference=runtime.reference,
        state_root=str(config.state_root),
        journal_root=str(config.journal_root),
        oauth_root=str(config.oauth_root),
        executable_sha256=sha,
        application_sha256=application_tree_sha256(anchor),
        bootstrap_config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        management_config_sha256=ready.configuration_sha256,
    )
    settings = N(bootstrap_config_path=config_path, config_path=management, state_root=config.oauth_root, operation_state_root=config.journal_root)
    calls = []
    receipts = {runtime.ready_receipt: ready}

    class ControlledStager:
        def lookup(self, path):
            calls.append(path)
            value = receipts[path]
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr("auto_coder.management.configured_stager.configured_management_stager", lambda *a, **k: ControlledStager())
    statvfs = os.statvfs
    monkeypatch.setattr("auto_coder.management.fresh_runtime_binding.os.statvfs", lambda p: N(f_flag=os.ST_RDONLY) if Path(p) == anchor else statvfs(p))
    return N(config=config, path=config_path, intent=intent, settings=settings, anchor=anchor, runtime=runtime, ready=ready, calls=calls, receipts=receipts)


def verify(case, **kwargs):
    return verify_installed_fresh_binding(case.config, case.path, case.intent, case.settings, **kwargs)


def test_versioned_binding_is_opt_in_strict_and_intent_schema_unchanged(binding_case):
    case = binding_case
    assert parse_fresh_runtime_binding(None) is None
    assert parse_fresh_runtime_binding(asdict(case.config.fresh_runtime_binding)) == case.config.fresh_runtime_binding
    assert "fresh_runtime_binding" not in asdict(case.intent)
    assert fresh_management_path(case.runtime) == case.settings.config_path
    assert verify(case, bootstrap_application=case.anchor) == case.runtime
    assert case.calls == [case.runtime.ready_receipt]


@pytest.mark.parametrize("mutation", [dict(version=1), dict(version=3), dict(version=True), dict(initial_ready_identity="z" * 64), dict(initial_ready_path="../escape"), dict(bootstrap_application_root="/a/../b"), dict(extra="field")])
def test_binding_rejects_unknown_version_or_malformed_evidence(binding_case, mutation):
    raw = {**asdict(binding_case.config.fresh_runtime_binding), **mutation}
    with pytest.raises(ValueError):
        parse_fresh_runtime_binding(raw)


@pytest.mark.parametrize("fault", ["bootstrap-bytes", "management-bytes", "anchor-bytes", "wrong-owner-package", "wrong-ready-id", "wrong-config", "python-bytes", "missing-proof", "mutable-overlap", "hook"])
def test_original_binding_refuses_incomplete_or_changed_evidence(binding_case, fault):
    case = binding_case
    kwargs = {}
    if fault == "bootstrap-bytes":
        case.path.write_text("changed")
    elif fault == "management-bytes":
        case.settings.config_path.write_text("changed")
    elif fault == "anchor-bytes":
        (case.anchor / "__init__.py").write_text("changed")
    elif fault == "wrong-owner-package":
        kwargs["bootstrap_application"] = case.anchor.parent
    elif fault == "wrong-ready-id":
        case.receipts[case.runtime.ready_receipt] = replace(case.ready, receipt_identity="b" * 64)
    elif fault == "wrong-config":
        case.receipts[case.runtime.ready_receipt] = replace(case.ready, configuration_sha256="b" * 64)
    elif fault == "python-bytes":
        case.runtime.executable.write_bytes(b"changed")
    elif fault == "missing-proof":
        case.receipts[case.runtime.ready_receipt] = WorkerVersionError(PreparationFailure.UNAVAILABLE, "proof unavailable")
    elif fault == "mutable-overlap":
        case.config = replace(case.config, management_version_root=case.anchor)
    elif fault == "hook":
        case.config = replace(case.config, runtimes=(replace(case.runtime, environment=(*case.runtime.environment, ("LD_PRELOAD", "/unsafe"))),))
    with pytest.raises(ValueError):
        verify(case, **kwargs)


@pytest.mark.parametrize("hook", ["LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"])
def test_installed_fresh_never_inherits_code_hooks(binding_case, hook):
    case = binding_case
    case.config = replace(case.config, runtimes=(replace(case.runtime, environment=(*case.runtime.environment, (hook, "unsafe"))),))
    with pytest.raises(ValueError, match="code hooks"):
        verify(case)


def test_new_authenticated_child_does_not_have_to_equal_bootstrap_source(binding_case, monkeypatch):
    import os

    import auto_coder

    case = binding_case
    environment = case.config.management_version_root / "candidate" / "venv"
    package = environment / "lib" / "auto_coder"
    package.mkdir(parents=True)
    application = package / "__init__.py"
    application.write_text("genuinely different candidate code")
    current = replace(case.ready, management_runtime_id="installed-candidate", environment_path=str(environment), interpreter_path=str(environment / "bin" / "python"), receipt_identity="b" * 64)
    selected = runtime_for_receipt(case.config, current)
    case.receipts[selected.ready_receipt] = current
    child = ChildRecord(case.config.instance_id, ChildPhase.RUNNING, selected.reference, pid=os.getpid(), child_generation=2, drain_nonce="nonce", runtime_receipt=selected.receipt, source_receipt=current.receipt_identity)
    monkeypatch.setattr("auto_coder.management.fresh_runtime_binding._serving_live", lambda _: (child, selected))
    monkeypatch.setattr(auto_coder, "__file__", str(application))
    assert verify(case, serving_process=True) == case.runtime
    assert case.calls == [case.runtime.ready_receipt, selected.ready_receipt]
    case.receipts[selected.ready_receipt] = replace(current, receipt_identity="c" * 64)
    with pytest.raises(ValueError, match="authenticated READY"):
        verify(case, serving_process=True)


def test_serving_validation_waits_for_publication_without_authorizing_another_pid(binding_case, monkeypatch):
    import os

    case = binding_case
    child = ChildRecord(case.config.instance_id, ChildPhase.RUNNING, pid=os.getpid(), child_generation=2, drain_nonce="nonce")
    monkeypatch.setenv("AUTO_CODER_CHILD_GENERATION", "2")
    monkeypatch.setenv("AUTO_CODER_DRAIN_NONCE", "nonce")
    observations = iter([None, ("incarnation", child)])
    monkeypatch.setattr("auto_coder.management.self_update_handoff.observe_supervisor", lambda _: next(observations))
    lives = iter([None, (child, case.runtime)])
    monkeypatch.setattr(readiness_observation, "_live", lambda _: next(lives))
    monkeypatch.setattr("auto_coder.management.fresh_runtime_binding.time.sleep", lambda _: None)
    assert _serving_live(case.config) == (child, case.runtime)
    monkeypatch.setattr("auto_coder.management.self_update_handoff.observe_supervisor", lambda _: ("incarnation", replace(child, pid=os.getpid() + 1)))
    with pytest.raises(ValueError, match="identity differs"):
        _serving_live(case.config)


def test_serving_validation_never_downgrades_missing_or_mismatched_launch(binding_case, monkeypatch):
    case = binding_case
    monkeypatch.delenv("AUTO_CODER_CHILD_GENERATION", raising=False)
    monkeypatch.delenv("AUTO_CODER_DRAIN_NONCE", raising=False)
    with pytest.raises(ValueError, match="identity is missing"):
        _serving_live(case.config)
    monkeypatch.setenv("AUTO_CODER_CHILD_GENERATION", "2")
    monkeypatch.setenv("AUTO_CODER_DRAIN_NONCE", "nonce")
    monkeypatch.setattr("auto_coder.management.self_update_handoff.observe_supervisor", lambda _: None)
    monkeypatch.setattr(readiness_observation, "_live", lambda _: None)
    times = iter([0, 3])
    monkeypatch.setattr("auto_coder.management.fresh_runtime_binding.time.monotonic", lambda: next(times))
    with pytest.raises(ValueError, match="live proof is unavailable"):
        _serving_live(case.config)


@pytest.mark.parametrize("serving", [True, False])
def test_retained_reader_default_is_serving_and_owner_purpose_is_explicit(binding_case, monkeypatch, serving):
    from auto_coder.management.retained_startup import retained_from_fresh_intent

    case = binding_case
    captured = []

    class BoundaryReached(Exception):
        pass

    def verifier(*args, **kwargs):
        captured.append(kwargs)
        raise BoundaryReached()

    monkeypatch.setattr("auto_coder.management.retained_startup.BootstrapConfig.read", lambda _: case.config)
    monkeypatch.setattr("auto_coder.management.retained_startup.load_fresh_initialization", lambda _: (case.intent, "component-digest"))
    monkeypatch.setattr("auto_coder.management.fresh_runtime_binding.verify_installed_fresh_binding", verifier)
    with pytest.raises(BoundaryReached):
        if serving:
            retained_from_fresh_intent(case.settings)
        else:
            retained_from_fresh_intent(case.settings, serving_process=False)
    assert len(captured) == 1 and captured[0]["serving_process"] is serving


def test_public_bootstrap_parser_seals_new_profile_without_legacy_intent_changes(binding_case):
    import json

    case = binding_case
    raw = asdict(case.config)
    raw["runtimes"] = [{**asdict(case.runtime), "environment": dict(case.runtime.environment)}]
    case.path.write_text(json.dumps(raw, default=str))
    assert BootstrapConfig.read(case.path) == case.config
    raw["fresh_runtime_binding"]["version"] = 999
    case.path.write_text(json.dumps(raw, default=str))
    with pytest.raises(ValueError, match="unsupported fresh runtime binding version"):
        BootstrapConfig.read(case.path)
