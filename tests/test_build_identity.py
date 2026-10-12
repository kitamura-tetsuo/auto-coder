from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.build_identity import (
    BuildIdentityError,
    deserialize_build_identity,
    resolve_build_identity,
    serialize_build_identity,
)

SCRIPT = Path(__file__).parents[1] / "scripts" / "build_version.py"
COMMIT_DATE = "2026-10-12T00:30:00+09:00"
pytestmark = pytest.mark.usefixtures("_use_real_commands")


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


def git_path(repo: Path, name: str) -> Path:
    path = Path(git(repo, "rev-parse", "--git-path", name))
    return path if path.is_absolute() else repo / path


def commit(repo: Path, message: str, filename: str, *, date: str = "2020-01-01T00:00:00Z") -> str:
    (repo / filename).write_text(message, encoding="utf-8")
    git(repo, "add", filename)
    environment = os.environ.copy()
    environment.update({"GIT_AUTHOR_DATE": "2019-02-03T04:05:06Z", "GIT_COMMITTER_DATE": date})
    git(repo, "commit", "-m", message, env=environment)
    return git(repo, "rev-parse", "HEAD")


def merge_repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    commit(repo, "A", "a.txt")
    git(repo, "checkout", "-b", "left")
    commit(repo, "B", "b.txt")
    git(repo, "checkout", "main")
    commit(repo, "C", "c.txt")
    environment = os.environ.copy()
    environment.update({"GIT_AUTHOR_DATE": "2021-04-05T06:07:08Z", "GIT_COMMITTER_DATE": COMMIT_DATE})
    git(repo, "merge", "--no-ff", "left", "-m", "D", env=environment)
    return repo, git(repo, "rev-parse", "HEAD")


