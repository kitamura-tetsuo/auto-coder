"""Command Line Interface for Auto-Coder."""

import atexit
import os
import sys
from os import PathLike
from typing import IO

import click

try:
    from dotenv import load_dotenv
except ImportError:
    # Fallback if python-dotenv is not installed
    def load_dotenv(
        dotenv_path: str | PathLike[str] | None = None,
        stream: IO[str] | None = None,
        verbose: bool = False,
        override: bool = False,
        interpolate: bool = True,
        encoding: str | None = "utf-8",
    ) -> bool:
        """No-op function when python-dotenv is not installed."""
        return True


from . import __version__ as AUTO_CODER_VERSION
from .cli_commands_config import config_group
from .cli_commands_graphrag import graphrag_group
from .cli_commands_lock import lock_group
from .cli_commands_main import create_feature_issues, fix_to_pass_tests_command, process_issues
from .cli_commands_mcp import mcp_group
from .cli_commands_mcp_pdb import mcp_pdb_group
from .cli_commands_utils import auth_status, get_actions_logs, migrate_branches
from .cli_helpers import qwen_help_has_flags  # Re-export for tests
from .lock_manager import LockManager
from .update_manager import maybe_run_auto_update, record_startup_options

# Load environment variables
load_dotenv()

# Global lock manager instance
_lock_manager: LockManager | None = None


@click.group(
    invoke_without_command=True,
    help="Auto-Coder: Automated application development using Gemini CLI and GitHub integration.",
)
@click.version_option(version=AUTO_CODER_VERSION, package_name="auto-coder")
@click.option(
    "--force",
    is_flag=True,
    help="Force execution by overriding the lock file if it exists",
)
@click.pass_context
def main(ctx: click.Context, force: bool) -> None:
    # Only run initialization if not showing help
    # Check for multiple help-related flags to avoid initialization during help display
    help_flags = ["--help", "-h", "--version", "-V"]
    has_help_flag = any(help_flag in sys.argv for help_flag in help_flags)

    if not has_help_flag:
        # Check for lock file before proceeding
        global _lock_manager
        _lock_manager = LockManager()

        # Determine which command is being invoked
        invoked_cmd = ctx.invoked_subcommand if hasattr(ctx, "invoked_subcommand") else None

        # Skip lock check for read-only commands
        read_only_commands = ["config", "auth-status", "unlock", "get-actions-logs", "mcp-pdb"]
        is_unlock = invoked_cmd == "unlock" or "unlock" in sys.argv

        # Track whether we acquired a lock so we can clean it up properly
        lock_acquired = False

        if not (invoked_cmd in read_only_commands or has_help_flag or is_unlock):
            if _lock_manager.is_locked():
                lock_info = _lock_manager.get_lock_info_obj()
                if lock_info:
                    click.echo("Error: auto-coder is already running!", err=True)
                    click.echo("", err=True)
                    click.echo("Lock information:", err=True)
                    click.echo(f"  PID: {lock_info.pid}", err=True)
                    click.echo(f"  Hostname: {lock_info.hostname}", err=True)
                    click.echo(f"  Started at: {lock_info.started_at}", err=True)
                    click.echo("", err=True)

                    # Check if the process is still running
                    if _lock_manager._is_process_running(lock_info.pid):
                        click.echo("The process is still running. Please wait for it to complete.", err=True)
                    else:
                        click.echo("The process is no longer running (stale lock).", err=True)
                        click.echo("You can use '--force' to override or run 'auto-coder unlock' to remove the lock.", err=True)

                    sys.exit(1)
                else:
                    # Lock file exists but couldn't be read
                    click.echo("Error: Lock file exists but is corrupted.", err=True)
                    if force:
                        click.echo("Removing corrupted lock file...", err=True)
                        _lock_manager.release_lock()
                    else:
                        click.echo("Use '--force' to remove the corrupted lock file.", err=True)
                        sys.exit(1)

            # Try to acquire the lock for non-read-only commands
            if not _lock_manager.acquire_lock(force=force):
                click.echo("Error: Could not acquire lock.", err=True)
                sys.exit(1)

            # Register cleanup handler to release lock on exit
            lock_acquired = True
            atexit.register(_cleanup_lock)

        record_startup_options(sys.argv, os.environ)
        maybe_run_auto_update()


def _cleanup_lock() -> None:
    """Release the lock file on program exit."""
    global _lock_manager
    if _lock_manager is not None:
        _lock_manager.release_lock()


# Set the command name to 'auto-coder' when used as a CLI
main.name = "auto-coder"


# Register main commands
main.add_command(process_issues)
main.add_command(create_feature_issues)
main.add_command(fix_to_pass_tests_command)
main.add_command(get_actions_logs)
main.add_command(auth_status)
main.add_command(migrate_branches)

# Register command groups
main.add_command(config_group)
main.add_command(graphrag_group)
main.add_command(lock_group)
main.add_command(mcp_group)
main.add_command(mcp_pdb_group)


if __name__ == "__main__":
    main()
