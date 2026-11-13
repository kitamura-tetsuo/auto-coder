"""Bounded guards for the separate real-VM second-verifier mutation control.

These checks validate transformation and result parsing, not native acceptance.
The dedicated disposable VM must execute the same no-grant oracle with genuine
sources, original records, Docker processes and actual queued receiver bytes.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from scripts import recovery_epoch_mutation_control as control

from auto_coder.management import recovery_epoch


def _original() -> bytes:
    return Path(recovery_epoch.__file__).read_bytes()


def test_real_issuer_mutation_removes_only_the_second_post_consumption_verifier() -> None:
    source = _original()
    mutant, line = control.omit_post_consumption_verifier(source)
    assert source.splitlines()[line - 1].strip() == b"final_verification()"
    assert mutant == b"".join(source.splitlines(keepends=True)[: line - 1] + source.splitlines(keepends=True)[line:])
    function = next(node for node in ast.parse(mutant).body if isinstance(node, ast.FunctionDef) and node.name == "issue_epoch_once")
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "final_verification"]
    assert len(calls) == 1
    consume = next(node for node in ast.walk(function) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "consume")
    assert calls[0].lineno < consume.lineno
    assert b"session.check(custody)" in mutant and b"session.connection.sendall(packet)" in mutant


@pytest.mark.parametrize("change", ["already-removed", "extra-call", "wrong-consume", "argument", "wrong-function"])
def test_transform_refuses_other_issuer_shapes(change: str) -> None:
    source = _original()
    if change == "already-removed":
        source = control.omit_post_consumption_verifier(source)[0]
    elif change == "extra-call":
        source = source.replace(b"        final_verification()", b"        final_verification()\n        final_verification()", 1)
    elif change == "wrong-consume":
        source = source.replace(b"receipt = state.consume(binding)", b"receipt = state.reserve(binding)")
    elif change == "argument":
        source = source.replace(b"        final_verification()", b"        final_verification(binding)", 1)
    else:
        source = source.replace(b"def issue_epoch_once(", b"def another_issuer(")
    with pytest.raises(ValueError):
        control.omit_post_consumption_verifier(source)


def _copy(tmp_path: Path) -> tuple[Path, Path, Path]:
    source, destination, evidence = tmp_path / "original", tmp_path / "control", tmp_path / "mutation-source.json"
    selected = source / control.ISSUER_SOURCE
    selected.parent.mkdir(parents=True)
    selected.write_bytes(_original())
    (source / "unchanged.txt").write_bytes(b"original project bytes")
    (source / ".venv").mkdir()
    (source / ".venv" / "not-copied").write_bytes(b"environment")
    return source, destination, evidence


def test_private_copy_retains_original_and_all_other_project_files(tmp_path: Path) -> None:
    source, destination, evidence = _copy(tmp_path)
    original = (source / control.ISSUER_SOURCE).read_bytes()
    value = control.prepare(source, destination, evidence)
    assert (source / control.ISSUER_SOURCE).read_bytes() == original
    assert (destination / "unchanged.txt").read_bytes() == b"original project bytes"
    assert not (destination / ".venv").exists()
    observed, digest = control.verify_source_evidence(evidence)
    assert observed == value and digest == hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert value["original_source_sha256"] != value["mutant_source_sha256"]
    assert value["original_project_sha256"] != value["mutant_project_sha256"]
    assert (evidence.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("target", ["source", "copy", "evidence"])
def test_source_evidence_refuses_changed_original_mutant_or_claim(tmp_path: Path, target: str) -> None:
    source, destination, evidence = _copy(tmp_path)
    control.prepare(source, destination, evidence)
    if target == "evidence":
        value = json.loads(evidence.read_bytes())
        value["first_verifier_retained"] = False
        evidence.write_bytes(control._canonical(value))
    else:
        path = (source if target == "source" else destination) / control.ISSUER_SOURCE
        path.write_bytes(path.read_bytes() + b"\n# changed original source\n")
    with pytest.raises(AssertionError):
        control.verify_source_evidence(evidence)


def test_copy_refuses_existing_destination_without_changing_it(tmp_path: Path) -> None:
    source, destination, evidence = _copy(tmp_path)
    destination.mkdir()
    retained = destination / "retained"
    retained.write_bytes(b"existing")
    with pytest.raises(ValueError, match="new separate"):
        control.prepare(source, destination, evidence)
    assert retained.read_bytes() == b"existing" and not evidence.exists()


def _empty_dispatch() -> dict[str, object]:
    return {"schema": "actual-recovery-post-receipt-dispatch-v1", "receiver_stopped": True, "queue_empty_before_resume": True, "socket_flags_unchanged": True, "queue_bytes_observed": 0, "queued_grant": None, "issuer_returncode": 2}


def test_correct_post_receipt_refusal_passes_the_shared_oracle() -> None:
    control.assert_post_receipt_refusal(_empty_dispatch())


@pytest.mark.parametrize("field,value", [("issuer_returncode", None), ("issuer_returncode", 0), ("issuer_returncode", -9), ("queue_bytes_observed", 1), ("receiver_stopped", False), ("queue_empty_before_resume", False), ("socket_flags_unchanged", False)])
def test_generic_failure_kill_timeout_or_unsupported_queue_is_not_mutation_detection(field: str, value: object) -> None:
    observation = _empty_dispatch()
    observation[field] = value
    with pytest.raises(AssertionError) as caught:
        control.assert_post_receipt_refusal(observation)
    assert type(caught.value) is not control.PostReceiptGrantDispatched


def _queued_dispatch() -> dict[str, object]:
    issuer = {"issuer_pid": 123, "issuer_start_token": "345", "target_pid": 678, "target_start_token": "901", "peer_credentials": {"pid": 123, "uid": 0, "gid": 0}}
    grant = {
        "schema": "management-recovery-epoch-grant-v1",
        "granted": "recovery-epoch-consumed",
        "request_id": "fixture-request",
        "authorization_id": "fixture-authorization",
        "target_id": "a" * 64,
        "challenge": "b" * 64,
        "receipt_nonce": "c" * 64,
        "inventory_sha256": "d" * 64,
        "binding_sha256": "e" * 64,
        "consumed_sha256": "f" * 64,
        "receipt_sha256": "0" * 64,
        "host_boot_id": "actual-component-boot",
        "host_pid": issuer["issuer_pid"],
        "host_start_token": issuer["issuer_start_token"],
    }
    encoded = control._canonical(grant) + b"\n"
    queued = {
        **issuer,
        "schema": "actual-recovery-queued-grant-v1",
        "grant_schema": grant["schema"],
        "target_id": grant["target_id"],
        "grant": grant,
        "bytes_observed": len(encoded),
        "message_sha256": hashlib.sha256(encoded).hexdigest(),
        "socket_flags_before": 2,
        "socket_flags_after": 2,
        "socket_inode": 77,
        "peek_only": True,
        "socket_flags_unchanged": True,
        "bytes_still_queued": True,
        "receiver_stopped": True,
        "issuer_alive": True,
    }
    return {**_empty_dispatch(), **issuer, "queued_grant": queued, "queue_bytes_observed": len(encoded), "issuer_returncode": None}


def test_only_exact_original_queued_grant_triggers_named_native_failure() -> None:
    with pytest.raises(control.PostReceiptGrantDispatched, match=control.ORACLE):
        control.assert_post_receipt_refusal(_queued_dispatch())


@pytest.mark.parametrize("mutation", ["digest", "length", "foreign-issuer", "dead-issuer", "consumed-bytes", "wrong-lifetime", "modified-flags"])
def test_nonmatching_or_incomplete_dispatch_evidence_cannot_count_as_mutation_detection(mutation: str) -> None:
    observation = _queued_dispatch()
    queued = observation["queued_grant"]
    if mutation == "digest":
        queued["message_sha256"] = "f" * 64
    elif mutation == "length":
        queued["bytes_observed"] += 1
    elif mutation == "foreign-issuer":
        queued["peer_credentials"] = {"pid": 999, "uid": 0, "gid": 0}
    elif mutation == "dead-issuer":
        queued["issuer_alive"] = False
    elif mutation == "consumed-bytes":
        queued["bytes_still_queued"] = False
    elif mutation == "wrong-lifetime":
        queued["issuer_start_token"] = "987"
    else:
        queued["socket_flags_after"] += 1
    with pytest.raises(AssertionError) as caught:
        control.assert_post_receipt_refusal(observation)
    assert type(caught.value) is not control.PostReceiptGrantDispatched