def test_resolver_uses_all_merge_parents_utc_and_explicit_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, revision = merge_repository(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    monkeypatch.chdir(other)
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    monkeypatch.setenv("LC_ALL", "C")

    record = resolve_build_identity(repo)

    assert record.state == "clean"
    assert record.version == f"2026.10.11.4+g{revision}"
    assert record.source_revision == revision
    assert record.source_date_epoch == 1791732600
    assert record.revision_count == 4
    assert record.reason is None

    git(repo, "checkout", "--detach", revision)
    assert resolve_build_identity(repo) == record
    worktree = tmp_path / "linked"
    git(repo, "worktree", "add", "--detach", str(worktree), revision)
    assert resolve_build_identity(worktree) == record


@pytest.mark.parametrize("change", ["unstaged", "staged", "deleted", "untracked"])
def test_resolver_reports_dirty_tracked_and_untracked_changes(tmp_path: Path, change: str) -> None:
    repo, revision = merge_repository(tmp_path)
    if change == "unstaged":
        (repo / "a.txt").write_text("changed", encoding="utf-8")
    elif change == "staged":
        (repo / "a.txt").write_text("changed", encoding="utf-8")
        git(repo, "add", "a.txt")
    elif change == "deleted":
        (repo / "a.txt").unlink()
    else:
        (repo / "new.txt").write_text("new", encoding="utf-8")

    record = resolve_build_identity(repo)

    assert record.state == "dirty"
    assert record.version == f"2026.10.11.4+g{revision}.dirty"
    assert record.reason is None


def test_assume_unchanged_content_change_is_dirty_without_index_writeback(tmp_path: Path) -> None:
    repo, revision = merge_repository(tmp_path)
    git(repo, "update-index", "--assume-unchanged", "a.txt")
    index = git_path(repo, "index")
    before_index = index.read_bytes()
    (repo / "a.txt").write_text("changed despite index flag", encoding="utf-8")

    record = resolve_build_identity(repo)

    assert record.state == "dirty"
    assert record.version == f"2026.10.11.4+g{revision}.dirty"
    assert index.read_bytes() == before_index
    assert git(repo, "ls-files", "-v", "a.txt").startswith("h ")


def test_ignored_files_tags_refs_and_abbreviation_do_not_change_identity(tmp_path: Path) -> None:
    repo, _ = merge_repository(tmp_path)
    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    environment = os.environ.copy()
    environment.update({"GIT_AUTHOR_DATE": COMMIT_DATE, "GIT_COMMITTER_DATE": COMMIT_DATE})
    git(repo, "commit", "-m", "ignore generated", env=environment)
    expected = resolve_build_identity(repo)
    (repo / "generated").mkdir()
    (repo / "generated" / "build.log").write_text("ignored", encoding="utf-8")
    git(repo, "tag", "v999.0.0")
    git(repo, "config", "core.abbrev", "5")

    assert resolve_build_identity(repo) == expected


def test_unrelated_orphan_branch_growth_does_not_change_selected_identity(tmp_path: Path) -> None:
    repo, revision = merge_repository(tmp_path)
    expected = resolve_build_identity(repo)
    assert expected.revision_count == 4

    git(repo, "checkout", "--orphan", "unrelated")
    git(repo, "rm", "-rf", ".")
    commit(repo, "unrelated", "orphan.txt", date="2030-01-02T03:04:05Z")
    git(repo, "checkout", "--detach", revision)

    assert git(repo, "rev-list", "--count", "--all") == "5"
    assert resolve_build_identity(repo) == expected


def test_replacement_graft_does_not_change_actual_object_identity(tmp_path: Path) -> None:
    repo, revision = merge_repository(tmp_path)
    expected = resolve_build_identity(repo)
    root = git(repo, "rev-list", "--max-parents=0", revision)

    git(repo, "replace", "--graft", revision, root)

    assert git(repo, "rev-list", "--count", revision) == "2"
    assert resolve_build_identity(repo) == expected


def test_legacy_graft_does_not_change_actual_object_ancestry(tmp_path: Path) -> None:
    repo = tmp_path / "history"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    commit(repo, "A", "a.txt")
    revision = commit(repo, "S", "s.txt")
    expected = resolve_build_identity(repo)
    assert expected.revision_count == 2
    graft = git_path(repo, "info/grafts")
    graft.parent.mkdir(parents=True, exist_ok=True)
    graft.write_text(f"{revision}\n", encoding="ascii")
    before_graft = graft.read_bytes()

    assert git(repo, "rev-list", "--count", revision) == "1"
    assert resolve_build_identity(repo) == expected
    assert graft.read_bytes() == before_graft


def test_unknown_for_invalid_unborn_and_shallow_repositories(tmp_path: Path) -> None:
    missing = resolve_build_identity(tmp_path / "missing")
    assert missing.state == "unknown" and missing.version == "0+unknown" and missing.reason

    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init")
    assert resolve_build_identity(unborn).state == "unknown"

    source = tmp_path / "history"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test User")
    git(source, "config", "user.email", "test@example.com")
    commit(source, "one", "one.txt")
    revision = commit(source, "two", "two.txt")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth", "1", source.as_uri(), str(shallow)], check=True, capture_output=True, text=True)

    record = resolve_build_identity(shallow)
    assert record.state == "unknown"
    assert record.version == "0+unknown"
    assert record.source_revision == revision
    assert record.source_date_epoch is None
    assert record.revision_count is None
    assert record.reason and "shallow" in record.reason


def test_serializer_round_trip_and_rejects_inconsistent_records(tmp_path: Path) -> None:
    repo, _ = merge_repository(tmp_path)
    record = resolve_build_identity(repo)
    assert deserialize_build_identity(serialize_build_identity(record)) == record

    invalid = [
        "not json",
        json.dumps({}),
        serialize_build_identity(record).replace('"schema_version":1', '"schema_version":2'),
        serialize_build_identity(record).replace('"revision_count":4', '"revision_count":true'),
        serialize_build_identity(record).replace(record.source_revision or "", "ABC"),
    ]
    for payload in invalid:
        with pytest.raises(BuildIdentityError):
            deserialize_build_identity(payload)

    with pytest.raises(BuildIdentityError):
        serialize_build_identity(replace(record, revision_count=None))
    with pytest.raises(BuildIdentityError):
        serialize_build_identity(replace(record, state="dirty"))


def test_command_output_policy_and_exit_status(tmp_path: Path) -> None:
    repo, _ = merge_repository(tmp_path)
    before_status = git(repo, "status", "--porcelain=v1")
    before_head = git(repo, "rev-parse", "HEAD")
    external = tmp_path / "identity.json"

    result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--output", str(external)], capture_output=True, text=True)
    assert result.returncode == 0
    assert external.read_text(encoding="utf-8") == result.stdout
    assert deserialize_build_identity(result.stdout).state == "clean"
    assert git(repo, "status", "--porcelain=v1") == before_status
    assert git(repo, "rev-parse", "HEAD") == before_head

    tracked = repo / "a.txt"
    original = tracked.read_bytes()
    rejected = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--output", str(tracked)], capture_output=True, text=True)
    assert rejected.returncode == 2 and rejected.stdout == ""
    assert tracked.read_bytes() == original

    nonignored = repo / "identity.json"
    rejected = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--output", str(nonignored)], capture_output=True, text=True)
    assert rejected.returncode == 2 and not nonignored.exists()

    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore generated")
    ignored = repo / "generated" / "identity.json"
    accepted = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--output", str(ignored)], capture_output=True, text=True)
    assert accepted.returncode == 0
    assert ignored.read_text(encoding="utf-8") == accepted.stdout

    output_directory = tmp_path / "not-a-file"
    output_directory.mkdir()
    failed_write = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--output", str(output_directory)], capture_output=True, text=True)
    assert failed_write.returncode == 2
    assert failed_write.stdout == ""


def test_linked_worktree_rejects_common_git_ref_and_external_index_outputs(tmp_path: Path) -> None:
    repo, _ = merge_repository(tmp_path)
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "linked-branch", str(linked))
    common_dir = Path(git(linked, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = linked / common_dir
    branch_ref = common_dir / "refs/heads/main"
    linked_index = git_path(linked, "index")

    for protected in (branch_ref, linked_index):
        before = protected.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(linked), "--output", str(protected)], capture_output=True, text=True)
        assert result.returncode == 2
        assert result.stdout == ""
        assert protected.read_bytes() == before


def test_resolver_and_rejected_output_do_not_refresh_index_stat_cache(tmp_path: Path) -> None:
    repo, _ = merge_repository(tmp_path)
    git(repo, "update-index", "--refresh")
    index = git_path(repo, "index")
    before_index = index.read_bytes()
    tracked = repo / "a.txt"
    original_stat = tracked.stat()
    os.utime(tracked, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 5_000_000_000))

    assert resolve_build_identity(repo).state == "clean"
    assert index.read_bytes() == before_index

    rejected = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--output", str(repo / "not-ignored.json")], capture_output=True, text=True)
    assert rejected.returncode == 2
    assert rejected.stdout == ""
    assert index.read_bytes() == before_index


def test_command_writes_unknown_record_with_nonzero_status(tmp_path: Path) -> None:
    output = tmp_path / "unknown.json"
    result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(tmp_path / "absent"), "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 1
    assert output.read_text(encoding="utf-8") == result.stdout
    assert deserialize_build_identity(result.stdout).state == "unknown"
