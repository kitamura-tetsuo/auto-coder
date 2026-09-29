from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import time
from pathlib import Path

from click.testing import CliRunner

from auto_coder.cli import main
from auto_coder.github_pending_work import (
    ObligationStatus,
    PendingWorkOwnershipError,
    PendingWorkReadiness,
    PendingWorkStore,
    WorkIdentity,
    migrate_pending_work_store,
    repository_pending_work_path,
    resolve_pending_work_store,
)
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubResponseMetadata,
    RequestProvenance,
)


def _error(classification, *, delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED):
    outcome = GitHubRequestOutcome(
        GitHubRequestContext("operation", "attempt", "test", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}"),
        403,
        classification,
        RequestProvenance.NETWORK,
        delivery,
        GitHubResponseMetadata(retry_after_seconds=5),
        1,
    )
    return GitHubRequestError(outcome)


class _CommitGateConnection:
    """Delegate SQLite operations while exposing a real destination commit boundary."""

    def __init__(self, connection, reached, release, *, gate_after_commit=False, raise_after_commit=False):
        self._connection = connection
        self._reached = reached
        self._release = release
        self._gate_after_commit = gate_after_commit
        self._raise_after_commit = raise_after_commit

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        if not self._gate_after_commit:
            self._reached.set()
            if self._release is not None:
                assert self._release.wait(10), "commit gate was not released"
        self._connection.commit()
        if self._gate_after_commit:
            self._reached.set()
            if self._release is not None:
                assert self._release.wait(10), "post-commit gate was not released"
        if self._raise_after_commit:
            raise sqlite3.OperationalError("injected unknown commit result")


class _BeginProbeConnection:
    """Record when a contender reaches SQLite's destination write boundary."""

    def __init__(self, connection, reached):
        self._connection = connection
        self._reached = reached

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, statement, parameters=()):
        if statement == "BEGIN IMMEDIATE":
            self._reached.set()
        return self._connection.execute(statement, parameters)


def _migration_process(home, commit_reached, release_commit, output, gate_after_commit=False):
    """Run the shipped migration boundary with its real commit held open."""
    from auto_coder import github_pending_work

    destination = github_pending_work.repository_pending_work_path("acme/widgets", home=Path(home))
    original_connect = github_pending_work.sqlite3.connect

    def gated_connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if Path(database) == destination and "isolation_level" in kwargs and kwargs["isolation_level"] is None:
            return _CommitGateConnection(connection, commit_reached, release_commit, gate_after_commit=gate_after_commit)
        return connection

    github_pending_work.sqlite3.connect = gated_connect
    result = github_pending_work.migrate_pending_work_store("acme/widgets", offline=True, home=Path(home))
    output.put((result.readiness.value, result.detail))


def _contending_migration_process(home, begin_reached, output):
    """Run a second migration and prove that it attempts BEGIN IMMEDIATE."""
    from auto_coder import github_pending_work

    destination = github_pending_work.repository_pending_work_path("acme/widgets", home=Path(home))
    original_connect = github_pending_work.sqlite3.connect

    def probed_connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if Path(database) == destination and "isolation_level" in kwargs and kwargs["isolation_level"] is None:
            return _BeginProbeConnection(connection, begin_reached)
        return connection

    github_pending_work.sqlite3.connect = probed_connect
    result = github_pending_work.migrate_pending_work_store("acme/widgets", offline=True, home=Path(home))
    output.put((result.readiness.value, result.detail))


def _legacy(home, identities):
    path = home / ".auto-coder" / "github_pending_work.db"
    store = PendingWorkStore(path)
    for number, identity in enumerate(identities):
        store.defer(identity, _error(GitHubApiOutcome.SECONDARY_THROTTLED), (f"effect-{number}",), now=100 + number)
    return path


