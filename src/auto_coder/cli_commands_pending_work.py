"""Operator CLI for the durable GitHub pending-work store.

The scheduler (``github_pending_work.PendingWorkScheduler``) resumes retained
obligations automatically. The one operation it deliberately does not perform
on its own is clearing a retry block after an operator has corrected the
underlying condition (e.g. rotated an expired token) - REQ-007 requires an
explicit, documented manual retry for a single selected obligation.
"""

import json

import click

from .github_pending_work import PendingWorkStore, WorkIdentity, default_pending_work_path
from .logger_config import get_logger

logger = get_logger(__name__)


@click.group(name="pending-work")
def pending_work_group() -> None:
    """Inspect and manage durable GitHub pending-work obligations.

    - list: Show every retained obligation (waiting, running, or blocked)
    - retry: Manually retry one selected retry-blocked obligation
    """
    pass


@pending_work_group.command(name="list")
def pending_work_list() -> None:
    """List every retained obligation in the durable pending-work store."""
    store = PendingWorkStore()
    obligations = store.all_pending()
    if not obligations:
        click.echo(f"No pending GitHub work retained ({default_pending_work_path()}).")
        return
    for obligation in obligations:
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
                }
            )
        )


@pending_work_group.command(name="retry")
@click.option("--repository", required=True, help="Repository in owner/repo form, e.g. acme/widgets")
@click.option("--entity", required=True, help="Entity identifier as retained, e.g. issue:12 or pr:4")
@click.option("--stage", required=True, help="Semantic stage that owns this obligation, e.g. validation")
@click.option("--revision", default="", help="Input revision the obligation was retained against, if any")
def pending_work_retry(repository: str, entity: str, stage: str, revision: str) -> None:
    """Manually retry one selected retry-blocked obligation.

    This only clears the selected obligation's retry deadline and throttle
    count; it does not discharge confirmed effect receipts, does not affect
    any other obligation, and still goes through the shared governor for
    admission on its next dispatch.
    """
    store = PendingWorkStore()
    identity = WorkIdentity(repository, entity, stage, revision)
    obligation = store.manual_retry(identity)
    if obligation is None:
        click.echo(f"No retained obligation found for {identity.key()}")
        raise SystemExit(1)
    click.echo(f"Obligation for {identity.key()} is eligible for retry (reason={obligation.reason.value}, effects={list(obligation.unfinished_effects)}).")
