"""Real-container regression for retained startup, fixture identity and owner locking."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from scripts.prepare_management_test_image import verify_image

from auto_coder.management.operation_store import ManagementOperationStore


def _fixture_image() -> str:
    image = os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:management-local-abc65544")
    if "AUTOCODER_FRESH_TEST_BASE_IMAGE" in os.environ:
        # CI exports an immutable image ID after checked-source preparation.
        verify_image(Path(__file__).resolve().parents[1], image)
    return image


def _exercise_real_retained_startup(tmp_path: Path, *, source: Path, image: str) -> None:
    root = tmp_path / "old"
    ManagementOperationStore(root).get("initialize")
    root.chmod(0o755)
    database = root / "management-operations.sqlite3"
    database.chmod(0o644)
    fence = tmp_path / "owner.lock"
    fence.touch(mode=0o644)
    db, lock = database.stat(), fence.stat()
    code = """
import fcntl, os
from pathlib import Path
from types import SimpleNamespace as N
import auto_coder.management.retained_startup as startup
runtime=N(reference='bootstrap',executable=Path('/some/executable'))
bootstrap=N(instance_id='new', state_root=Path('/state'), journal_root=Path('/new-operations'), oauth_root=Path('/oauth'), fresh_prior_lock_path=Path('/management-prior-owner.lock'), runtimes=(runtime,), archived_exclusions=(), fresh_runtime_binding=None)
intent=N(new_instance='new', state_root='/state', journal_root='/new-operations', oauth_root='/oauth',
 runtime_reference='bootstrap', host_prior_journal_root='/old', prior_journal_root='/management-prior-operations',
 prior_journal_device=int(os.environ['DB_DEVICE']), prior_journal_inode=int(os.environ['DB_INODE']),
 owner_lock_device=int(os.environ['LOCK_DEVICE']), owner_lock_inode=int(os.environ['LOCK_INODE']), old_instance='old')
settings=N(bootstrap_config_path=Path('/bootstrap/config.json'), config_path=Path('/management/config.json'), state_root=Path('/oauth'), operation_state_root=Path('/new-operations'))
startup.BootstrapConfig.read=lambda _: bootstrap
startup.load_fresh_initialization=lambda _: (intent,'digest')
startup.verify_initialization_inputs=lambda *a,**k: None
reader=startup.retained_from_fresh_intent(settings)
assert reader is not None and reader.read() == ()
fd=os.open('/management-prior-owner.lock', os.O_RDONLY)
try:
 try:
  fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
  raise AssertionError('old owner fence did not exclude another owner')
 except BlockingIOError: pass
finally: os.close(fd)
reader.close()
print('readonly mount, identity and prior-owner fence verified')
"""
    command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--entrypoint",
        "/opt/auto-coder/.venv/bin/python",
        "-v",
        f"{source}:/source:ro",
        "-v",
        f"{root}:/management-prior-operations:ro",
        "-v",
        f"{fence}:/management-prior-owner.lock:ro",
        "-e",
        "PYTHONPATH=/source",
        "-e",
        f"DB_DEVICE={db.st_dev}",
        "-e",
        f"DB_INODE={db.st_ino}",
        "-e",
        f"LOCK_DEVICE={lock.st_dev}",
        "-e",
        f"LOCK_INODE={lock.st_ino}",
        image,
        "-c",
        code,
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "readonly mount, identity and prior-owner fence verified" in result.stdout
    wal = database.with_name(database.name + "-wal")
    wal.write_bytes(b"uncheckpointed-evidence")
    refused = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0 and "write-ahead log is not proven empty" in refused.stderr
    wal.unlink()
    writable = list(command)
    mount_index = writable.index(f"{root}:/management-prior-operations:ro")
    writable[mount_index] = f"{root}:/management-prior-operations:rw"
    refused = subprocess.run(writable, check=False, capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0, "writable retained-journal mount was accepted"
    assert "retained journal mount is not read-only" in refused.stderr


@pytest.mark.supervisor_integration
def test_retained_startup_uses_real_readonly_bind_and_shared_owner_lock(tmp_path: Path, _use_real_commands) -> None:
    _exercise_real_retained_startup(
        tmp_path,
        source=Path(__file__).resolve().parents[1] / "src",
        image=_fixture_image(),
    )


@pytest.mark.supervisor_integration
def test_retained_startup_detects_removed_readonly_guard(tmp_path: Path, _use_real_commands) -> None:
    """A disposable source mutation must make the unchanged real-container oracle fail."""
    source = tmp_path / "mutated-source"
    shutil.copytree(Path(__file__).resolve().parents[1] / "src", source)
    retained = source / "auto_coder" / "management" / "retained_startup.py"
    original = "if root.is_symlink() or not root.is_dir() or not os.statvfs(root).f_flag & os.ST_RDONLY:"
    assert retained.read_text().count(original) == 1
    retained.write_text(retained.read_text().replace(original, "if root.is_symlink() or not root.is_dir():"))
    mutable_probe = tmp_path / "probe"
    mutable_probe.mkdir()
    with pytest.raises(AssertionError, match="writable retained-journal mount was accepted"):
        _exercise_real_retained_startup(mutable_probe, source=source, image=_fixture_image())
