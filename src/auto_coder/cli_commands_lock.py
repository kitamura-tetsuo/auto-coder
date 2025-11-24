"""Commands for lock management."""

import click

from .lock_manager import LockManager


@click.group(invoke_without_command=False)
def lock_group():
    """Lock management commands."""
    pass


@lock_group.command()
@click.option(
    "--force",
    is_flag=True,
    help="Force unlock by removing the lock file",
)
def unlock(force: bool):
    """Remove the lock file."""
    lock_manager = LockManager()

    if not lock_manager.is_locked():
        click.echo("No lock file found. Nothing to unlock.")
        return

    if not force:
        lock_info = lock_manager.get_lock_info_obj()
        if lock_info:
            click.echo("Lock file exists with the following information:")
            click.echo(f"  PID: {lock_info.pid}")
            click.echo(f"  Hostname: {lock_info.hostname}")
            click.echo(f"  Started at: {lock_info.started_at}")

            if lock_manager._is_process_running(lock_info.pid):
                click.echo("\nThe process is still running.", err=True)
                click.echo("Use --force to remove the lock file anyway.", err=True)
                return
            else:
                click.echo("\nThe process is no longer running (stale lock).")
                click.echo("Removing lock file...", err=True)

    lock_manager.release_lock()
    click.echo("Lock file removed successfully.")
