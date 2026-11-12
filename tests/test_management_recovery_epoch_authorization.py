"""Fixed authorization/persistence component tests, not host recovery acceptance."""

import hashlib
import inspect
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management.original_attempts import HistoryBinding, canonical
from auto_coder.management.recovery_containment_model import RecoveryFile
from auto_coder.management.recovery_epoch_authorization import AUTHORIZATION_SCHEMA, EpochAuthorization, _load_authorization, load_authorization, read_bound_file, validate_uv_version
from auto_coder.management.recovery_epoch_state import EpochBinding, EpochInventory, EpochUnavailable, _hold_recovery_epoch, issuer_lifetime

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@pytest.fixture
def _use_real_commands():
    pass


@pytest.fixture
def _use_real_sleep():
    pass


def _write(path, encoded):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)


def _selected(path):
    info = path.stat()
    return RecoveryFile(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), info.st_dev, info.st_ino)


def _fixture(tmp_path):
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    for name in ("consumer.json", "READY.json"):
        _write(authority_root / name, b"{}")
    authorization = EpochAuthorization(
        AUTHORIZATION_SCHEMA,
        "deployment-A",
        "review-A",
        "request-A",
        "preparation-A",
        "a" * 64,
        "enrollment-A",
        HistoryBinding(str(tmp_path / "history"), "deployment-A", "00000000-0000-4000-8000-000000000001", "sha256:" + "b" * 64),
        _selected(authority_root / "consumer.json"),
        _selected(authority_root / "READY.json"),
        str(tmp_path / "source"),
        str(authority_root / "source-token"),
        "/usr/bin/python3",
        "/usr/bin/uv",
        "0.7.22",
        "c" * 40,
        authority_root.stat().st_dev,
        authority_root.stat().st_ino,
    )
    path = authority_root / "authorization.json"
    encoded = canonical(asdict(authorization))
    _write(path, encoded)
    root = tmp_path / "epoch"
    root.mkdir(mode=0o700)
    _write(root / "epoch.lock", b"")
    lock = (root / "epoch.lock").stat()
    inventory = EpochInventory(
        2,
        str(root),
        root.stat().st_dev,
        root.stat().st_ino,
        lock.st_dev,
        lock.st_ino,
        authorization.deployment_id,
        authorization.authorization_id,
        (authorization.enrollment_id,),
        authorization.request_id,
        authorization.preparation_id,
        authorization.target_id,
        "b" * 64,
        "d" * 64,
        hashlib.sha256(encoded).hexdigest(),
    )
    _write(root / "inventory.json", canonical(asdict(inventory)))
    return root, path, authorization, inventory


def _binding(inventory, *, evidence=None):
    evidence = canonical({"originals": [{"journal": "retained", "sha256": "e" * 64}], "outcome": "unknown"}) if evidence is None else evidence
    boot, pid, start = issuer_lifetime()
    return EpochBinding(
        inventory.deployment_id,
        inventory.authorization_id,
        inventory.enrollment_ids,
        inventory.request_id,
        inventory.preparation_id,
        inventory.target_id,
        inventory.source_sha256,
        inventory.config_sha256,
        hashlib.sha256(evidence).hexdigest(),
        boot,
        pid,
        start,
        inventory.authorization_sha256,
        evidence.decode(),
    )


def test_complete_authorization_is_bound_to_fixed_inventory(tmp_path):
    _, path, authorization, inventory = _fixture(tmp_path)
    result = _load_authorization(path, inventory)
    assert result.authorization == authorization
    assert result.original == _selected(path)
    assert tuple(inspect.signature(load_authorization).parameters) == ("inventory",)


@pytest.mark.parametrize("version", ["0.7.22", "0.13.6 (329bf94 2026-10-26)", "0.13.6 (x86_64-unknown-linux-gnu)", "0.13.6 (x)", "0.13.6 (" + "x" * 119 + ")"])
def test_authorization_preserves_the_complete_installer_pin(tmp_path, version):
    _, path, authorization, inventory = _fixture(tmp_path)
    selected = replace(authorization, uv_version=version)
    encoded = canonical(asdict(selected))
    path.write_bytes(encoded)
    observed = _load_authorization(path, replace(inventory, authorization_sha256=hashlib.sha256(encoded).hexdigest()))
    assert validate_uv_version(version) == version
    assert observed.authorization == selected
    assert observed.authorization.uv_version == version
    assert observed.original.sha256 != inventory.authorization_sha256 or version == authorization.uv_version


