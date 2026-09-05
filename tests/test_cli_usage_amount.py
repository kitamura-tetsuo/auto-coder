"""Unit tests for the usage-amount CLI command."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from auto_coder.claude_usage_checker import (
    ClaudeCredentialResolution,
    ClaudeExtraUsage,
    ClaudeUsageQuota,
    ClaudeUsageWindow,
)
from auto_coder.cli import main
from auto_coder.codex_usage_checker import CodexOAuthCredentials, CodexResetCredits, CodexWeeklyUsage


def _mock_claude_quota(insufficient: bool = False, reason: str = "") -> ClaudeUsageQuota:
    return ClaudeUsageQuota(
        five_hour=ClaudeUsageWindow(utilization=40.0, resets_at="2026-08-24T12:00:00Z"),
        seven_day=ClaudeUsageWindow(utilization=30.0, resets_at="2026-08-30T00:00:00Z"),
        seven_day_sonnet=ClaudeUsageWindow(utilization=25.0, resets_at="2026-08-30T00:00:00Z"),
        extra_usage=ClaudeExtraUsage(
            is_enabled=True,
            monthly_limit=50.0,
            used_credits=12.5,
            utilization=25.0,
            currency="$",
        ),
        is_quota_insufficient=insufficient,
        reason=reason,
    )


def _mock_codex_usage(
    can_start: bool = True,
    reset_credits: CodexResetCredits = CodexResetCredits(available_count=2, status="available"),
) -> CodexWeeklyUsage:
    return CodexWeeklyUsage(
        remaining_percent=75.0 if can_start else 3.0,
        reset_at=datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc),
        days_until_reset=6,
        minimum_remaining_percent=5.0,
        reset_credits=reset_credits,
    )


class TestUsageAmountCLI:
    """Tests for auto-coder usage-amount command."""

    def test_usage_amount_help(self):
        """Test usage-amount --help output."""
        runner = CliRunner()
        result = runner.invoke(main, ["usage-amount", "--help"])
        assert result.exit_code == 0
        assert "usage-amount" in result.output
        assert "Claude" in result.output
        assert "Codex" in result.output
        assert "--backend" in result.output
        assert "--json" in result.output

    def test_usage_amount_default_both_backends(self):
        """Test default execution showing both Claude and Codex usage."""
        runner = CliRunner()
        with (
            patch("auto_coder.cli_commands_usage.acquire_claude_usage_credential", return_value=ClaudeCredentialResolution("valid-token", "resolved")),
            patch("auto_coder.cli_commands_usage.check_claude_usage", return_value=_mock_claude_quota()),
            patch("auto_coder.cli_commands_usage.load_codex_oauth_credentials", return_value=CodexOAuthCredentials("token", "acct")),
            patch("auto_coder.cli_commands_usage.get_codex_weekly_usage", return_value=_mock_codex_usage(True)),
        ):
            result = runner.invoke(main, ["usage-amount"])
            assert result.exit_code == 0
            assert "Claude Usage (Anthropic OAuth)" in result.output
            assert "5-hour Window: 40.0% used, 60.0% remaining" in result.output
            assert "7-day Window: 30.0% used, 70.0% remaining" in result.output
            assert "Extra Usage: Status: Enabled" in result.output
            assert "Codex Usage (ChatGPT OAuth)" in result.output
            assert "Weekly Window: 25.0% used, 75.0% remaining" in result.output
            assert "Task Start Allowed: Yes" in result.output
            assert "Reset Credits: 2" in result.output

    def test_usage_amount_claude_only_target_arg(self):
        """Test usage-amount claude only outputs Claude usage."""
        runner = CliRunner()
        with (
            patch("auto_coder.cli_commands_usage.acquire_claude_usage_credential", return_value=ClaudeCredentialResolution("valid-token", "resolved")),
            patch("auto_coder.cli_commands_usage.check_claude_usage", return_value=_mock_claude_quota()),
        ):
            result = runner.invoke(main, ["usage-amount", "claude"])
            assert result.exit_code == 0
            assert "Claude Usage (Anthropic OAuth)" in result.output
            assert "Codex Usage" not in result.output

    def test_usage_amount_claude_only_backend_option(self):
        """Test usage-amount --backend claude only outputs Claude usage."""
        runner = CliRunner()
        with (
            patch("auto_coder.cli_commands_usage.acquire_claude_usage_credential", return_value=ClaudeCredentialResolution("valid-token", "resolved")),
            patch("auto_coder.cli_commands_usage.check_claude_usage", return_value=_mock_claude_quota()),
        ):
            result = runner.invoke(main, ["usage-amount", "--backend", "claude"])
            assert result.exit_code == 0
            assert "Claude Usage (Anthropic OAuth)" in result.output
            assert "Codex Usage" not in result.output

    def test_usage_amount_codex_only_target_arg(self):
        """Test usage-amount codex only outputs Codex usage."""
        runner = CliRunner()
        with (
            patch("auto_coder.cli_commands_usage.load_codex_oauth_credentials", return_value=CodexOAuthCredentials("token", "acct")),
            patch("auto_coder.cli_commands_usage.get_codex_weekly_usage", return_value=_mock_codex_usage(True)),
        ):
            result = runner.invoke(main, ["usage-amount", "codex"])
            assert result.exit_code == 0
            assert "Codex Usage (ChatGPT OAuth)" in result.output
            assert "Claude Usage" not in result.output

    def test_usage_amount_codex_only_backend_option(self):
        """Test usage-amount -b codex only outputs Codex usage."""
        runner = CliRunner()
        with (
            patch("auto_coder.cli_commands_usage.load_codex_oauth_credentials", return_value=CodexOAuthCredentials("token", "acct")),
            patch("auto_coder.cli_commands_usage.get_codex_weekly_usage", return_value=_mock_codex_usage(True)),
        ):
            result = runner.invoke(main, ["usage-amount", "-b", "codex"])
            assert result.exit_code == 0
            assert "Codex Usage (ChatGPT OAuth)" in result.output
            assert "Claude Usage" not in result.output

    def test_usage_amount_json_output(self):
        """Test usage-amount --json formats valid JSON."""
        runner = CliRunner()
        with (
            patch("auto_coder.cli_commands_usage.acquire_claude_usage_credential", return_value=ClaudeCredentialResolution("valid-token", "resolved")),
            patch("auto_coder.cli_commands_usage.check_claude_usage", return_value=_mock_claude_quota()),
            patch("auto_coder.cli_commands_usage.load_codex_oauth_credentials", return_value=CodexOAuthCredentials("token", "acct")),
            patch("auto_coder.cli_commands_usage.get_codex_weekly_usage", return_value=_mock_codex_usage(True)),
        ):
            result = runner.invoke(main, ["usage-amount", "--json"])
            assert result.exit_code == 0
            parsed = json.loads(result.output)
            assert "claude" in parsed
            assert "codex" in parsed
            assert parsed["claude"]["available"] is True
            assert parsed["claude"]["status"] == "ok"
            assert parsed["codex"]["available"] is True
            assert parsed["codex"]["can_start_task"] is True
            assert parsed["codex"]["remaining_percent"] == 75.0
            assert parsed["codex"]["reset_credit_count"] == 2

    def test_codex_text_output_distinguishes_zero_from_missing_reset_credits(self):
        runner = CliRunner()
        credentials = CodexOAuthCredentials("token", "acct")
        cases = (
            (CodexResetCredits(0, "available"), "Reset Credits: 0", "Unavailable"),
            (CodexResetCredits(None, "missing"), "Reset Credits: Unavailable (missing)", "Reset Credits: 0"),
        )
        for credits, expected, forbidden in cases:
            with (
                patch("auto_coder.cli_commands_usage.load_codex_oauth_credentials", return_value=credentials),
                patch("auto_coder.cli_commands_usage.get_codex_weekly_usage", return_value=_mock_codex_usage(reset_credits=credits)),
            ):
                result = runner.invoke(main, ["usage-amount", "codex"], env={"NO_COLOR": "1"})
            assert result.exit_code == 0
            assert expected in result.output
            assert forbidden not in result.output

    def test_codex_json_output_distinguishes_zero_from_missing_reset_credits(self):
        runner = CliRunner()
        credentials = CodexOAuthCredentials("token", "acct")
        cases = (
            (CodexResetCredits(0, "available"), 0, "available"),
            (CodexResetCredits(None, "missing"), None, "missing"),
        )
        for credits, expected_count, expected_status in cases:
            with (
                patch("auto_coder.cli_commands_usage.load_codex_oauth_credentials", return_value=credentials),
                patch("auto_coder.cli_commands_usage.get_codex_weekly_usage", return_value=_mock_codex_usage(reset_credits=credits)),
            ):
                result = runner.invoke(main, ["usage-amount", "codex", "--json"])
            assert result.exit_code == 0
            report = json.loads(result.output)["codex"]
            assert report["reset_credit_count"] is expected_count
            assert report["reset_credit_status"] == expected_status

    def test_usage_amount_claude_missing_credentials(self):
        """Test usage-amount when Claude OAuth token is missing."""
        runner = CliRunner()
        status_process = MagicMock(returncode=1, stdout=json.dumps({"loggedIn": False}), stderr="Not logged in")
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("auto_coder.claude_usage_checker._read_credentials_file", return_value=None),
            patch("auto_coder.claude_usage_checker.subprocess.run", return_value=status_process),
        ):
            result = runner.invoke(main, ["usage-amount", "claude"])

        assert result.exit_code == 0
        assert "No Claude OAuth credentials found" in result.output
        assert "claude auth login" in result.output

    def test_usage_amount_authenticated_cli_session_reaches_usage_api(self, tmp_path):
        """Exercise a macOS Keychain login through acquisition and the usage request."""

        def run_claude(command, **kwargs):
            process = MagicMock(returncode=0, stderr="")
            if command[-3:] == ["auth", "status", "--json"]:
                process.stdout = json.dumps({"loggedIn": True})
            elif command[0] == "security":
                process.stdout = json.dumps({"claudeAiOauth": {"accessToken": "session-token", "expiresAt": 9_999_999_999_999}})
            else:
                process.stdout = "pong"
            return process

        usage_response = MagicMock()
        usage_response.__enter__.return_value.read.return_value = json.dumps({"five_hour": {"utilization": 12.0}}).encode()
        runner = CliRunner()
        with (
            patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, clear=True),
            patch("auto_coder.claude_usage_checker.platform.system", return_value="Darwin"),
            patch("auto_coder.claude_usage_checker.subprocess.run", side_effect=run_claude),
            patch("auto_coder.claude_usage_checker.urllib.request.urlopen", return_value=usage_response) as request,
        ):
            result = runner.invoke(main, ["usage-amount", "claude", "--no-cache"])

        assert result.exit_code == 0
        assert "Quota Status: OK" in result.output
        assert "missing" not in result.output.lower()
        assert request.call_count == 1
        assert request.call_args.args[0].headers["Authorization"] == "Bearer session-token"

    def test_usage_amount_reports_authenticated_acquisition_failure(self):
        runner = CliRunner()
        credential = ClaudeCredentialResolution(status="credential_acquisition_failed")
        with patch("auto_coder.cli_commands_usage.acquire_claude_usage_credential", return_value=credential):
            result = runner.invoke(main, ["usage-amount", "claude"])

        assert result.exit_code == 0
        assert "Claude Code is authenticated" in result.output
        assert "claude auth login" not in result.output

    def test_usage_amount_codex_missing_credentials(self):
        """Test usage-amount when Codex OAuth credentials are missing."""
        runner = CliRunner()
        with (patch("auto_coder.cli_commands_usage.load_codex_oauth_credentials", return_value=None),):
            result = runner.invoke(main, ["usage-amount", "codex"])
            assert result.exit_code == 0
            assert "Codex OAuth credentials are missing" in result.output

    def test_usage_amount_insufficient_quota_displays_warning(self):
        """Test usage-amount displays warning when quota is insufficient."""
        runner = CliRunner()
        insufficient_quota = _mock_claude_quota(insufficient=True, reason="5-hour limit reached")
        with (
            patch("auto_coder.cli_commands_usage.acquire_claude_usage_credential", return_value=ClaudeCredentialResolution("valid-token", "resolved")),
            patch("auto_coder.cli_commands_usage.check_claude_usage", return_value=insufficient_quota),
            patch("auto_coder.cli_commands_usage.load_codex_oauth_credentials", return_value=CodexOAuthCredentials("token", "acct")),
            patch("auto_coder.cli_commands_usage.get_codex_weekly_usage", return_value=_mock_codex_usage(can_start=False)),
        ):
            result = runner.invoke(main, ["usage-amount"])
            assert result.exit_code == 0
            assert "Quota Status: Insufficient" in result.output
            assert "5-hour limit reached" in result.output
            assert "Task Start Allowed: No" in result.output