def test_repository_location_and_empty_initialization_are_durable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = hashlib.sha256(b"acme/widgets").hexdigest()
    first_path = repository_pending_work_path("  Acme/Widgets\t", home=tmp_path)
    assert first_path == tmp_path / ".auto-coder" / "repositories" / expected / "github_pending_work.db"
    assert first_path != repository_pending_work_path("acme_widgets/x", home=tmp_path)

    result = resolve_pending_work_store("  Acme/Widgets\t", home=tmp_path)
    assert result.readiness is PendingWorkReadiness.READY
    assert result.store is not None
    assert result.store.all_pending() == []
    assert resolve_pending_work_store("ACME/WIDGETS", home=tmp_path).readiness is PendingWorkReadiness.READY

    with sqlite3.connect(first_path) as connection:
        assert connection.execute("SELECT repository_key FROM pending_work_owner").fetchone() == ("acme/widgets",)
        assert connection.execute("SELECT kind FROM pending_work_initialization").fetchone() == ("empty",)


def test_readiness_requires_offline_migration_and_preserves_all_fields(tmp_path):
    own = WorkIdentity("Acme/Widgets", "issue:1", "publish", "rev-a")
    foreign = WorkIdentity("other/repo", "issue:1", "publish", "rev-b")
    source = _legacy(tmp_path, [own, foreign])
    legacy = PendingWorkStore(source)
    legacy.mark_running(own)
    before = legacy.get(own)
    assert before is not None and before.status == ObligationStatus.RUNNING.value

    readiness = resolve_pending_work_store("acme/widgets", home=tmp_path)
    assert readiness.readiness is PendingWorkReadiness.MIGRATION_REQUIRED
    refused = migrate_pending_work_store("acme/widgets", offline=False, home=tmp_path)
    assert refused.readiness is PendingWorkReadiness.MIGRATION_REQUIRED
    assert not refused.destination.exists()

    migrated = migrate_pending_work_store("acme/widgets", offline=True, home=tmp_path)
    assert migrated.readiness is PendingWorkReadiness.READY
    assert migrated.store is not None
    assert migrated.store.all_pending() == [before]
    assert PendingWorkStore(source).get(own) == before
    assert PendingWorkStore(source).get(foreign) is not None

    # Completion in the authoritative destination cannot be resurrected from backup.
    assert migrated.store.complete_effect(own, "effect-0") is True
    assert migrated.store.get(own) is None
    repeated = migrate_pending_work_store("ACME/WIDGETS", offline=True, home=tmp_path)
    assert repeated.readiness is PendingWorkReadiness.READY
    assert repeated.detail == "already initialized"
    assert repeated.store is not None and repeated.store.get(own) is None


def test_migration_reads_committed_wal_and_keeps_source_unchanged(tmp_path):
    source = tmp_path / ".auto-coder" / "github_pending_work.db"
    source.parent.mkdir(parents=True)
    keeper = sqlite3.connect(source)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    identity = WorkIdentity("acme/widgets", "pr:9", "ci", "head")
    PendingWorkStore(source).defer(identity, _error(GitHubApiOutcome.FORBIDDEN), ("publish",), now=50)
    assert source.with_name(source.name + "-wal").exists()

    migrated = migrate_pending_work_store("acme/widgets", offline=True, home=tmp_path)
    keeper.close()
    assert migrated.readiness is PendingWorkReadiness.READY
    assert migrated.store is not None
    record = migrated.store.get(identity)
    assert record is not None
    assert record.unfinished_effects == ("publish",)
    assert record.not_before == 0


def test_conflicts_and_malformed_selected_rows_fail_closed(tmp_path):
    identity = WorkIdentity("acme/widgets", "issue:2", "stage", "rev")
    source = _legacy(tmp_path, [identity])
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE github_pending_work SET work_key='contradiction'")
    result = migrate_pending_work_store("acme/widgets", offline=True, home=tmp_path)
    assert result.readiness is PendingWorkReadiness.UNAVAILABLE
    assert "inconsistent work key" in result.detail

    other_home = tmp_path / "other"
    destination = repository_pending_work_path("acme/widgets", home=other_home)
    destination.parent.mkdir(parents=True)
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE unexpected(value TEXT)")
    conflict = resolve_pending_work_store("acme/widgets", home=other_home)
    assert conflict.readiness is PendingWorkReadiness.UNAVAILABLE
    assert "incompatible" in conflict.detail