@pytest.mark.parametrize(
    "version",
    [
        None,
        True,
        13,
        "",
        "0.13",
        "uv 0.13.6",
        "0.13.6\n",
        "0.13.6\r",
        "0.13.6 ",
        "0.13.6 (x) extra",
        "0.13.6 (x) (y)",
        "0.13.6 ()",
        "0.13.6 ( )",
        "0.13.6 ( x)",
        "0.13.6 (x )",
        "0.13.6 (x\ny)",
        "0.13.6 (x\ty)",
        "0.13.6 (nested(x))",
        "0.13.6 ($HOME)",
        "0.13.6 (`id`)",
        "0.13.6 (x;y)",
        "0.13.6 (x/y)",
        "0.13.6 (é)",
        "０.１３.６",
        "0.13.6 (" + "x" * 120 + ")",
    ],
)
def test_authorization_rejects_ambiguous_or_unbounded_installer_pin(tmp_path, version):
    _, _, authorization, _ = _fixture(tmp_path)
    raw = asdict(authorization)
    raw["uv_version"] = version
    with pytest.raises(EpochUnavailable, match="exact configured installer version"):
        EpochAuthorization.parse(raw)
    with pytest.raises(EpochUnavailable, match="exact configured installer version"):
        validate_uv_version(version)


@pytest.mark.parametrize("field,value", [("authorization_id", "other-review"), ("request_id", "other-request"), ("preparation_id", "other-preparation"), ("target_id", "f" * 64), ("deployment_id", "other-deployment"), ("enrollment_ids", ("another-scope",)), ("authorization_sha256", "f" * 64)])
def test_authorization_cannot_retarget_an_inventory(tmp_path, field, value):
    root, path, _, inventory = _fixture(tmp_path)
    before = sorted(item.name for item in root.iterdir())
    with pytest.raises(EpochUnavailable):
        _load_authorization(path, replace(inventory, **{field: value}))
    assert sorted(item.name for item in root.iterdir()) == before


def test_legacy_persistence_inventory_does_not_create_issuer_authority(tmp_path):
    _, path, _, inventory = _fixture(tmp_path)
    with pytest.raises(EpochUnavailable, match="version-two"):
        _load_authorization(path, replace(inventory, schema_version=1))


@pytest.mark.parametrize("change", ["partial", "duplicate", "noncanonical", "different-history", "unknown-field", "wrong-target-type"])
def test_authorization_schema_refuses_ambiguous_selection(tmp_path, change):
    _, path, authorization, inventory = _fixture(tmp_path)
    raw = asdict(authorization)
    if change == "partial":
        del raw["ready_receipt"]
    elif change == "different-history":
        raw["history"]["deployment"] = "elsewhere"
    elif change == "unknown-field":
        raw["inventory_root"] = "/tmp/another-epoch"
    elif change == "wrong-target-type":
        raw["target_id"] = True
    encoded = canonical(raw)
    if change == "duplicate":
        encoded = encoded[:-1] + b',"request_id":"request-A"}'
    elif change == "noncanonical":
        encoded = json.dumps(raw, indent=2).encode()
    path.unlink()
    _write(path, encoded)
    selected = replace(inventory, authorization_sha256=hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ValueError):
        _load_authorization(path, selected)


def test_version_two_retains_full_canonical_evidence_before_consumption(tmp_path):
    root, _, _, inventory = _fixture(tmp_path)
    binding = _binding(inventory)
    with _hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
        state.reserve(binding)
        state.record_intent(binding)
        receipt = state.consume(binding)
        state.check()
        assert state.descriptor >= 0
    for phase in ("reserved", "intent", "consumed", "receipt"):
        saved = json.loads((root / (phase + ".json")).read_bytes())["binding"]
        assert saved["evidence_json"] == binding.evidence_json
        assert hashlib.sha256(saved["evidence_json"].encode()).hexdigest() == saved["evidence_sha256"]
        assert saved["authorization_sha256"] == inventory.authorization_sha256
    assert receipt.grant_delivered is False
    with pytest.raises(EpochUnavailable):
        with _hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("a consumed version-two epoch was replayed")


@pytest.mark.parametrize("evidence", [b"", b"[]", b'{"x": 1}', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":"' + b"a" * (512 * 1024) + b'"}'])
def test_version_two_rejects_incomplete_or_ambiguous_evidence_before_reservation(tmp_path, evidence):
    root, _, _, inventory = _fixture(tmp_path)
    before = {item.name: item.read_bytes() for item in root.iterdir()}
    with _hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
        with pytest.raises(ValueError):
            state.reserve(_binding(inventory, evidence=evidence))
    assert {item.name: item.read_bytes() for item in root.iterdir()} == before


def test_retained_evidence_digest_mismatch_cannot_consume(tmp_path):
    root, _, _, inventory = _fixture(tmp_path)
    binding = _binding(inventory)
    with pytest.raises(EpochUnavailable, match="closed, failed or inherited"):
        with _hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
            state.reserve(binding)
            with pytest.raises(EpochUnavailable, match="digest"):
                state.record_intent(replace(binding, evidence_sha256="f" * 64))
            with pytest.raises(EpochUnavailable, match="closed, failed or inherited"):
                state.record_intent(binding)
    assert not (root / "consumed.json").exists()


@pytest.mark.parametrize("argument", ["--root", "--inventory", "--authorization", "--request"])
def test_host_cli_rejects_request_selected_authority_before_execution(tmp_path, argument):
    result = subprocess.run([sys.executable, "-m", "auto_coder.management.recovery_epoch", argument, str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert list(tmp_path.iterdir()) == []
