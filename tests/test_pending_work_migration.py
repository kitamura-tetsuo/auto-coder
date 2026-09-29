from __future__ import annotations

import hashlib
import sqlite3

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


def _error(classification):
    outcome = GitHubRequestOutcome(
        GitHubRequestContext("operation", "attempt", "test", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}"),
        403,
        classification,
        RequestProvenance.NETWORK,
        DeliveryCertainty.HTTP_RESPONSE_RECEIVED,
        GitHubResponseMetadata(retry_after_seconds=5),
        1,
    )
    return GitHubRequestError(outcome)


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
