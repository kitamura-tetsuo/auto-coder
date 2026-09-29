"""Production adoption of repository-specific pending-work storage (Issue #2377).

These tests use a temporary HOME with the normal storage factories, the real
engine entry points, and the shipped pending-work / pr-repair CLI. No test
supplies a preselected store to the code under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from auto_coder import github_pending_work as gpw
from auto_coder.automation_config import AutomationConfig, CandidateProcessingResult, ExplicitTargetOutcome
from auto_coder.automation_engine import ISSUE_PROCESSING_STAGE, AutomationEngine, _issue_content_revision
from auto_coder.cli import main
from auto_coder.durable_repair_allowance import RepairAllowanceLedger
from auto_coder.github_pending_work import (
    PendingWorkNotReadyError,
    PendingWorkPersistenceError,
    PendingWorkStore,
    StageOutcome,
    WorkIdentity,
    default_pending_work_path,
    get_pending_work_store,
    repository_pending_work_path,
)
from auto_coder.pr_processor import PR_PROCESSING_REFRESH_EFFECT, PR_PROCESSING_STAGE
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubResponseMetadata,
    RequestProvenance,
)

R = "acme/alpha"
S = "acme/beta"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gpw, "_READY_STORES", {})
    assert Path.home() == tmp_path
    return tmp_path


def _throttle() -> GitHubRequestError:
    outcome = GitHubRequestOutcome(
        GitHubRequestContext("op", "attempt", "test", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}"),
        403,
        GitHubApiOutcome.PRIMARY_THROTTLED,
        RequestProvenance.NETWORK,
        DeliveryCertainty.HTTP_RESPONSE_RECEIVED,
        GitHubResponseMetadata(retry_after_seconds=5),
        1,
    )
    return GitHubRequestError(outcome)


def _legacy_store() -> PendingWorkStore:
    return PendingWorkStore(default_pending_work_path())


def _identity(repo: str, number: int = 5411, stage: str = ISSUE_PROCESSING_STAGE, revision: str = "rev") -> WorkIdentity:
    return WorkIdentity(repo, f"issue:{number}", stage, revision)


def _rows(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT repository,entity,stage,revision,reason,not_before,unfinished_effects,throttle_attempts,last_error,status,updated_at FROM github_pending_work ORDER BY work_key").fetchall()


def _snapshot(root: Path) -> dict[str, object]:
    """Logical rows of every pending-work database (WAL/SHM bookkeeping excluded)."""
    result: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_dir() or path.name != "github_pending_work.db":
            continue
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                result[relative] = list(connection.iterdump())
        except sqlite3.DatabaseError:
            result[relative] = path.read_bytes()
    return result


def _invoke(*args: str):
    return CliRunner().invoke(main, ["pending-work", *args])


class _RecordingGithub:
    def __init__(self, issue: dict) -> None:
        self.issue = issue
        self.calls: list[tuple] = []

    def get_issue_dispatch_snapshot_strict(self, repo_name, issue_number):
        self.calls.append((repo_name, issue_number))
        return dict(self.issue)

    def get_direct_sub_issues_strict(self, repo_name, issue_number):
        return []


def _engine(repo: str, github=None) -> AutomationEngine:
    return AutomationEngine(github or _RecordingGithub({}), AutomationConfig())


def test_default_routing_places_each_obligation_in_its_own_store_only(home, monkeypatch):
    """AS-001/AS-006: normal binding, real deferral writer, no injected store."""
    issue = {"number": 5411, "title": "Same", "body": "Same", "labels": [], "user": {"id": 999}}
    revision = _issue_content_revision(issue)
    engines = {}
    for repo in (R, S):
        engines[repo] = _engine(repo, _RecordingGithub(issue))
        engines[repo]._bind_pending_work_scheduler(repo)
    # Environment and cwd changes never redirect an already bound repository.
    monkeypatch.setenv("REPO_NAME", S)
    monkeypatch.chdir(home)

    for repo in (R, S):
        engines[repo]._defer_issue_evaluation(repo, 5411, issue, _throttle(), CandidateProcessingResult(type="issue", number=5411, title="Same"))

    for repo in (R, S):
        path = repository_pending_work_path(repo)
        assert path.exists() and str(path).startswith(str(home / ".auto-coder" / "repositories"))
        rows = _rows(path)
        assert [(row[0], row[1], row[2], row[3]) for row in rows] == [(repo, "issue:5411", ISSUE_PROCESSING_STAGE, revision)]
    assert repository_pending_work_path(R) != repository_pending_work_path(S)
    assert not default_pending_work_path().exists()

    async def run_only(repo: str) -> None:
        scheduler = engines[repo].pending_work_scheduler
        scheduler._clock = lambda: 10**12
        await scheduler._dispatch_due()
        await asyncio.gather(*tuple(scheduler._tasks))

    github_r, github_s = engines[R].github, engines[S].github
    asyncio.run(run_only(R))
    assert github_r.calls and set(github_r.calls) == {(R, 5411)}
    assert github_s.calls == []
    assert _rows(repository_pending_work_path(R)) == []
    assert len(_rows(repository_pending_work_path(S))) == 1
    asyncio.run(run_only(S))
    assert github_s.calls and set(github_s.calls) == {(S, 5411)}
    assert _rows(repository_pending_work_path(S)) == []


def test_store_registry_association_is_immutable_and_refuses_ambient_targets(home, monkeypatch):
    first = get_pending_work_store(R)
    second = get_pending_work_store(S)
    monkeypatch.setenv("REPO_NAME", S)
    assert get_pending_work_store(R) is first
    assert first.db_path == repository_pending_work_path(R)
    assert second.db_path == repository_pending_work_path(S)
    with pytest.raises(PendingWorkPersistenceError):
        first.defer(_identity(S), _throttle(), ("effect",))
    for invalid in ("", "acme", "acme/a/b", "https://github.com/acme/alpha", "acme/.."):
        with pytest.raises(PendingWorkPersistenceError):
            get_pending_work_store(invalid)


def test_mismatched_database_owner_refuses_without_selecting_another_repository(home):
    get_pending_work_store(R)
    path = repository_pending_work_path(R)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE pending_work_owner SET repository_key=?", (S,))
    gpw._READY_STORES.clear()
    with pytest.raises(PendingWorkNotReadyError):
        get_pending_work_store(R)
    assert not repository_pending_work_path(S).exists()


def test_ready_destination_that_disappears_refuses_instead_of_recreating_empty_queue(home):
    store = get_pending_work_store(R)
    store.defer(_identity(R), _throttle(), ("effect",))
    repository_pending_work_path(R).unlink()
    with pytest.raises(PendingWorkPersistenceError):
        store.all_pending()
    assert not repository_pending_work_path(R).exists()


def test_legacy_rows_block_daemon_and_explicit_processing_until_real_offline_migration(home):
    """AS-002: MIGRATION_REQUIRED at both entries, then the real CLI cutover."""
    legacy = _legacy_store()
    running = _identity(R, 1, revision="run")
    for repo in (R, S):
        legacy.defer(_identity(repo, 7), _throttle(), ("effect-a", "effect-b"))
    legacy.defer(running, _throttle(), ("effect",))
    legacy.mark_running(running)
    legacy.complete_effect(_identity(R, 7), "effect-a")
    before = _snapshot(home)

    github = _RecordingGithub({})
    engine = _engine(R, github)
    with pytest.raises(PendingWorkNotReadyError) as refusal:
        engine._bind_pending_work_scheduler(R)
    assert R in str(refusal.value) and f"auto-coder pending-work migrate --repository {R} --offline" in str(refusal.value)

    result = engine.process_single(R, "issue", 7, explicit_only=True)
    assert result["target_outcome"] == ExplicitTargetOutcome.FAILED.value
    assert f"pending-work migrate --repository {R} --offline" in result["target_reason"]
    assert github.calls == []
    assert _snapshot(home) == before
    assert not repository_pending_work_path(R).exists()

    migrated = _invoke("migrate", "--repository", R, "--offline")
    assert migrated.exit_code == 0, migrated.output
    engine._bind_pending_work_scheduler(R)
    store = get_pending_work_store(R)
    assert {(item.identity.entity, item.status) for item in store.all_pending()} == {("issue:7", "waiting"), ("issue:1", "running")}
    assert store.get(_identity(R, 7)).unfinished_effects == ("effect-b",)
    assert store.get(_identity(R, 7)).not_before > 0
    # S remains preserved, visible as migration-required, and still refuses.
    listing = _invoke("list")
    states = {(json.loads(line)["repository"], json.loads(line)["storage_state"]) for line in listing.output.splitlines() if line.startswith("{")}
    assert states == {(R, "initialized"), (S, "migration-required")}
    with pytest.raises(PendingWorkNotReadyError):
        get_pending_work_store(S)
    assert _invoke("migrate", "--repository", S, "--offline").exit_code == 0
    assert [item.identity.entity for item in get_pending_work_store(S).all_pending()] == ["issue:7"]


def test_completed_migrated_work_is_not_replayed_or_listed_from_backup(home):
    """AS-002/AS-004: the initialization record outlives the last pending row."""
    legacy = _legacy_store()
    legacy.defer(_identity(R, 7), _throttle(), ("effect",))
    legacy.defer(_identity(S, 7), _throttle(), ("effect",))
    assert _invoke("migrate", "--repository", R, "--offline").exit_code == 0
    store = get_pending_work_store(R)
    assert store.complete_effect(_identity(R, 7), "effect")
    assert store.all_pending() == []

    gpw._READY_STORES.clear()
    assert get_pending_work_store(R).all_pending() == []
    assert _invoke("migrate", "--repository", R, "--offline").exit_code == 0
    assert get_pending_work_store(R).all_pending() == []

    before = _snapshot(home)
    listed = _invoke("list")
    assert listed.exit_code == 0
    records = [json.loads(line) for line in listed.output.splitlines() if line.startswith("{")]
    assert [(record["repository"], record["storage_state"]) for record in records] == [(S, "migration-required")]
    assert str(default_pending_work_path()) == records[0]["storage_path"]
    only_r = _invoke("list", "--repository", R.upper())
    assert only_r.exit_code == 0 and "No pending GitHub work retained" in only_r.output
    assert _snapshot(home) == before


def test_list_reports_unavailable_storage_with_nonzero_exit_and_no_side_effects(home):
    get_pending_work_store(R).defer(_identity(R), _throttle(), ("effect",))
    get_pending_work_store(S)
    repository_pending_work_path(S).write_bytes(b"not a sqlite database at all" * 10)
    before = _snapshot(home)
    result = CliRunner().invoke(main, ["pending-work", "list"])
    assert result.exit_code != 0
    assert '"repository": "acme/alpha"' in result.output
    assert "ERROR" in result.output and str(repository_pending_work_path(S)) in result.output
    assert _snapshot(home) == before


def test_retry_targets_one_identity_in_its_own_ready_store_only(home):
    """AS-005: equal tuple in both repositories; only the selected row changes."""
    stores = {repo: get_pending_work_store(repo) for repo in (R, S)}
    for repo, store in stores.items():
        store.defer(_identity(repo), _throttle(), ("effect-a", "effect-b"))
        store.complete_effect(_identity(repo), "effect-a")
    other_before = _rows(repository_pending_work_path(S))
    before_init = sqlite3.connect(repository_pending_work_path(R)).execute("SELECT * FROM pending_work_initialization").fetchall()
    old_rows = _rows(repository_pending_work_path(R))

    result = _invoke("retry", "--repository", R.upper(), "--entity", "issue:5411", "--stage", ISSUE_PROCESSING_STAGE, "--revision", "rev")
    assert result.exit_code == 0, result.output
    new_rows = _rows(repository_pending_work_path(R))
    assert len(new_rows) == 1
    repo, entity, stage, revision, reason, not_before, effects, throttle_count, last_error, status, updated = new_rows[0]
    assert (repo, entity, stage, revision) == (R, "issue:5411", ISSUE_PROCESSING_STAGE, "rev")
    assert (reason, effects, last_error) == (old_rows[0][4], old_rows[0][6], old_rows[0][8])
    assert json.loads(effects) == ["effect-b"]
    assert status == "waiting" and throttle_count == 0 and not_before == updated
    assert sqlite3.connect(repository_pending_work_path(R)).execute("SELECT * FROM pending_work_initialization").fetchall() == before_init
    assert _rows(repository_pending_work_path(S)) == other_before
    assert not default_pending_work_path().exists()


def test_retry_refusals_do_not_reset_or_search_other_stores(home):
    store_r, store_s = get_pending_work_store(R), get_pending_work_store(S)
    store_s.defer(_identity(S, 9), _throttle(), ("effect",))
    # A deliberately foreign row inside R's correctly owned database.
    with sqlite3.connect(repository_pending_work_path(R)) as connection:
        foreign = _identity(S, 9)
        connection.execute(
            "INSERT INTO github_pending_work VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (foreign.key(), S, foreign.entity, foreign.stage, foreign.revision, "throttled", 9e12, '["effect"]', 3, "", 1.0, "waiting"),
        )
    # Equal identity differing only in repository text case is ambiguous for K(R).
    store_r.defer(WorkIdentity("acme/alpha", "issue:1", ISSUE_PROCESSING_STAGE, "rev"), _throttle(), ("e",))
    with sqlite3.connect(repository_pending_work_path(R)) as connection:
        twin = WorkIdentity("ACME/Alpha", "issue:1", ISSUE_PROCESSING_STAGE, "rev")
        connection.execute(
            "INSERT INTO github_pending_work VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (twin.key(), twin.repository, twin.entity, twin.stage, twin.revision, "throttled", 9e12, '["e"]', 3, "", 1.0, "waiting"),
        )
    before = _snapshot(home)
    for args in (
        ["--repository", R, "--entity", "issue:9", "--stage", ISSUE_PROCESSING_STAGE, "--revision", "rev"],  # foreign row invisible
        ["--repository", R, "--entity", "issue:404", "--stage", ISSUE_PROCESSING_STAGE, "--revision", "rev"],  # no match
        ["--repository", R, "--entity", "issue:1", "--stage", ISSUE_PROCESSING_STAGE, "--revision", "rev"],  # ambiguous
        ["--repository", "acme/gamma", "--entity", "issue:1", "--stage", ISSUE_PROCESSING_STAGE, "--revision", "rev"],  # no destination
        ["--repository", "not-a-repository", "--entity", "issue:1", "--stage", ISSUE_PROCESSING_STAGE],
    ):
        result = _invoke("retry", *args)
        assert result.exit_code != 0, result.output
    assert _snapshot(home) == before
    assert not repository_pending_work_path("acme/gamma").exists()


def test_retry_refuses_migration_required_without_touching_legacy(home):
    _legacy_store().defer(_identity(R), _throttle(), ("effect",))
    before = _snapshot(home)
    result = _invoke("retry", "--repository", R, "--entity", "issue:5411", "--stage", ISSUE_PROCESSING_STAGE, "--revision", "rev")
    assert result.exit_code != 0 and "migration" in result.output.lower()
    assert _snapshot(home) == before


def test_repair_grant_cli_schedules_into_ready_destination_not_legacy(home):
    """AS-005/AS-003: the shipped grant writer targets the repository store."""
    from tests.test_pr_repair_exhaustion import PR_NUMBER, REPO, _exhaust_blocker

    _legacy_store().defer(_identity(S), _throttle(), ("effect",))
    legacy_before = _rows(default_pending_work_path())
    epoch = _exhaust_blocker(RepairAllowanceLedger(), "blk_2377")

    result = CliRunner().invoke(main, ["pr-repair", "resume", "--repo", REPO, "--pr", str(PR_NUMBER), "--expected-epoch", str(epoch), "--request-id", "req-2377"])
    assert result.exit_code == 0, result.output
    rows = _rows(repository_pending_work_path(REPO))
    assert [(row[0], row[1], row[2]) for row in rows] == [(REPO, f"pr:{PR_NUMBER}", PR_PROCESSING_STAGE)]
    assert _rows(default_pending_work_path()) == legacy_before
    assert not repository_pending_work_path(S).exists()


def test_pr_repair_resume_refuses_when_repository_is_not_ready(home):
    """A migration-required repository is not granted repair through shared storage."""
    _legacy_store().defer(_identity(R, 3, PR_PROCESSING_STAGE), _throttle(), (PR_PROCESSING_REFRESH_EFFECT,))
    legacy_before = _rows(default_pending_work_path())
    result = CliRunner().invoke(main, ["pr-repair", "resume", "--repo", R, "--pr", "3", "--expected-epoch", "0", "--request-id", "req-1"])
    assert result.exit_code != 0
    assert _rows(default_pending_work_path()) == legacy_before
    assert not repository_pending_work_path(R).exists()


def test_scoped_ready_store_does_not_claim_or_change_foreign_row(home):
    """AS-006: correct filename and owner, but a foreign row is never executed."""
    store = get_pending_work_store(R)
    foreign = _identity(S, 5)
    with sqlite3.connect(repository_pending_work_path(R)) as connection:
        connection.execute(
            "INSERT INTO github_pending_work VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (foreign.key(), S, foreign.entity, foreign.stage, foreign.revision, "throttled", 1.0, '["effect"]', 0, "", 1.0, "running"),
        )
    before = _rows(repository_pending_work_path(R))
    calls: list[WorkIdentity] = []

    class Handler:
        def dispatch(self, obligation):
            calls.append(obligation.identity)
            return StageOutcome()

        recover = dispatch

    scheduler = gpw.PendingWorkScheduler(store, repository=R, clock=lambda: 10**12)
    scheduler.register_handler(ISSUE_PROCESSING_STAGE, Handler())

    async def go() -> None:
        await scheduler._recover_interrupted()
        await scheduler._dispatch_due()
        await asyncio.gather(*tuple(scheduler._tasks))

    asyncio.run(go())
    assert calls == []
    assert _rows(repository_pending_work_path(R)) == before
    assert store.all_pending() == [] and store.due() == [] and store.interrupted() == []


def _insert_row(path: Path, identity: WorkIdentity, status: str = "waiting") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO github_pending_work VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity.key(), identity.repository, identity.entity, identity.stage, identity.revision, "throttled", 9e12, '["effect"]', 0, "", 1.0, status),
        )


def test_list_reports_foreign_row_in_initialized_destination_with_nonzero_exit(home):
    get_pending_work_store(R)
    _insert_row(repository_pending_work_path(R), _identity(S, 9))
    before = _snapshot(home)
    for args in ([], ["--repository", R]):
        result = CliRunner().invoke(main, ["pending-work", "list", *args])
        assert result.exit_code != 0
        assert "another repository" in result.output and str(repository_pending_work_path(R)) in result.output
        assert "No pending GitHub work" not in result.output
    assert _snapshot(home) == before


def test_cached_store_is_reverified_on_every_processing_invocation(home):
    """A vanished or downgraded destination refuses before any target request."""
    github = _RecordingGithub({})
    engine = _engine(R, github)
    get_pending_work_store(R)
    repository_pending_work_path(R).unlink()
    result = engine.process_single(R, "issue", 7, explicit_only=True)
    assert result["target_outcome"] == ExplicitTargetOutcome.FAILED.value
    assert github.calls == []
    assert not repository_pending_work_path(R).exists()


def test_cached_store_refuses_after_initialization_receipt_is_removed(home):
    store = get_pending_work_store(R)
    store.defer(_identity(R), _throttle(), ("effect",))
    path = repository_pending_work_path(R)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM pending_work_initialization")
    before = _rows(path)
    with pytest.raises(PendingWorkPersistenceError):
        store.defer(_identity(R, 2), _throttle(), ("effect",))
    with pytest.raises(PendingWorkPersistenceError):
        store.complete_effect(_identity(R), "effect")
    with pytest.raises(PendingWorkNotReadyError):
        get_pending_work_store(R)
    assert _rows(path) == before
