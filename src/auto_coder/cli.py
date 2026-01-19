"""Command Line Interface for Auto-Coder."""

import contextlib
import os
import sys
from os import PathLike
from typing import IO

print(f"DEBUG: sys.executable = {sys.executable}", file=sys.stderr)

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
from .cli_commands_debug import debug
from .cli_commands_graphrag import graphrag_group
from .cli_commands_lock import lock_group, unlock
from .cli_commands_main import create_feature_issues, fix_to_pass_tests_command, process_issues
from .cli_commands_mcp import mcp_group
from .cli_commands_mcp_pdb import mcp_pdb_group
from .cli_commands_utils import auth_status, get_actions_logs, migrate_branches
from .cli_helpers import qwen_help_has_flags  # Re-export for tests
from .cli_ui import print_lock_error
from .lock_manager import LockManager
from .update_manager import maybe_run_auto_update, record_startup_options

# Load environment variables
load_dotenv()


class ForceAwareGroup(click.Group):
    """Custom Group to handle global --force flag positioned after subcommand."""

    def invoke(self, ctx):
        if "--force" in ctx.args:
            try:
                # Determine which command would be invoked
                # Note: ctx.protected_args contains the command name when invoke_without_command=True
                check_args = ctx.protected_args + ctx.args
                cmd_name, cmd, _ = self.resolve_command(ctx, check_args)

                if cmd:
                    # Check if the command supports --force
                    supports_force = False
                    for param in cmd.params:
                        if final_name := getattr(param, "name", None):
                            if final_name == "force":
                                supports_force = True
                                break
                        # Also check opts just in case
                        if "--force" in getattr(param, "opts", []):
                            supports_force = True
                            break

                    if not supports_force:
                        # Command doesn't support --force, so it must be intended for the main group
                        # Strip it from args to prevent "No such option" error
                        if "--force" in ctx.args:
                            ctx.args.remove("--force")
                            # Enable force on the main group context
                            ctx.params["force"] = True
            except Exception:
                # If resolution fails, let standard click mechanics handle the error
                pass

        return super().invoke(ctx)


@contextlib.contextmanager
def lock_manager_context(force: bool = False):
    """Context manager wrapper for LockManager that handles force flag."""
    lock_manager = LockManager()
    if not lock_manager.acquire_lock(force=force):
        raise RuntimeError("Failed to acquire lock")
    try:
        yield lock_manager
    finally:
        lock_manager.release_lock()


@click.group(
    cls=ForceAwareGroup,
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
        # Determine which command is being invoked
        invoked_cmd = ctx.invoked_subcommand if hasattr(ctx, "invoked_subcommand") else None

        # Skip lock check for read-only commands
        read_only_commands = ["config", "auth-status", "unlock", "get-actions-logs", "mcp-pdb"]
        is_unlock = invoked_cmd == "unlock" or "unlock" in sys.argv

        if not (invoked_cmd in read_only_commands or has_help_flag or is_unlock):
            # Create LockManager and check for existing lock
            lock_manager = LockManager()

            # Check for existing lock before attempting to acquire
            if lock_manager.is_locked() and not force:
                lock_info = lock_manager.get_lock_info_obj()
                if lock_info:
                    is_running = lock_manager._is_process_running(lock_info.pid)
                    print_lock_error(lock_info, is_running)
                    sys.exit(1)
                else:
                    # Lock file exists but couldn't be read
                    click.echo("Error: Lock file exists but is corrupted.", err=True)
                    if force:
                        click.echo("Removing corrupted lock file...", err=True)
                        lock_manager.release_lock()
                    else:
                        click.echo("Use '--force' to remove the corrupted lock file.", err=True)
                        sys.exit(1)

            # Use LockManager as a context manager with force flag
            # Store in ctx.with_resource to keep it alive for the entire command execution
            ctx.with_resource(lock_manager_context(force))

        record_startup_options(sys.argv, os.environ)
        maybe_run_auto_update()


# Set the command name to 'auto-coder' when used as a CLI
main.name = "auto-coder"


# Register main commands
main.add_command(process_issues)
main.add_command(create_feature_issues)
main.add_command(fix_to_pass_tests_command)

# Register commands and command groups
main.add_command(config_group)
main.add_command(graphrag_group)
main.add_command(lock_group)  # Keep for backward compatibility
main.add_command(mcp_group)
main.add_command(mcp_pdb_group)

# Register top-level utility commands
main.add_command(get_actions_logs)
main.add_command(auth_status)
main.add_command(migrate_branches)
main.add_command(unlock)
main.add_command(debug)


if __name__ == "__main__":
    main()
