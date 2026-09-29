import hashlib
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.auto_coder.repository_readiness import (
    RepositoryReadinessError,
    validate_codex_effective_directory,
    verify_worker_repository,
)


def _worker_identity() -> tuple[int, int]:
    if os.geteuid() != 0:
        pytest.skip("credential-transition regression requires a root controller")
    nobody = pwd.getpwnam("nobody")
    return nobody.pw_uid, nobody.pw_gid


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["/usr/bin/git", "init", "-q", str(path)], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "marker.txt").write_text("private marker\n")
    subprocess.run(["/usr/bin/git", "-C", str(path), "add", "marker.txt"], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(path), "commit", "-qm", "initial"], check=True)
    return path


@pytest.fixture
def private_parent() -> Path:
    path = Path(tempfile.mkdtemp(prefix="auto-coder-readiness-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_worker_probe_rejects_controller_visible_repository_under_inaccessible_parent(private_parent: Path) -> None:
    uid, gid = _worker_identity()
    repository = _repository(private_parent / "result")
    private_parent.chmod(0o700)

    with pytest.raises(RepositoryReadinessError, match="worker repository probe failed"):
        verify_worker_repository(repository, worker_uid=uid, worker_gid=gid, environment=os.environ)


def test_worker_probe_establishes_private_root_and_tracked_file_readability(private_parent: Path) -> None:
    uid, gid = _worker_identity()
    repository = _repository(private_parent / "result")
    for current, directories, files in os.walk(private_parent):
        os.chown(current, uid, gid)
        for name in (*directories, *files):
            os.chown(Path(current) / name, uid, gid, follow_symlinks=False)

    evidence = verify_worker_repository(repository, worker_uid=uid, worker_gid=gid, environment=os.environ)

    assert evidence.worker_uid == uid
    assert evidence.worker_gid == gid
    assert evidence.root == repository.resolve()
    assert evidence.git_dir == (repository / ".git").resolve()
    assert evidence.common_dir == (repository / ".git").resolve()
    assert len(evidence.head) == 40
    assert evidence.readable_regular_files == 1
    expected = hashlib.sha256(b"marker.txt\0private marker\n").hexdigest()
    assert evidence.tracked_contents_checksum == expected


def test_worker_probe_rejects_head_whose_commit_object_is_missing(private_parent: Path) -> None:
    uid, gid = _worker_identity()
    repository = _repository(private_parent / "result")
    head = subprocess.run(
        ["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / ".git" / "objects" / head[:2] / head[2:]).unlink()
    for current, directories, files in os.walk(private_parent):
        os.chown(current, uid, gid)
        for name in (*directories, *files):
            os.chown(Path(current) / name, uid, gid, follow_symlinks=False)

    with pytest.raises(RepositoryReadinessError, match="git cat-file: could not get object info"):
        verify_worker_repository(repository, worker_uid=uid, worker_gid=gid, environment=os.environ)


@pytest.mark.parametrize(
    "arguments",
    [
        ["exec", "--cd", "../peer", "-"],
        ["exec", "--cd=../peer", "-"],
        ["exec", "-C=../peer", "-"],
        ["exec", "-C../peer", "-"],
        ["exec", "-C", "../peer", "-"],
    ],
)
def test_codex_directory_options_cannot_redirect_from_bound_workspace(tmp_path: Path, arguments: list[str]) -> None:
    workspace = _repository(tmp_path / "workspace")
    (tmp_path / "peer").mkdir()

    with pytest.raises(RepositoryReadinessError, match="does not match bound private result root"):
        validate_codex_effective_directory(arguments, workspace)


def test_codex_directory_option_may_explicitly_select_bound_workspace(tmp_path: Path) -> None:
    workspace = _repository(tmp_path / "workspace")

    validate_codex_effective_directory(["exec", "--cd", str(workspace), "-"], workspace)