def test_ready_handle_rejects_foreign_mutations(tmp_path):
    resolution = resolve_pending_work_store("acme/widgets", home=tmp_path)
    assert resolution.store is not None
    foreign = WorkIdentity("other/repo", "issue:1", "stage")
    try:
        resolution.store.supersede(foreign)
    except PendingWorkOwnershipError:
        pass
    else:
        raise AssertionError("foreign mutation was accepted")


def test_migration_cli_reports_paths_and_requires_acknowledgement(tmp_path):
    identity = WorkIdentity("acme/widgets", "issue:3", "stage")
    _legacy(tmp_path, [identity])
    runner = CliRunner()
    refused = runner.invoke(main, ["pending-work", "migrate", "--repository", "acme/widgets"], env={"HOME": str(tmp_path)})
    assert refused.exit_code != 0
    assert "outcome=MIGRATION_REQUIRED" in refused.output
    migrated = runner.invoke(main, ["pending-work", "migrate", "--repository", "acme/widgets", "--offline"], env={"HOME": str(tmp_path)})
    assert migrated.exit_code == 0, migrated.output
    assert "outcome=READY" in migrated.output
    assert "source=" in migrated.output and "destination=" in migrated.output


def _legacy_operational_states(home):
    """Create migration evidence only through production store transitions."""
    source = home / ".auto-coder" / "github_pending_work.db"
    store = PendingWorkStore(source)
    identities = {
        "running": WorkIdentity("acme/widgets", "issue:running", "stage"),
        "authentication": WorkIdentity("acme/widgets", "issue:authentication", "stage"),
        "forbidden": WorkIdentity("acme/widgets", "issue:forbidden", "stage"),
        "indeterminate": WorkIdentity("acme/widgets", "issue:indeterminate", "stage"),
        "exhausted": WorkIdentity("acme/widgets", "issue:exhausted", "stage"),
        "partial": WorkIdentity("acme/widgets", "issue:partial", "stage"),
    }
    store.defer(identities["running"], _error(GitHubApiOutcome.SECONDARY_THROTTLED), ("resume",), now=10)
    store.mark_running(identities["running"])
    store.defer(identities["authentication"], _error(GitHubApiOutcome.AUTHENTICATION_FAILURE), ("authenticate",), now=11)
    store.defer(identities["forbidden"], _error(GitHubApiOutcome.FORBIDDEN), ("authorize",), now=12)
    indeterminate = _error(GitHubApiOutcome.TRANSPORT_FAILURE, delivery=DeliveryCertainty.INDETERMINATE)
    store.defer(identities["indeterminate"], indeterminate, ("reconcile",), now=13)
    for attempt in range(4):
        store.defer(identities["exhausted"], _error(GitHubApiOutcome.PRIMARY_THROTTLED), ("retry",), now=20 + attempt)
    store.defer(identities["partial"], _error(GitHubApiOutcome.SECONDARY_THROTTLED), ("done", "remaining"), now=30)
    assert store.complete_effect(identities["partial"], "done") is True
    return source, identities, {obligation.identity.key(): obligation for obligation in store.all_pending()}


def _assert_migrated_snapshot(home, expected):
    destination = repository_pending_work_path("acme/widgets", home=home)
    reopened = PendingWorkStore(destination, repository="acme/widgets")
    actual = {obligation.identity.key(): obligation for obligation in reopened.all_pending()}
    assert actual == expected
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pending_work_initialization").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM github_pending_work").fetchone() == (len(expected),)


