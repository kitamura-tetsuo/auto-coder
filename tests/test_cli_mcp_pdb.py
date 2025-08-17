"""
Tests for mcp-pdb CLI helper commands.
"""

from click.testing import CliRunner
from src.auto_coder.cli import main


class TestMCPPDBCLI:
    def test_mcp_pdb_group_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["mcp-pdb", "--help"])
        assert result.exit_code == 0
        assert "MCP-PDB" in result.output
        assert "print-config" in result.output
        assert "status" in result.output

    def test_mcp_pdb_print_config_windsurf(self):
        runner = CliRunner()
        result = runner.invoke(main, ["mcp-pdb", "print-config", "--target", "windsurf"])
        assert result.exit_code == 0
        # Expects a JSON snippet with keys
        assert '"mcpServers"' in result.output
        assert '"mcp-pdb"' in result.output
        assert '"command"' in result.output
        assert '"uv"' in result.output

    def test_mcp_pdb_print_config_claude(self):
        runner = CliRunner()
        result = runner.invoke(main, ["mcp-pdb", "print-config", "--target", "claude"])
        assert result.exit_code == 0
        assert "claude mcp add mcp-pdb -- uv run --with mcp-pdb mcp-pdb" in result.output
        assert "--python 3.13 --with mcp-pdb mcp-pdb" in result.output

    def test_mcp_pdb_status(self):
        runner = CliRunner()
        result = runner.invoke(main, ["mcp-pdb", "status"])
        assert result.exit_code == 0
        # Our test conftest stubs uv --version as success
        assert "uv" in result.output
        assert "Setup tips" in result.output

