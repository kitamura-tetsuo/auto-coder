"""MCP-PDB setup utility CLI commands."""

import json
from typing import Optional

import click

from .logger_config import get_logger, setup_logger

logger = get_logger(__name__)


@click.group(name="mcp-pdb")
def mcp_pdb_group() -> None:
    """MCP-PDB のセットアップ支援ユーティリティ。

    - print-config: Windsurf/Claude 用の設定スニペットを出力
    - status: 必要な前提コマンドの存在を確認
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
    help="出力先ツールの種類 (windsurf|claude)",
)
@click.option(
    "--write-to",
    type=click.Path(dir_okay=False, resolve_path=True),
    help="出力内容をファイルにも保存するパス (任意)",
)
def mcp_pdb_print_config(target: str, write_to: Optional[str]) -> None:
    """mcp-pdb の設定を出力（必要に応じてファイル保存）。"""
    setup_logger()  # 標準設定
    if target.lower() == "windsurf":
        content = _windsurf_mcp_config_snippet()
    else:
        # Claude Code 用はコマンドラインをそのまま提示
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
    """mcp-pdb 利用に必要な前提コマンドの存在確認を行う。"""
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
    click.echo("  - Windsurf: settings.json に mcpServers を追加")
    click.echo(
        "  - Claude Code: 'claude mcp add mcp-pdb -- uv run --with mcp-pdb mcp-pdb'"
    )