def test_contending_migrations_use_one_committed_receipt_and_exact_operational_state(tmp_path):
    _, identities, expected = _legacy_operational_states(tmp_path)
    assert expected[identities["running"].key()].status == ObligationStatus.RUNNING.value
    assert expected[identities["authentication"].key()].reason.value == "authentication_failure"
    assert expected[identities["forbidden"].key()].reason.value == "forbidden"
    assert expected[identities["indeterminate"].key()].reason.value == "indeterminate_delivery"
    assert expected[identities["exhausted"].key()].reason.value == "throttle_retries_exhausted"
    assert expected[identities["partial"].key()].unfinished_effects == ("remaining",)

    context = multiprocessing.get_context("fork")
    commit_reached = context.Event()
    release_commit = context.Event()
    contender_reached = context.Event()
    first_output = context.Queue()
    second_output = context.Queue()
    first = context.Process(target=_migration_process, args=(str(tmp_path), commit_reached, release_commit, first_output))
    first.start()
    assert commit_reached.wait(5), "migration A did not reach its real destination commit"

    second = context.Process(target=_contending_migration_process, args=(str(tmp_path), contender_reached, second_output))
    second.start()
    assert contender_reached.wait(5), "migration B did not reach the contested BEGIN IMMEDIATE boundary"
    assert first.is_alive(), "migration A did not still own the transaction when B contended"
    release_commit.set()
    first.join(10)
    second.join(10)
    assert first.exitcode == 0
    assert second.exitcode == 0
    assert first_output.get(timeout=1)[0] == PendingWorkReadiness.READY.value
    assert second_output.get(timeout=1)[0] in {PendingWorkReadiness.READY.value, PendingWorkReadiness.UNAVAILABLE.value}
    _assert_migrated_snapshot(tmp_path, expected)


def test_precommit_kill_is_non_ready_and_retryable_from_preserved_source(tmp_path):
    source, _, expected = _legacy_operational_states(tmp_path)
    context = multiprocessing.get_context("fork")
    commit_reached = context.Event()
    release_commit = context.Event()
    output = context.Queue()
    migration = context.Process(target=_migration_process, args=(str(tmp_path), commit_reached, release_commit, output))
    migration.start()
    assert commit_reached.wait(5)
    migration.terminate()
    migration.join(10)
    assert migration.exitcode is not None and migration.exitcode != 0
    assert resolve_pending_work_store("acme/widgets", home=tmp_path).readiness is PendingWorkReadiness.MIGRATION_REQUIRED
    assert PendingWorkStore(source).all_pending()

    retried = migrate_pending_work_store("acme/widgets", offline=True, home=tmp_path)
    assert retried.readiness is PendingWorkReadiness.READY
    _assert_migrated_snapshot(tmp_path, expected)


def test_postcommit_kill_and_unknown_commit_reopen_confirm_durable_ready(tmp_path, monkeypatch):
    _, _, expected = _legacy_operational_states(tmp_path)
    context = multiprocessing.get_context("fork")
    committed = context.Event()
    keep_process_alive = context.Event()
    output = context.Queue()
    migration = context.Process(target=_migration_process, args=(str(tmp_path), committed, keep_process_alive, output, True))
    migration.start()
    assert committed.wait(5), "migration did not commit before the simulated crash"
    migration.terminate()
    migration.join(10)
    assert resolve_pending_work_store("acme/widgets", home=tmp_path).readiness is PendingWorkReadiness.READY
    _assert_migrated_snapshot(tmp_path, expected)

    uncertain_home = tmp_path / "uncertain"
    _legacy_operational_states(uncertain_home)
    destination = repository_pending_work_path("acme/widgets", home=uncertain_home)
    original_connect = sqlite3.connect
    commit_attempted = multiprocessing.Event()

    def uncertain_connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if Path(database) == destination and "isolation_level" in kwargs and kwargs["isolation_level"] is None:
            return _CommitGateConnection(connection, commit_attempted, None, raise_after_commit=True)
        return connection

    monkeypatch.setattr("auto_coder.github_pending_work.sqlite3.connect", uncertain_connect)
    uncertain = migrate_pending_work_store("acme/widgets", offline=True, home=uncertain_home)
    assert commit_attempted.is_set()
    assert uncertain.readiness is PendingWorkReadiness.READY
    assert "confirmed after uncertain result" in uncertain.detail
    _assert_migrated_snapshot(uncertain_home, expected)
