"""Real filesystem/process regressions for the read-only draft boundary."""

import hashlib
import json
import os
import select
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.management.fresh_preflight import assess_new_instance, revalidate_new_instance

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@pytest.fixture
def request_file(tmp_path: Path) -> Path:
    for name in ("state", "operations", "guard", "oauth", "app"):
        (tmp_path / name).mkdir(mode=0o700)
    (tmp_path / "oauth/original.sqlite3").write_bytes(b"original OAuth fixture bytes; never open as a database")
    (tmp_path / "oauth/grants").write_bytes(b"unchanged grants")
    # This executable would leave an observable effect if incorrectly invoked.
    executable = tmp_path / "executable"
    executable.write_text(f"#!/bin/sh\ntouch '{tmp_path / 'LAUNCHED'}'\n")
    executable.chmod(0o700)
    for name in ("bootstrap.json", "management.yaml", "app/main.py"):
        (tmp_path / name).write_text("original\n")
        (tmp_path / name).chmod(0o600)
    request = {
        "schema_version": 1,
        "purpose": "NEW",
        "request_id": "00000000-0000-4000-8000-000000000001",
        "new_instance": "fixture-new",
        "runtime_reference": "fixture-runtime",
        "state_root": str(tmp_path / "state"),
        "operation_root": str(tmp_path / "operations"),
        "guard_root": str(tmp_path / "guard"),
        "oauth_root": str(tmp_path / "oauth"),
        "bootstrap_config": str(tmp_path / "bootstrap.json"),
        "management_config": str(tmp_path / "management.yaml"),
        "executable": str(executable),
        "application_root": str(tmp_path / "app"),
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request, indent=2) + "\n")
    path.chmod(0o600)
    return path


def _change(path: Path, **fields: object) -> None:
    raw = json.loads(path.read_bytes())
    raw.update(fields)
    path.write_text(json.dumps(raw))


def _bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def _environment() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"}


def _command(path: Path) -> list[str]:
    return [sys.executable, "-B", "-m", "auto_coder.management.fresh_host", "preflight", "--request-file", str(path)]


