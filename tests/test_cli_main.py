"""
Tests for main CLI commands and help functionality.
"""

from click.testing import CliRunner

from src.auto_coder.cli import create_feature_issues, main


class TestCLIMain:
    """Test cases for main CLI commands."""

    def test_main_command_help(self):
        """Test main command help output."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0
        assert "Auto-Coder" in result.output
        assert "Automated application development" in result.output

    def test_main_command_version(self):
        """Test main command version output."""
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])

        assert result.exit_code == 0

    def test_create_feature_issues_help(self):
        """Test create-feature-issues command help."""
        runner = CliRunner()
        result = runner.invoke(create_feature_issues, ["--help"])

        assert result.exit_code == 0
        assert "--repo" in result.output
        assert "--github-token" in result.output
        assert "--backend" in result.output
        assert "--model-gemini" in result.output
        assert "--model-qwen" in result.output
        assert "--model-auggie" in result.output
        assert "--verbose" in result.output
