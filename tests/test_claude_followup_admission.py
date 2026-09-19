"""Regression tests for strict Claude existing-session admission."""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.claude_routine_client import ClaudeRoutineClient
from auto_coder.claude_usage_checker import (
    ClaudeStrictUsageObservation,
    observe_claude_usage_strict,
)
from auto_coder.exceptions import (
    ClaudeFollowupDeferralReason,
    ClaudeFollowupUsageLimitError,
    DeliveryCertainty,
)
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class UsageResponse:
    """Minimal urlopen response containing provider-shaped JSON."""

    def __init__(self, payload: dict) -> None:
        self.body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.body.read()


def make_client(oauth_token: str | None = "oauth-selected") -> ClaudeRoutineClient:
    config = LLMBackendConfiguration()
    config.backends["claude-alias"] = BackendConfig(
        name="claude-alias",
        backend_type="claude-routine",
        claude_code_routine_token="routine-trigger",
        claude_code_oauth_token=oauth_token,
        usage_markers=['{"error":{"code":"quota_stop"}}'],
    )
    with patch("auto_coder.claude_routine_client.get_llm_config", return_value=config):
        return ClaudeRoutineClient("claude-alias", repo_name="owner/repo")


@pytest.mark.parametrize(
    ("utilization", "insufficient"),
    [(80, True), (80.1, True), (79.9, False)],
)
def test_strict_usage_parser_enforces_five_hour_boundary(utilization, insufficient):
    response = UsageResponse({"five_hour": {"utilization": utilization}, "seven_day": None})
    with patch("auto_coder.claude_usage_checker.urllib.request.urlopen", return_value=response) as request:
        observation = observe_claude_usage_strict("selected-secret", now=1000)

    assert observation.available is True
    assert observation.insufficient is insufficient
    assert observation.blockers == (("five_hour",) if insufficient else ())
    assert request.call_args.args[0].headers["Authorization"] == "Bearer selected-secret"


@pytest.mark.parametrize("bad_value", [True, float("nan"), float("inf"), -1, 101, "80"])
def test_strict_usage_parser_rejects_malformed_percentages(bad_value):
    response = UsageResponse({"five_hour": {"utilization": bad_value}})
    with patch("auto_coder.claude_usage_checker.urllib.request.urlopen", return_value=response):
        observation = observe_claude_usage_strict("oauth", now=1000)

    assert observation.available is False
    assert observation.insufficient is False


def test_followup_insufficient_quota_is_typed_and_never_sent():
    client = make_client()
    observation = ClaudeStrictUsageObservation(
        available=True,
        insufficient=True,
        blockers=("five_hour",),
        blocker_resets=(1600,),
        detail="five_hour",
    )
    with (
        patch("auto_coder.claude_routine_client.time.time", return_value=1000),
        patch("auto_coder.claude_routine_client.observe_claude_usage_strict", return_value=observation) as usage,
        patch("auto_coder.claude_routine_client.CommandExecutor.run_command") as command,
        pytest.raises(ClaudeFollowupUsageLimitError) as raised,
    ):
        client.send_followup("session-1", "fix it")

    assert raised.value.reason is ClaudeFollowupDeferralReason.QUOTA_INSUFFICIENT
    assert raised.value.delivery_certainty is DeliveryCertainty.NOT_SENT
    assert raised.value.repository == "owner/repo"
    assert raised.value.backend_name == "claude-alias"
    assert raised.value.retry_not_before == 1600
    assert "oauth-selected" not in raised.value.credential_context
    usage.assert_called_once_with("oauth-selected", now=1000)
    command.assert_not_called()


def test_followup_uses_oauth_not_trigger_token_and_succeeds_once():
    client = make_client()
    eligible = ClaudeStrictUsageObservation(available=True, detail="eligible")
    result = MagicMock(returncode=0, stdout="completed", stderr="")
    with (
        patch("auto_coder.claude_routine_client.observe_claude_usage_strict", return_value=eligible),
        patch("auto_coder.claude_routine_client.CommandExecutor.run_command", return_value=result) as command,
    ):
        assert client.send_followup("session-1", "fix it") is True

    args, kwargs = command.call_args
    assert args[0] == ["claude", "-p", "--cloud=session-1", "fix it"]
    assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-selected"
    assert kwargs["env"]["CLAUDE_CODE_ROUTINE_TOKEN"] == "routine-trigger"
    assert client.active_sessions == {"session-1": "fix it"}


def test_provider_limit_on_zero_exit_is_not_successful_delivery():
    client = make_client()
    eligible = ClaudeStrictUsageObservation(available=True, detail="eligible")
    result = MagicMock(
        returncode=0,
        stdout='{"error":{"type":"rate_limit_error","message":"assignment rejected"}}',
        stderr="",
    )
    with (
        patch("auto_coder.claude_routine_client.observe_claude_usage_strict", return_value=eligible),
        patch("auto_coder.claude_routine_client.CommandExecutor.run_command", return_value=result) as command,
        pytest.raises(ClaudeFollowupUsageLimitError) as raised,
    ):
        client.send_followup("session-1", "fix it")

    assert raised.value.reason is ClaudeFollowupDeferralReason.PROVIDER_USAGE_LIMIT
    assert raised.value.delivery_certainty is DeliveryCertainty.NOT_SENT
    assert client.active_sessions == {}
    command.assert_called_once()


def test_task_echo_and_bare_429_are_not_provider_limit():
    client = make_client()
    eligible = ClaudeStrictUsageObservation(available=True, detail="eligible")
    message = "test rate_limit_error and 429 tests passed"
    result = MagicMock(returncode=0, stdout=message, stderr="")
    with (
        patch("auto_coder.claude_routine_client.observe_claude_usage_strict", return_value=eligible),
        patch("auto_coder.claude_routine_client.CommandExecutor.run_command", return_value=result),
    ):
        assert client.send_followup("session-1", message) is True


def test_missing_oauth_does_not_use_routine_token_or_ping():
    client = make_client(oauth_token=None)
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("auto_coder.claude_usage_checker._read_credentials_file", return_value=None),
        patch("auto_coder.claude_routine_client.CommandExecutor.run_command") as command,
        pytest.raises(ClaudeFollowupUsageLimitError) as raised,
    ):
        client.send_followup("session-1", "fix it")

    assert raised.value.reason is ClaudeFollowupDeferralReason.QUOTA_UNAVAILABLE
    assert raised.value.delivery_certainty is DeliveryCertainty.NOT_SENT
    command.assert_not_called()