def test_host_boundary_preserves_original_bytes_and_reports_no_authority(request_file: Path) -> None:
    before = _bytes(request_file.parent)
    result = subprocess.run(_command(request_file), env=_environment(), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["status"] == "assessed-only"
    assert observed["launch_authorized"] is False
    assert observed["original_request"]["sha256"] == hashlib.sha256(before["request.json"]).hexdigest()
    assert observed["original_request"]["inode"] == request_file.stat().st_ino
    assert [entry["mode"] for entry in observed["roots"][:3]] == [0o700] * 3
    assert _bytes(request_file.parent) == before
    assert all(list((request_file.parent / name).iterdir()) == [] for name in ("state", "operations", "guard"))
    assessment = assess_new_instance(request_file)
    revalidate_new_instance(assessment)
    assert json.loads(json.dumps(asdict(assessment))) == observed


@pytest.mark.parametrize("fault", ["state_mode", "guard_mode", "request_mode", "config_mode", "root_symlink", "ancestor_symlink", "config_symlink", "app_symlink", "existing", "partial", "overlap", "missing", "recovery", "unknown", "duplicate", "bool_schema", "fifo"])
def test_real_host_refusals_preserve_existing_records(request_file: Path, fault: str) -> None:
    root = request_file.parent
    if fault in {"state_mode", "guard_mode"}:
        (root / fault.removesuffix("_mode")).chmod(0o750)
    elif fault == "request_mode":
        request_file.chmod(0o644)
    elif fault == "config_mode":
        (root / "bootstrap.json").chmod(0o640)
    elif fault == "root_symlink":
        (root / "state").rmdir()
        (root / "state").symlink_to(root / "operations", target_is_directory=True)
    elif fault == "ancestor_symlink":
        (root / "alias").symlink_to(root, target_is_directory=True)
        _change(request_file, state_root=str(root / "alias/state"))
    elif fault in {"config_symlink", "app_symlink"}:
        path = root / ("bootstrap.json" if fault == "config_symlink" else "app/main.py")
        path.unlink()
        path.symlink_to(root / "management.yaml")
    elif fault in {"existing", "partial"}:
        (root / "state" / ("management-child.sqlite3" if fault == "existing" else "fresh-launch-claimed.json")).write_bytes(b"original unresolved record")
    elif fault == "overlap":
        _change(request_file, guard_root=str(root / "oauth/subdir"))
    elif fault == "missing":
        (root / "operations").rmdir()
    elif fault == "recovery":
        _change(request_file, purpose="RECOVER")
    elif fault == "unknown":
        _change(request_file, authorize=True)
    elif fault == "duplicate":
        request_file.write_text(request_file.read_text().replace('"purpose": "NEW"', '"purpose": "NEW", "purpose": "NEW"'))
    elif fault == "bool_schema":
        _change(request_file, schema_version=True)
    elif fault == "fifo":
        (root / "bootstrap.json").unlink()
        os.mkfifo(root / "bootstrap.json", 0o600)
    before = _bytes(root) if fault not in {"ancestor_symlink", "fifo"} else None
    result = subprocess.run(_command(request_file), env=_environment(), capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "fresh host observation unavailable:" in result.stderr
    if before is not None:
        assert _bytes(root) == before
    assert not (root / "LAUNCHED").exists()
    assert (root / "oauth/grants").read_bytes() == b"unchanged grants"


@pytest.mark.parametrize("changed", ["request", "request_whitespace", "bootstrap", "management", "executable", "application", "root_identity", "root_mode", "new_record"])
def test_revalidation_refuses_changed_original_identity(request_file: Path, changed: str) -> None:
    assessment = assess_new_instance(request_file)
    root = request_file.parent
    if changed == "request":
        _change(request_file, runtime_reference="different")
    elif changed == "request_whitespace":
        request_file.write_bytes(request_file.read_bytes() + b" ")
    elif changed in {"bootstrap", "management", "executable", "application"}:
        path = root / {"bootstrap": "bootstrap.json", "management": "management.yaml", "executable": "executable", "application": "app/main.py"}[changed]
        path.write_bytes(path.read_bytes() + b"changed")
    elif changed == "root_identity":
        (root / "state").rename(root / "original-state")
        (root / "state").mkdir(mode=0o700)
    elif changed == "root_mode":
        (root / "guard").chmod(0o755)
    else:
        (root / "operations/interrupted").write_bytes(b"preserve me")
    before = _bytes(root)
    with pytest.raises((ValueError, OSError)):
        revalidate_new_instance(assessment)
    assert _bytes(root) == before
    assert not (root / "LAUNCHED").exists()


# Audit hooks pause an actual process at its real configuration open. They do
# not replace filesystem results or turn this assessment into launch admission.
_PAUSED = """
import os, sys
from pathlib import Path
from auto_coder.management.fresh_preflight import assess_new_instance
request = Path(sys.argv[1])
def barrier(event, args):
    if event == 'open' and args[0] == 'bootstrap.json':
        print('configuration-open', flush=True)
        sys.stdin.read(1)
sys.addaudithook(barrier)
assessment = assess_new_instance(request)
print(assessment.status, flush=True)
"""


def _paused(path: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen([sys.executable, "-B", "-c", _PAUSED, str(path)], env=_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 10)[0], "assessment did not reach configuration-open barrier"
    assert process.stdout.readline() == "configuration-open\n"
    return process


def test_overlapping_assessments_and_killed_scan_never_claim_or_launch(request_file: Path) -> None:
    before = _bytes(request_file.parent)
    first = _paused(request_file)
    second = None
    try:
        second = _paused(request_file)
        assert first.poll() is None and second.poll() is None
        first.kill()
        first.communicate(timeout=5)
        assert first.returncode == -9
        stdout, stderr = second.communicate("xxxx", timeout=10)
        assert second.returncode == 0, stderr
        assert stdout.endswith("assessed-only\n")
        # Reconstruction remains assessment only; no durable claim was created.
        replay = subprocess.run(_command(request_file), env=_environment(), capture_output=True, text=True, timeout=10)
        assert replay.returncode == 0, replay.stderr
        assert json.loads(replay.stdout)["launch_authorized"] is False
        assert _bytes(request_file.parent) == before
        assert all(list((request_file.parent / name).iterdir()) == [] for name in ("state", "operations", "guard"))
    finally:
        for process in (first, second):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


def test_root_replacement_during_real_scan_refuses(request_file: Path) -> None:
    process = _paused(request_file)
    root = request_file.parent
    try:
        (root / "guard").rename(root / "original-guard")
        (root / "guard").mkdir(mode=0o700)
        stdout, stderr = process.communicate("xxxx", timeout=10)
        assert process.returncode != 0
        assert "request or roots changed during assessment" in stderr
        assert "assessed-only" not in stdout
        assert not (root / "LAUNCHED").exists()
        assert list((root / "original-guard").iterdir()) == []
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("target", ["state", "bootstrap.json", "app/main.py"])
def test_kernel_read_denial_refuses_without_permission_repair(request_file: Path, target: str) -> None:
    path = request_file.parent / target
    original_mode = path.stat().st_mode & 0o777
    before = _bytes(request_file.parent)
    path.chmod(0)
    try:
        result = subprocess.run(_command(request_file), env=_environment(), capture_output=True, text=True, timeout=10)
        assert result.returncode == 1
        assert "Permission denied" in result.stderr
        assert result.stdout == ""
        assert path.stat().st_mode & 0o777 == 0
        assert not (request_file.parent / "LAUNCHED").exists()
    finally:
        path.chmod(original_mode)
    assert _bytes(request_file.parent) == before
