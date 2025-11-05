"""Command Line Interface for Auto-Coder."""

import os
import sys

import click

try:
    from dotenv import load_dotenv
except ImportError:
    # Fallback if python-dotenv is not installed
    def load_dotenv():
        """No-op function when python-dotenv is not installed."""
        pass


from . import __version__ as AUTO_CODER_VERSION
from .cli_commands_graphrag import graphrag_group
from .cli_commands_main import create_feature_issues, fix_to_pass_tests_command, process_issues
from .cli_commands_mcp import mcp_group
from .cli_commands_mcp_pdb import mcp_pdb_group
from .cli_commands_utils import auth_status, get_actions_logs
from .cli_helpers import qwen_help_has_flags  # Re-export for tests
from .update_manager import maybe_run_auto_update, record_startup_options

# Load environment variables
load_dotenv()


@click.group()
@click.version_option(version=AUTO_CODER_VERSION, package_name="auto-coder")
def main() -> None:
    """Auto-Coder: Automated application development using Gemini CLI and GitHub integration."""
    record_startup_options(sys.argv, os.environ)
    maybe_run_auto_update()


# Register main commands
main.add_command(process_issues)
main.add_command(create_feature_issues)
main.add_command(fix_to_pass_tests_command)
main.add_command(get_actions_logs)
main.add_command(auth_status)

# Register command groups
main.add_command(graphrag_group)
main.add_command(mcp_group)
main.add_command(mcp_pdb_group)


if __name__ == "__main__":
    main()
