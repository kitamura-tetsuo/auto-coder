from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.worker_versions import (
    InstalledProvenance,
    PreparationFailure,
    PrepareIntent,
    ReadyReceipt,
    WorkerVersionConfig,
    WorkerVersionError,
    WorkerVersionPreparer,
)


def _config(tmp_path: Path) -> WorkerVersionConfig:
    return WorkerVersionConfig(tmp_path / "repo", tmp_path / "versions", Path("/python"), Path("/uv"), "1.2.3")


@pytest.mark.parametrize("sha,attempt", [("main", "a"), ("a" * 39, "a"), ("A" * 40, "a"), ("a" * 40, "../escape")])
def test_untrusted_intent_accepts_only_a_full_sha_and_safe_attempt(tmp_path: Path, sha: str, attempt: str) -> None:
    with pytest.raises(WorkerVersionError) as caught:
        WorkerVersionPreparer._validate_intent(PrepareIntent(sha, attempt))
    assert caught.value.failure is PreparationFailure.INVALID_REQUEST


def test_ready_publication_is_atomic_and_schema_is_bounded(tmp_path: Path) -> None:
    target = tmp_path / "READY.json"
    provenance = InstalledProvenance("a" * 40, "1.0")
    receipt = ReadyReceipt(1, "a" * 40, "a" * 40, "/v", "/v/bin/python", "1.0", "b" * 64, "attempt", "python-3.12", "opaque", "c" * 64, provenance, "2026-01-01T00:00:00+00:00")

    WorkerVersionPreparer._publish(target, receipt)

    assert json.loads(target.read_text()) == asdict(receipt)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "command,diagnostic,expected",
    [
        (["uv", "sync"], "No space left on device", PreparationFailure.INSUFFICIENT_SPACE),
        (["git", "fetch"], "fatal: Authentication failed", PreparationFailure.AUTHENTICATION_UNAVAILABLE),
        (["git", "fetch"], "Could not resolve host", PreparationFailure.NETWORK_UNAVAILABLE),
        (["/usr/local/bin/uv", "sync"], "failed to build native wheel", PreparationFailure.INCOMPATIBLE_DEPENDENCIES),
    ],
)
def test_process_failures_have_safe_typed_classifications(command: list[str], diagnostic: str, expected: PreparationFailure) -> None:
    assert WorkerVersionPreparer._classify_process_failure(command, diagnostic) is expected


def test_checkout_uses_argument_vectors_and_rejects_non_exact_resolution(tmp_path: Path) -> None:
    preparer = WorkerVersionPreparer(_config(tmp_path))
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: object) -> str:
        calls.append(arguments)
        if "rev-parse" in arguments:
            return "b" * 40 + "\n"
        return ""

    preparer._run = run  # type: ignore[method-assign]
    with pytest.raises(WorkerVersionError) as caught:
        preparer._checkout(tmp_path / "checkout", "a" * 40)
    assert caught.value.failure is PreparationFailure.VERIFICATION_MISMATCH
    assert all(isinstance(call, list) for call in calls)
    assert calls[0][:3] == ["git", "clone", "--no-checkout"]


def test_receipt_content_identity_binds_lock_runtime_and_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    attempt = config.version_root / "revisions" / ("k" * 64) / "attempt"
    environment = attempt / "venv"
    interpreter = environment / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("executable")
    os.chmod(interpreter, 0o755)
    source = attempt / "source"
    source.mkdir()
    lock = b"locked-input"
    (source / "uv.lock").write_bytes(lock)
    observed = InstalledProvenance("a" * 40, "1.0")
    bindings = {
        "attempt_id": "attempt",
        "runtime_profile": "python-3.12",
        "environment_path": str(environment.resolve()),
        "interpreter_path": str(interpreter.resolve()),
        "source_sha": "a" * 40,
        "distribution_version": "1.0",
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "installed_runtime_id": "opaque",
    }
    identity = hashlib.sha256(json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt = ReadyReceipt(1, "a" * 40, "a" * 40, bindings["environment_path"], bindings["interpreter_path"], "1.0", bindings["lock_sha256"], "attempt", "python-3.12", "opaque", identity, observed, "now")
    ready = attempt / "READY.json"
    WorkerVersionPreparer._publish(ready, receipt)
    preparer = WorkerVersionPreparer(config)
    preparer._observe = lambda _environment: observed  # type: ignore[method-assign]
    preparer._interpreter_prefix = lambda _interpreter: environment.resolve()  # type: ignore[method-assign]

    assert preparer.read_ready(ready) == receipt
    (source / "uv.lock").write_bytes(b"substituted")
    with pytest.raises(WorkerVersionError) as caught:
        preparer.read_ready(ready)
    assert caught.value.failure is PreparationFailure.VERIFICATION_MISMATCH


@pytest.mark.parametrize("bad_value", [["a" * 40], {"sha": "a" * 40}, 7, None])
def test_corrupt_ready_field_types_are_typed_and_never_reused(tmp_path: Path, bad_value: object) -> None:
    config = _config(tmp_path)
    ready = config.version_root / "revisions" / ("k" * 64) / "attempt" / "READY.json"
    ready.parent.mkdir(parents=True)
    provenance = InstalledProvenance("a" * 40, "1.0")
    receipt = ReadyReceipt(1, "a" * 40, "a" * 40, "/v", "/v/bin/python", "1.0", "b" * 64, "attempt", "python-3.12", "opaque", "c" * 64, provenance, "2026-01-01T00:00:00+00:00")
    payload = asdict(receipt)
    payload["source_sha"] = bad_value
    ready.write_text(json.dumps(payload))

    with pytest.raises(WorkerVersionError) as caught:
        WorkerVersionPreparer(config).read_ready(ready)

    assert caught.value.failure is PreparationFailure.CORRUPT_READY
    assert json.loads(ready.read_text())["source_sha"] == bad_value


def test_configured_repository_supplies_exact_revision_lock_identity(tmp_path: Path, _use_real_commands: None) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "uv.lock").write_text("revision-one")
    subprocess.run(["git", "-C", str(repo), "add", "uv.lock"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    first = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (repo / "uv.lock").write_text("revision-two")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "two"], check=True)

    assert WorkerVersionPreparer._run_bytes(["git", "-C", str(repo), "show", f"{first}:uv.lock"]) == b"revision-one"
