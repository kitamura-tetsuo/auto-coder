"""
Tests for main CLI commands and help functionality.
"""

from click.testing import CliRunner

from auto_coder.cli import create_feature_issues, main, process_issues


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

    def test_process_issues_help_includes_skip_flag(self):
        runner = CliRunner()
        result = runner.invoke(process_issues, ["--help"])
        assert result.exit_code == 0
        # Click help may split flag across lines; check presence of at least one alias
        assert "--skip-main-update" in result.output

    def test_process_issues_help_includes_only_flag(self):
        runner = CliRunner()
        result = runner.invoke(process_issues, ["--help"])
        assert result.exit_code == 0
        assert "--only" in result.output
        # New option should appear in help
        assert "--ignore-dependabot-prs" in result.output

    def test_process_issues_help(self):
        """Test process-issues command help."""
        runner = CliRunner()
        result = runner.invoke(process_issues, ["--help"])

        assert result.exit_code == 0
        assert "--repo" in result.output
        assert "--github-token" in result.output
        # Backend options are no longer available (replaced by configuration file)
        assert "--backend" not in result.output
        assert "--model-gemini" not in result.output
        assert "--model-qwen" not in result.output
        assert "--model-auggie" not in result.output
        assert "--verbose" in result.output

    def test_create_feature_issues_help(self):
        """Test create-feature-issues command help."""
        runner = CliRunner()
        result = runner.invoke(create_feature_issues, ["--help"])

        assert result.exit_code == 0
        assert "--repo" in result.output
        assert "--github-token" in result.output
        # Backend options are no longer available (replaced by configuration file)
        assert "--backend" not in result.output
        assert "--model-gemini" not in result.output
        assert "--model-qwen" not in result.output
        assert "--model-auggie" not in result.output
        assert "--verbose" in result.output
