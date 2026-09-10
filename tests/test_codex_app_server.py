"""Exercise the stdio handshake, interleaved messages, and process cleanup."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder.codex_app_server import read_account_data
from auto_coder.codex_usage_checker import get_codex_account_type


@pytest.mark.parametrize("mode", ["success", "error", "eof", "invalid", "timeout", "oversized"])
def test_stdio_protocol_and_cleanup(mode, tmp_path):
    real_popen = subprocess.Popen
    processes = []

    def start(command, **kwargs):
        assert command == ["codex", "app-server"]
        process = real_popen([sys.executable, str(Path(__file__).parent / "fixtures" / "codex_account_server.py"), mode, str(tmp_path / "requests.jsonl")], **kwargs)
        processes.append(process)
        return process

    with patch("auto_coder.codex_app_server.subprocess.Popen", side_effect=start):
        if mode == "success":
            assert read_account_data("account/rateLimits/read", timeout=2) == {"rateLimits": {"limitId": "codex"}}
        else:
            expected = {"error": ValueError, "eof": EOFError, "invalid": ValueError, "timeout": TimeoutError, "oversized": ValueError}[mode]
            with pytest.raises(expected):
                read_account_data("account/rateLimits/read", timeout=0.5 if mode == "timeout" else 2)
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed
    assert processes[0].stdout.closed
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert [request["method"] for request in requests] == ["initialize", "initialized", "account/rateLimits/read"]
    assert requests[0]["params"]["clientInfo"]["name"] == "auto_coder"


def test_mutating_methods_are_rejected_before_launch():
    with patch("auto_coder.codex_app_server.subprocess.Popen") as start:
        with pytest.raises(ValueError):
            read_account_data("account/rateLimitResetCredit/consume")
    start.assert_not_called()


@pytest.mark.parametrize("account, expected", [({"type": "apiKey"}, "apiKey"), ({"type": "chatgpt"}, "chatgpt"), (None, None), ({}, None)])
def test_account_type_uses_codex_managed_authentication(account, expected):
    with patch("auto_coder.codex_usage_checker.read_account_data", return_value={"account": account}) as read:
        assert get_codex_account_type() == expected
    read.assert_called_once_with("account/read")


def test_account_read_failure_is_not_api_key_authentication():
    with patch("auto_coder.codex_usage_checker.read_account_data", side_effect=TimeoutError()):
        assert get_codex_account_type() is None
