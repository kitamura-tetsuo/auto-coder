"""Operator CLI for the durable GitHub pending-work store.

The scheduler (``github_pending_work.PendingWorkScheduler``) resumes retained
obligations automatically. The one operation it deliberately does not perform
on its own is clearing a retry block after an operator has corrected the
underlying condition (e.g. rotated an expired token) - REQ-007 requires an
explicit, documented manual retry for a single selected obligation.
"""

import json

import click

from .github_pending_work import (
    PendingWorkOwnershipError,
    PendingWorkPersistenceError,
    PendingWorkReadiness,
    inspect_pending_work,
    migrate_pending_work_store,
    resolve_existing_pending_work_store,
)
from .logger_config import get_logger

logger = get_logger(__name__)


@click.group(name="pending-work")
def pending_work_group() -> None:
    """Inspect and manage durable GitHub pending-work obligations.

    - list: Show retained obligations (waiting, running, or blocked) across repositories
    - retry: Manually retry one selected retained obligation
    - migrate: Cut one repository over from the legacy shared database
    """
    pass


@pending_work_group.command(name="list")
@click.option("--repository", default=None, help="Only show this repository (owner/repo form)")
def pending_work_list(repository: str | None) -> None:
    """List retained obligations across initialized and not-yet-migrated repositories.

    This is a read-only aggregate view: it never initializes, imports, resets,
    or executes anything.
    """
    try:
        inspection = inspect_pending_work(repository)
    except PendingWorkOwnershipError as exc:
        raise click.ClickException(str(exc)) from exc
    shown = 0
    for view in inspection.views:
        for obligation in view.obligations:
            shown += 1
            click.echo(
                json.dumps(
                    {
                        "repository": obligation.identity.repository,
                        "entity": obligation.identity.entity,
                        "stage": obligation.identity.stage,
                        "revision": obligation.identity.revision,
                        "reason": obligation.reason.value,
                        "status": obligation.status,
                        "not_before": obligation.not_before,
                        "unfinished_effects": list(obligation.unfinished_effects),
                        "throttle_attempts": obligation.throttle_attempts,
                        "last_error": obligation.last_error,
                        "storage_path": str(view.storage),
                        "storage_state": view.state.value,
                    }
                )
            )
    for error in inspection.errors:
        logger.error("Pending-work listing incomplete: {}", error)
        click.echo(f"ERROR: {error}", err=True)
    if not shown and not inspection.errors:
        locations = ", ".join(f"{view.repository}={view.storage} ({view.state.value})" for view in inspection.views)
        click.echo(f"No pending GitHub work retained{f' ({locations})' if locations else ''}.")
    if inspection.errors:
        raise SystemExit(1)


@pending_work_group.command(name="retry")
@click.option("--repository", required=True, help="Repository in owner/repo form, e.g. acme/widgets")
@click.option("--entity", required=True, help="Entity identifier as retained, e.g. issue:12 or pr:4")
@click.option("--stage", required=True, help="Semantic stage that owns this obligation, e.g. validation")
@click.option("--revision", default="", help="Input revision the obligation was retained against, if any")
def pending_work_retry(repository: str, entity: str, stage: str, revision: str) -> None:
    """Manually retry one selected retained obligation.

    This only clears the selected obligation's retry deadline and throttle
    count in the selected repository's READY store; it does not discharge
    confirmed effect receipts, does not affect any other obligation, does not
    execute anything itself, and still goes through the shared governor for
    admission on its next dispatch.
    """
    try:
        resolution = resolve_existing_pending_work_store(repository)
    except PendingWorkOwnershipError as exc:
        raise click.ClickException(str(exc)) from exc
    if resolution.readiness is not PendingWorkReadiness.READY or resolution.store is None:
        logger.error("Pending-work retry refused: repository={} readiness={} storage={} detail={}", resolution.repository, resolution.readiness.value, resolution.destination, resolution.detail)
        raise click.ClickException(f"repository={resolution.repository} storage={resolution.destination} state={resolution.readiness.value}: {resolution.detail}")
    store = resolution.store
    try:
        selected = [item for item in store.all_pending() if item.identity.entity == entity and item.identity.stage == stage and item.identity.revision == revision]
        if not selected:
            raise click.ClickException(f"No retained obligation found for repository={resolution.repository} entity={entity} stage={stage} revision={revision!r}")
        if len(selected) > 1:
            raise click.ClickException(f"Ambiguous retained obligations for repository={resolution.repository} entity={entity} stage={stage} revision={revision!r}; nothing was reset")
        obligation = store.manual_retry(selected[0].identity)
    except PendingWorkPersistenceError as exc:
        raise click.ClickException(f"repository={resolution.repository} storage={resolution.destination} unavailable: {exc}") from exc
    if obligation is None:
        raise click.ClickException(f"No retained obligation found for repository={resolution.repository} entity={entity} stage={stage} revision={revision!r}")
    click.echo(f"Obligation for {obligation.identity.key()} is eligible for retry (reason={obligation.reason.value}, effects={list(obligation.unfinished_effects)}).")


@pending_work_group.command(name="migrate")
@click.option("--repository", required=True, help="Repository in owner/repo form, e.g. acme/widgets")
@click.option("--offline", is_flag=True, help="Acknowledge that all controllers using the legacy shared database are stopped")
def pending_work_migrate(repository: str, offline: bool) -> None:
    """Cut over one repository from the preserved shared legacy database."""
    result = migrate_pending_work_store(repository, offline=offline)
    click.echo(f"repository={result.repository} source={result.source} destination={result.destination} " f"outcome={result.readiness.value} detail={result.detail}")
    if result.readiness is not PendingWorkReadiness.READY:
        raise click.ClickException(result.detail)
