"""MCP-PDB setup utility CLI commands."""

import json
from typing import Optional

import click

from .logger_config import get_logger, setup_logger

logger = get_logger(__name__)


@click.group(name="mcp-pdb")
def mcp_pdb_group() -> None:
    """MCP-PDB setup helper utilities.

    - print-config: Output configuration snippets for Windsurf/Claude
    - status: Check for prerequisite commands
    """
    pass


def _windsurf_mcp_config_snippet() -> str:
    """Generate Windsurf MCP configuration snippet."""
    return json.dumps(
        {
            "mcpServers": {
                "mcp-pdb": {
                    "command": "uv",
                    "args": ["run", "--with", "mcp-pdb", "mcp-pdb"],
                }
            }
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp_pdb_group.command("print-config")
@click.option(
    "--target",
    type=click.Choice(["windsurf", "claude"], case_sensitive=False),
    default="windsurf",
    help="Target tool type to output for (windsurf|claude)",
)
@click.option(
    "--write-to",
    type=click.Path(dir_okay=False, resolve_path=True),
    help="Path to also write the output to a file (optional)",
)
def mcp_pdb_print_config(target: str, write_to: Optional[str]) -> None:
    """Output mcp-pdb configuration (optionally save to file)."""
    setup_logger()  # default settings
    if target.lower() == "windsurf":
        content = _windsurf_mcp_config_snippet()
    else:
        # For Claude Code, present the command line as-is
        content = (
            "# Install the MCP server\n"
            "claude mcp add mcp-pdb -- uv run --with mcp-pdb mcp-pdb\n\n"
            "# Alternative: Install with specific Python version\n"
            "claude mcp add mcp-pdb -- uv run --python 3.13 --with mcp-pdb mcp-pdb\n\n"
            "# Note: The -- separator is required for Claude Code CLI\n"
        )

    click.echo(content)
    if write_to:
        try:
            with open(write_to, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Wrote mcp-pdb config to: {write_to}")
        except Exception as e:
            raise click.ClickException(f"Failed to write file: {e}")


@mcp_pdb_group.command("status")
def mcp_pdb_status() -> None:
    """Check for prerequisite commands required to use mcp-pdb."""
    setup_logger()
    click.echo("Checking MCP-PDB prerequisites...\n")

    # uv の存在確認
    try:
        import subprocess as _sp

        res = _sp.run(["uv", "--version"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            ver = (res.stdout or "").strip()
            click.echo("✅ uv available")
            if ver:
                click.echo(f"   {ver}")
        else:
            click.echo("❌ uv not working")
    except Exception:
        click.echo("❌ uv not found")
        click.echo("   Install uv: https://docs.astral.sh/uv/")

    click.echo()
    click.echo("Setup tips:")
    click.echo("  - Windsurf: add mcpServers to settings.json")
    click.echo(
        "  - Claude Code: 'claude mcp add mcp-pdb -- uv run --with mcp-pdb mcp-pdb'"
    )
