"""Production-path regression coverage for the OpenCode local backend (Issue #2124)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path
from typing import Optional, Tuple
from unittest.mock import patch

import pytest
from click import ClickException

from src.auto_coder.cli_helpers import build_backend_manager, check_backend_prerequisites
from src.auto_coder.exceptions import AutoCoderRetryableBackendError, AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.opencode_client import OpenCodeClient
from src.auto_coder.prompt_loader import render_prompt


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "OpenCode Tests")
    (repo / "tracked.txt").write_text("before\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


# Generic fake `opencode` executable. Behavior is driven entirely by
# environment variables so individual tests stay short:
#   OPENCODE_TEST_REPORT_FILE  -> write a JSON report of argv/cwd/env/prompt digest
#   OPENCODE_TEST_STDOUT_FILE  -> file whose contents are copied verbatim to stdout
#   OPENCODE_TEST_STDERR_FILE  -> file whose contents are copied verbatim to stderr
#   OPENCODE_TEST_SENTINEL_FILE -> touched once the CLI actually launches (proves task-launch happened)
#   OPENCODE_TEST_BODY_FILE    -> extra python source exec'd with argv/prompt/os/sys/json in scope
#   OPENCODE_TEST_EXIT_CODE    -> process exit code (default 0)
#   OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE -> `debug agent <name>` stdout, with
#       every "__AGENT_NAME__" substituted for the actual (randomly generated)
#       agent name so tests don't need to predict it. Missing -> exit 1 (an
#       unavailable no-edit enforcement prerequisite).
#   OPENCODE_TEST_DEBUG_AGENT_EXIT_CODE -> `debug agent` exit code override
#   OPENCODE_TEST_SESSION_LIST_RESPONSE_FILE -> `session list --format json` stdout
#       (Issue #2126 REQ-006's workspace-association preflight). Missing -> "[]"
#       (no session associated with this directory), matching the real CLI's
#       own behavior for a directory with no sessions.
_DRIVER_SOURCE = textwrap.dedent("""
    #!/usr/bin/env python3
    import hashlib
    import json
    import os
    import sys

    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print("opencode 1.18.31")
        raise SystemExit(0)

    if argv[:2] == ["debug", "agent"]:
        agent_name = argv[2] if len(argv) > 2 else ""
        response_path = os.environ.get("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE")
        if response_path:
            sys.stdout.write(open(response_path).read().replace("__AGENT_NAME__", agent_name))
            raise SystemExit(int(os.environ.get("OPENCODE_TEST_DEBUG_AGENT_EXIT_CODE", "0")))
        sys.stderr.write("unknown command: debug agent\\n")
        raise SystemExit(int(os.environ.get("OPENCODE_TEST_DEBUG_AGENT_EXIT_CODE", "1")))

    if argv[:2] == ["session", "list"]:
        response_path = os.environ.get("OPENCODE_TEST_SESSION_LIST_RESPONSE_FILE")
        sys.stdout.write(open(response_path).read() if response_path else "[]")
        raise SystemExit(0)

    prompt = sys.stdin.buffer.read()

    sentinel = os.environ.get("OPENCODE_TEST_SENTINEL_FILE")
    if sentinel:
        open(sentinel, "w").close()

    report_path = os.environ.get("OPENCODE_TEST_REPORT_FILE")
    if report_path:
        decoded = prompt.decode("utf-8", "ignore")
        report = {
            "argv": argv,
            "cwd": os.getcwd(),
            "prompt_in_argv": any(decoded and decoded in a for a in argv),
            "prompt_in_env": any(decoded and decoded in v for v in os.environ.values()),
            "prompt_digest": hashlib.sha256(prompt).hexdigest(),
            "prompt_len": len(prompt),
            "prompt_text": decoded,
            "opencode_config_content": os.environ.get("OPENCODE_CONFIG_CONTENT"),
        }
        with open(report_path, "w") as fh:
            json.dump(report, fh)

    body_path = os.environ.get("OPENCODE_TEST_BODY_FILE")
    if body_path:
        exec(compile(open(body_path).read(), body_path, "exec"))

    stdout_path = os.environ.get("OPENCODE_TEST_STDOUT_FILE")
    if stdout_path:
        sys.stdout.write(open(stdout_path).read())

    stderr_path = os.environ.get("OPENCODE_TEST_STDERR_FILE")
    if stderr_path:
        sys.stderr.write(open(stderr_path).read())

    sys.exit(int(os.environ.get("OPENCODE_TEST_EXIT_CODE", "0")))
    """).strip()


def _driver(tmp_path: Path, name: str = "opencode") -> Path:
    script = tmp_path / name
    script.write_text(_DRIVER_SOURCE + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _event(event_type: str, session_id: str = "ses_root1", **data) -> str:
    return json.dumps({"type": event_type, "timestamp": 0, "sessionID": session_id, **data})


def _manager(config: LLMBackendConfiguration, backend_name: str | None = None):
    name = backend_name or next(iter(config.backends))
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        return build_backend_manager([name], name, {})


# ---------------------------------------------------------------------------
# AC-001: configuration reaches production execution
# ---------------------------------------------------------------------------


def test_alias_reaches_production_execution_with_multi_slash_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report = tmp_path / "report.json"
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "done"}) + "\n")

    config = LLMBackendConfiguration(
        backends={
            "opencode-router": BackendConfig(
                name="opencode-router",
                backend_type="opencode",
                model="openrouter/anthropic/claude-3.5-sonnet",
            )
        }
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))

    output = _manager(config)._run_llm_cli("implement the feature")

    assert output == "done"
    observed = json.loads(report.read_text())
    assert observed["argv"][0] == "run"
    assert "--format" in observed["argv"] and observed["argv"][observed["argv"].index("--format") + 1] == "json"
    assert observed["argv"][observed["argv"].index("--model") + 1] == "openrouter/anthropic/claude-3.5-sonnet"
    # The manager runs local backends inside an isolated worktree of `repo`, so
    # the effective directory need not equal `repo` itself, but `--dir` must
    # match the actual directory the process ran in.
    assert observed["argv"][observed["argv"].index("--dir") + 1] == observed["cwd"]


def test_config_without_opencode_needs_no_opencode_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = LLMBackendConfiguration(
        backends={
            "qwen": BackendConfig(name="qwen", backend_type="qwen"),
            "opencode-unused": BackendConfig(name="opencode-unused", backend_type="opencode", model="anthropic/claude"),
        }
    )
    fake_qwen_client = type("FakeQwen", (), {"model_name": "qwen", "_run_llm_cli": lambda self, prompt, is_noedit=False: "qwen-ok", "get_last_session_id": lambda self: None})()
    monkeypatch.delenv("AUTOCODER_OPENCODE_CLI", raising=False)
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.cli_helpers.shutil.which", return_value=None), patch("src.auto_coder.qwen_client.QwenClient", return_value=fake_qwen_client):
        manager = build_backend_manager(["qwen"], "qwen", {})
        assert manager._run_llm_cli("go") == "qwen-ok"


def test_missing_model_fails_before_prompt_submission() -> None:
    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode")})
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        with pytest.raises(RuntimeError, match="explicit 'model' value"):
            OpenCodeClient(backend_name="opencode")


def test_invalid_model_format_rejected() -> None:
    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="no-slash-model")})
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        with pytest.raises(RuntimeError, match="provider/model"):
            OpenCodeClient(backend_name="opencode")


def test_alias_prerequisite_failure_is_actionable() -> None:
    config = LLMBackendConfiguration(backends={"opencode-payg": BackendConfig(name="opencode-payg", backend_type="opencode", model="anthropic/claude")})
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.cli_helpers.shutil.which", return_value=None):
        with pytest.raises(ClickException, match="opencode CLI is not found in PATH"):
            check_backend_prerequisites(["opencode-payg"])


# ---------------------------------------------------------------------------
# AC-002: input transport and option authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [4096, 2 * 1024 * 1024])
def test_full_prompt_transported_via_stdin_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, size: int) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report = tmp_path / f"report-{size}.json"
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "ok"}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    task = (("Unicode ☃ @ ' \" ; $(false)\r\n" * ((size // 30) + 1))[:size]).rstrip("\n")
    expected = render_prompt("opencode.execution", task_prompt=task).encode("utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))

    assert _manager(config)._run_llm_cli(task) == "ok"

    observed = json.loads(report.read_text())
    assert observed["prompt_digest"] == hashlib.sha256(expected).hexdigest()
    assert observed["prompt_len"] == len(expected)
    assert observed["prompt_in_argv"] is False
    assert observed["prompt_in_env"] is False
    assert task not in observed["argv"]


_BAD_OPTIONS_BEFORE_LAUNCH = [
    ["--attach", "http://localhost:4096"],
    ["--command", "/help"],
    ["extra-positional-prompt"],
    ["--file", "foo.txt"],
    ["--continue"],
    ["-c"],
    ["--session", "abc123"],
    ["--fork"],
    ["--share"],
    ["--auto"],
    ["--yolo"],
    ["--dangerously-skip-permissions"],
    ["--mini"],
    ["--interactive"],
    ["--format", "text"],
    ["--dir", "/somewhere/else"],
]


@pytest.mark.parametrize("bad_options", _BAD_OPTIONS_BEFORE_LAUNCH)
def test_incompatible_overrides_rejected_before_task_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, bad_options: list[str]) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        client.set_extra_args(bad_options)

        with pytest.raises(RuntimeError):
            client._run_llm_cli("implement")

    assert not sentinel.exists()


def test_legitimate_variant_override_reaches_the_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report = tmp_path / "report.json"
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "ok"}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        client.set_extra_args(["--variant", "high"])
        assert client._run_llm_cli("implement") == "ok"

    observed = json.loads(report.read_text())
    assert observed["argv"][observed["argv"].index("--variant") + 1] == "high"


def test_model_override_takes_precedence_and_preserves_slashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report = tmp_path / "report.json"
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "ok"}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        client.set_extra_args(["--model", "openrouter/anthropic/claude-3.5-sonnet"])
        assert client._run_llm_cli("implement") == "ok"

    observed = json.loads(report.read_text())
    assert observed["argv"][observed["argv"].index("--model") + 1] == "openrouter/anthropic/claude-3.5-sonnet"


# ---------------------------------------------------------------------------
# AC-003: final-answer extraction
# ---------------------------------------------------------------------------


def _final_answer_via_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jsonl: str, exit_code: int = 0) -> str:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(jsonl)

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    monkeypatch.setenv("OPENCODE_TEST_EXIT_CODE", str(exit_code))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        return OpenCodeClient(backend_name="opencode")._run_llm_cli("implement")


def test_final_answer_ignores_tool_json_intermediate_messages_and_dedupes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    lines = [
        _event("step_start", part={"id": "sp1", "messageID": "m1"}),
        _event("tool_use", part={"id": "t1", "messageID": "m1", "state": {"status": "completed", "output": '{"result": "PASS"}'}}),
        _event("text", part={"id": "x1", "messageID": "m1", "text": "Intermediate note"}),
        _event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "tool-calls"}),
        _event("step_start", part={"id": "sp2", "messageID": "m2"}),
        _event("text", part={"id": "x2", "messageID": "m2", "text": "Final answer line one."}),
        _event("text", part={"id": "x2", "messageID": "m2", "text": "Final answer line one."}),
        _event("text", part={"id": "x3", "messageID": "m2", "text": "Final answer line two."}),
        _event("step_finish", part={"id": "sf2", "messageID": "m2", "reason": "stop"}),
    ]
    result = _final_answer_via_driver(tmp_path, monkeypatch, "\n".join(lines) + "\n")
    assert result == "Final answer line one.\n\nFinal answer line two."
    assert "PASS" not in result
    assert "Intermediate" not in result


@pytest.mark.parametrize(
    "lines,exit_code,match",
    [
        (
            [
                _event("step_start", part={"id": "sp1", "messageID": "m1"}),
                _event("tool_use", part={"id": "t1", "messageID": "m1", "state": {"status": "completed"}}),
                _event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}),
            ],
            0,
            "final assistant text",
        ),
        (
            [
                _event("step_start", part={"id": "sp1", "messageID": "m1"}),
                _event("text", part={"id": "x1", "messageID": "m1", "text": "truncated"}),
                _event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "length"}),
            ],
            0,
            "finish reason",
        ),
        (
            [
                _event("step_start", part={"id": "sp1", "messageID": "m1"}),
                _event("text", part={"id": "x1", "messageID": "m1", "text": "hi"}, sessionID="ses_other"),
            ],
            0,
            "conflicting session",
        ),
        (
            [
                _event("text", part={"id": "x1", "messageID": "m1", "text": "hi"}),
                "not-json-at-all{{{",
            ],
            0,
            "malformed",
        ),
        (
            [
                _event("step_start", part={"id": "sp1", "messageID": "m1"}),
                _event("text", part={"id": "x1", "messageID": "m1", "text": "final"}),
                _event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}),
                _event("error", error={"name": "ProviderAuthError", "data": {"message": "boom"}}),
            ],
            0,
            "session error",
        ),
    ],
)
def test_incomplete_or_malformed_results_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, lines: list[str], exit_code: int, match: str) -> None:
    with pytest.raises(RuntimeError, match=match):
        _final_answer_via_driver(tmp_path, monkeypatch, "\n".join(lines) + "\n", exit_code=exit_code)


def test_recoverable_tool_failure_then_valid_answer_still_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    lines = [
        _event("tool_use", part={"id": "t1", "messageID": "m1", "state": {"status": "error", "error": "rate limit 429 while running tests"}}),
        _event("text", part={"id": "x1", "messageID": "m1", "text": "Recovered and completed."}),
        _event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}),
    ]
    result = _final_answer_via_driver(tmp_path, monkeypatch, "\n".join(lines) + "\n")
    assert result == "Recovered and completed."


# ---------------------------------------------------------------------------
# AC-004: diagnostics classification and routing
# ---------------------------------------------------------------------------


def test_usage_limit_diagnostic_raises_and_routes_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("error", error={"name": "RateLimitError", "data": {"message": "HTTP 429 rate limit exceeded"}}) + "\n")

    config = LLMBackendConfiguration(
        backends={
            "opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5"),
            "fallback": BackendConfig(name="fallback", backend_type="qwen"),
        }
    )
    fallback_client = type("FallbackClient", (), {"model_name": "fallback", "_run_llm_cli": lambda self, prompt, is_noedit=False: "fallback-success", "get_last_session_id": lambda self: None})()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))

    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config), patch("src.auto_coder.qwen_client.QwenClient", return_value=fallback_client):
        manager = build_backend_manager(["opencode", "fallback"], "opencode", {})
        assert manager._run_llm_cli("implement") == "fallback-success"
        assert manager.get_last_backend_and_model() == ("fallback", "fallback")


def test_authentication_failure_is_actionable_backend_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    lines = [_event("error", error={"name": "ProviderAuthError", "data": {"message": "missing credentials for provider anthropic"}})]
    with pytest.raises(RuntimeError, match="missing credentials"):
        _final_answer_via_driver(tmp_path, monkeypatch, "\n".join(lines) + "\n")


def test_transient_transport_failure_is_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    lines = [_event("error", error={"name": "APIConnectionError", "data": {"message": "fetch failed: socket hang up"}})]
    with pytest.raises(AutoCoderRetryableBackendError):
        _final_answer_via_driver(tmp_path, monkeypatch, "\n".join(lines) + "\n")


def test_marker_text_inside_successful_tool_payload_does_not_trigger_usage_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    lines = [
        _event("tool_use", part={"id": "t1", "messageID": "m1", "state": {"status": "completed", "output": 'saw "rate limit" and 429 in a log file, not a real limit'}}),
        _event("text", part={"id": "x1", "messageID": "m1", "text": "Handled the log line successfully."}),
        _event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}),
    ]
    result = _final_answer_via_driver(tmp_path, monkeypatch, "\n".join(lines) + "\n")
    assert result == "Handled the log line successfully."


def test_nonzero_exit_without_events_fails_with_return_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    with pytest.raises(RuntimeError, match="return code 17"):
        _final_answer_via_driver(tmp_path, monkeypatch, "", exit_code=17)


# ---------------------------------------------------------------------------
# AC-005: agent vs publisher ownership
# ---------------------------------------------------------------------------


def test_git_and_gh_lifecycle_mutations_denied_before_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    bare_remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(bare_remote))

    # Pre-existing caller content that must survive regardless of outcome.
    (repo / "untracked.txt").write_text("untracked-before\n")
    (repo / "tracked.txt").write_text("staged-change\n")
    _git(repo, "add", "tracked.txt")

    script = _driver(tmp_path)
    report = tmp_path / "denials.json"
    body = tmp_path / "body.py"
    body.write_text(textwrap.dedent("""
            import json
            import subprocess

            results = {}
            # Allowed: read-only inspection and a direct working-tree edit (not via git).
            results["status"] = subprocess.run(["git", "status", "--short"], capture_output=True, text=True).returncode
            open("agent_edit.txt", "w").write("edited by agent\\n")

            for key, cmd in {
                "add": ["git", "add", "agent_edit.txt"],
                "commit": ["git", "commit", "-m", "forbidden"],
                "shell_commit": ["sh", "-c", "git commit -m shellwrap"],
                "checkout_new": ["git", "checkout", "-b", "tmp-branch"],
                "push": ["git", "push", "origin", "HEAD:main"],
            }.items():
                proc = subprocess.run(cmd, capture_output=True, text=True)
                results[key] = {"returncode": proc.returncode, "stderr": proc.stderr}

            with open(%(report)r, "w") as fh:
                json.dump(results, fh)
            """ % {"report": str(report)}))

    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "done"}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_BODY_FILE", str(body))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))

    head_before = _git(repo, "rev-parse", "HEAD")
    assert _manager(config)._run_llm_cli("implement") == "done"

    denials = json.loads(report.read_text())
    for key in ("add", "commit", "shell_commit", "checkout_new", "push"):
        assert denials[key]["returncode"] != 0, key
        assert "reserved to Auto-Coder" in denials[key]["stderr"], key

    # The working-tree edit (not via git) is allowed and persisted.
    assert (repo / "agent_edit.txt").read_text() == "edited by agent\n"
    # Nothing reached the local "remote": it must still have zero refs.
    remote_refs = subprocess.run(["git", "--git-dir", str(bare_remote), "for-each-ref"], capture_output=True, text=True).stdout
    assert remote_refs.strip() == ""
    assert _git(repo, "rev-parse", "HEAD") == head_before
    # Pre-existing caller content survives.
    assert (repo / "untracked.txt").read_text() == "untracked-before\n"
    assert _git(repo, "diff", "--cached", "--name-only") == "tracked.txt"


def test_bypassed_git_commit_is_detected_restored_and_fails_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """Backstop: if the wrapper boundary is somehow bypassed, Auto-Coder still detects and restores.

    Exercises OpenCodeClient directly (not through BackendManager's worktree
    isolation) so the pre/post Git state can be asserted precisely against
    `repo` itself, complementing the full production-path denial test above.
    """
    repo = _repository(tmp_path)
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True, check=True).stdout.strip()

    (repo / "untracked.txt").write_text("untracked-before\n")

    script = _driver(tmp_path)
    body = tmp_path / "bypass_body.py"
    body.write_text(textwrap.dedent(f"""
            import subprocess

            with open("bypassed.txt", "w") as fh:
                fh.write("mutated\\n")
            subprocess.run([{real_git!r}, "add", "bypassed.txt"], check=True)
            subprocess.run([{real_git!r}, "commit", "-m", "bypassed commit"], check=True)
            """))

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_BODY_FILE", str(body))

    head_before = _git(repo, "rev-parse", "HEAD")
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        with pytest.raises(RuntimeError, match="Git lifecycle"):
            client._run_llm_cli("implement")

    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert (repo / "untracked.txt").read_text() == "untracked-before\n"


# ---------------------------------------------------------------------------
# AC-006: timeout really stops the writer
# ---------------------------------------------------------------------------


def test_timeout_kills_process_group_before_any_late_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    ready_file = tmp_path / "ready"
    late_file = tmp_path / "late"
    child_pid_file = tmp_path / "child.pid"
    body = tmp_path / "timeout_body.py"
    body.write_text(textwrap.dedent(f"""
            import os
            import subprocess
            import sys
            import time

            child = subprocess.Popen([sys.executable, "-c", (
                "import time;"
                "open({str(ready_file)!r}, 'w').close();"
                "time.sleep(5);"
                "open({str(late_file)!r}, 'w').close()"
            )])
            with open({str(child_pid_file)!r}, "w") as fh:
                fh.write(str(child.pid))
            while not os.path.exists({str(ready_file)!r}):
                time.sleep(0.01)
            time.sleep(5)
            """))

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5", timeout=1)})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_BODY_FILE", str(body))

    with pytest.raises(AutoCoderTimeoutError):
        _manager(config)._run_llm_cli("implement")

    assert not late_file.exists()
    child_pid = int(child_pid_file.read_text())
    # The child may still be a not-yet-reaped zombie (its own parent was also
    # killed), so `kill(pid, 0)` alone cannot prove it stopped; check /proc
    # state instead, tolerant of it having already been reaped entirely.
    try:
        state = Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        state = "gone"
    assert state in ("gone", "Z"), f"child process {child_pid} is still running (state={state!r})"


# ---------------------------------------------------------------------------
# AC-007: unsupported modes and stale global session
# ---------------------------------------------------------------------------


def test_ordinary_invocation_never_resumes_a_stale_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report = tmp_path / "report.json"
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "ok"}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))

    manager = _manager(config)
    # Simulate a stale session left over from a previous, unrelated run.
    manager._last_session_id = "stale-session-from-another-task"

    assert manager._run_llm_cli("implement") == "ok"

    observed = json.loads(report.read_text())
    for forbidden in ("--session", "--continue", "-c", "--fork"):
        assert forbidden not in observed["argv"]


def test_client_exposes_no_session_id_before_any_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _driver(tmp_path)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
    assert client.get_last_session_id() is None


def test_noedit_request_without_enforcement_fails_before_task_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """Issue #2125 REQ-004: an unavailable enforcement prerequisite fails before task launch.

    The fake driver's `debug agent` branch exits 1 (unsupported) unless
    OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE is set, modeling a CLI/environment
    where the enforcing no-edit policy cannot be established.
    """
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))

    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="no-edit enforcement could not be verified"):
        manager._run_llm_cli("review")

    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# Issue #2126: explicit session continuation with truthful continuity reporting
# ---------------------------------------------------------------------------


def _stdout_with_answer(text: str, session_id: str = "ses_root1", message_id: str = "m1") -> str:
    return _event("step_finish", session_id=session_id, part={"id": "sf1", "messageID": message_id, "reason": "stop"}) + "\n" + _event("text", session_id=session_id, part={"id": "t1", "messageID": message_id, "text": text}) + "\n"


def _session_list_response(*session_ids: str) -> str:
    return json.dumps([{"id": session_id, "directory": "/workspace"} for session_id in session_ids])


def _set_known_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *session_ids: str) -> None:
    """Make the fake driver's `session list --format json` report `session_ids` as
    associated with the current directory, satisfying the Issue #2126 REQ-006
    workspace-association preflight so a continuation test can reach `run`."""
    response = tmp_path / "session_list.json"
    response.write_text(_session_list_response(*session_ids))
    monkeypatch.setenv("OPENCODE_TEST_SESSION_LIST_RESPONSE_FILE", str(response))


def _continue_via_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jsonl: str, *, session_id: str = "ses_requested", exit_code: int = 0, is_noedit: bool = False, known_session_ids: Optional[Tuple[str, ...]] = None) -> str:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(jsonl)

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    monkeypatch.setenv("OPENCODE_TEST_EXIT_CODE", str(exit_code))
    _set_known_sessions(tmp_path, monkeypatch, *(known_session_ids if known_session_ids is not None else (session_id,)))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        return OpenCodeClient(backend_name="opencode").continue_session(session_id=session_id, prompt="continue", is_noedit=is_noedit)


# -- AC-001: fresh execution creates real resumable identity ---------------


def test_ac001_fresh_execution_exposes_root_session_and_continue_reaches_exactly_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report1 = tmp_path / "report1.json"
    report2 = tmp_path / "report2.json"
    stdout1 = tmp_path / "stdout1.jsonl"
    stdout2 = tmp_path / "stdout2.jsonl"
    # An opaque ID outside any assumed example-specific prefix.
    root_session = "opencode_zQ9-run:7f3c/session"
    stdout1.write_text(_stdout_with_answer("first answer", session_id=root_session))
    stdout2.write_text(_stdout_with_answer("second answer", session_id=root_session))

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")

        monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report1))
        monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout1))
        assert client._run_llm_cli("first task") == "first answer"
        assert client.get_last_session_id() == root_session

        _set_known_sessions(tmp_path, monkeypatch, root_session)
        monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report2))
        monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout2))
        result = client.continue_session(session_id=client.get_last_session_id(), prompt="second task")

    assert result == "second answer"
    observed = json.loads(report2.read_text())
    argv = observed["argv"]
    assert argv[argv.index("--session") + 1] == root_session
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    for forbidden in ("--continue", "-c", "--fork"):
        assert forbidden not in argv
    # Exactly one task submission for the continuation (one report write).
    assert json.loads(report1.read_text())["argv"][0] == "run"
    assert observed["argv"][0] == "run"


# -- AC-002: a missing session is not a fresh session -----------------------


def test_ac002_missing_session_reports_explicit_failure_without_exposing_new_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("error", session_id="ses_original", error={"name": "SessionNotFoundError", "data": {"message": "session ses_original not found"}}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    _set_known_sessions(tmp_path, monkeypatch, "ses_original")

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        with pytest.raises(RuntimeError, match="session error"):
            client.continue_session(session_id="ses_original", prompt="continue please")
        assert client.get_last_session_id() is None


def test_ac002_manager_local_fallback_after_missing_session_reports_non_continuity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """The adapter itself never resubmits; only `BackendManager.continue_session`'s
    documented fallback does, and it must report the fallback as non-continuity."""
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    missing_line = _event("error", session_id="ses_missing", error={"name": "SessionNotFoundError", "data": {"message": "not found"}})
    fresh_lines = _stdout_with_answer("fresh fallback answer", session_id="ses_fresh")
    body = tmp_path / "dynamic_body.py"
    body.write_text("import sys\n" f"if '--session' in argv:\n    sys.stdout.write({missing_line + chr(10)!r})\n" f"else:\n    sys.stdout.write({fresh_lines!r})\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_BODY_FILE", str(body))
    _set_known_sessions(tmp_path, monkeypatch, "ses_missing")

    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        manager = build_backend_manager(["opencode"], "opencode", {})
        manager._last_continue_session_resumed = True

        result = manager.continue_session(session_id="ses_missing", prompt="continue please")

    assert result == "fresh fallback answer"
    assert manager._last_continue_session_resumed is False


def test_ac002_manager_backend_switch_fallback_reports_non_continuity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """A configured backend-switch fallback returning a plausible answer under
    another backend/session is still reported as non-continuity."""
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("error", session_id="ses_x", error={"name": "RateLimitError", "data": {"message": "HTTP 429 rate limit exceeded"}}) + "\n")

    config = LLMBackendConfiguration(
        backends={
            "opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5"),
            "fallback": BackendConfig(name="fallback", backend_type="qwen"),
        }
    )
    fallback_client = type("FallbackClient", (), {"model_name": "fallback", "_run_llm_cli": lambda self, prompt, is_noedit=False: "fallback-success", "get_last_session_id": lambda self: "fallback-session-id"})()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    _set_known_sessions(tmp_path, monkeypatch, "ses_x")

    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config), patch("src.auto_coder.qwen_client.QwenClient", return_value=fallback_client):
        manager = build_backend_manager(["opencode", "fallback"], "opencode", {})
        manager._last_continue_session_resumed = True

        result = manager.continue_session(session_id="ses_x", prompt="continue please")

    assert result == "fallback-success"
    assert manager._last_continue_session_resumed is False
    assert manager.get_last_backend_and_model() == ("fallback", "fallback")


# -- AC-003: a same-looking answer with the wrong identity is not continuity


@pytest.mark.parametrize(
    "lines,match",
    [
        (
            [
                _event("step_finish", session_id="ses_other", part={"id": "sf1", "messageID": "m1", "reason": "stop"}),
                _event("text", session_id="ses_other", part={"id": "t1", "messageID": "m1", "text": "looks fine"}),
            ],
            "did not continue the requested session",
        ),
        (
            [
                # No sessionID reported at all.
                {"type": "step_finish", "part": {"id": "sf1", "messageID": "m1", "reason": "stop"}},
                {"type": "text", "part": {"id": "t1", "messageID": "m1", "text": "no session emitted"}},
            ],
            "did not continue the requested session",
        ),
        (
            [
                _event("tool_use", session_id="ses_requested", part={"id": "t1", "messageID": "m1", "state": {"status": "completed"}}),
                _event("step_finish", session_id="ses_requested", part={"id": "sf1", "messageID": "m1", "reason": "stop"}),
            ],
            "final assistant text",
        ),
        (
            [
                _event("step_finish", session_id="ses_requested", part={"id": "sf1", "messageID": "m1", "reason": "stop"}),
                _event("text", session_id="ses_requested", part={"id": "t1", "messageID": "m1", "text": "final"}),
                _event("error", session_id="ses_requested", error={"name": "ProviderAuthError", "data": {"message": "boom after final text"}}),
            ],
            "session error",
        ),
    ],
)
def test_ac003_syntactically_correct_result_with_wrong_identity_is_not_continuity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, lines: list, match: str) -> None:
    jsonl = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n"
    with pytest.raises(RuntimeError, match=match):
        _continue_via_driver(tmp_path, monkeypatch, jsonl, session_id="ses_requested", exit_code=0)


def test_ac003_manager_continuity_flag_resets_after_identity_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    good = tmp_path / "good.jsonl"
    good.write_text(_stdout_with_answer("first ok", session_id="ses_good"))
    mismatched = tmp_path / "mismatched.jsonl"
    mismatched.write_text(_stdout_with_answer("plausible but wrong", session_id="ses_other"))

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    _set_known_sessions(tmp_path, monkeypatch, "ses_good")

    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        manager = build_backend_manager(["opencode"], "opencode", {})

        monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(good))
        assert manager.continue_session(session_id="ses_good", prompt="first") == "first ok"
        assert manager._last_continue_session_resumed is True

        monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(mismatched))
        manager.continue_session(session_id="ses_good", prompt="second")

    assert manager._last_continue_session_resumed is False


# -- AC-006: one-shot state and exact prompt transport -----------------------


def test_ac006_continuation_transports_exact_prompt_once_then_ordinary_call_is_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report_continue = tmp_path / "report_continue.json"
    report_ordinary = tmp_path / "report_ordinary.json"
    stdout_continue = tmp_path / "stdout_continue.jsonl"
    stdout_ordinary = tmp_path / "stdout_ordinary.jsonl"
    stdout_continue.write_text(_stdout_with_answer("continued ok", session_id="ses_existing"))
    stdout_ordinary.write_text(_stdout_with_answer("fresh ok", session_id="ses_new"))

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    task = "Unicode ☃ large continuation payload あ " * 500
    expected = render_prompt("opencode.execution", task_prompt=task).encode("utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    _set_known_sessions(tmp_path, monkeypatch, "ses_existing")

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")

        monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report_continue))
        monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_continue))
        assert client.continue_session(session_id="ses_existing", prompt=task) == "continued ok"

        observed_continue = json.loads(report_continue.read_text())
        assert observed_continue["prompt_digest"] == hashlib.sha256(expected).hexdigest()
        assert observed_continue["prompt_in_argv"] is False
        assert observed_continue["prompt_in_env"] is False
        argv = observed_continue["argv"]
        assert argv[argv.index("--session") + 1] == "ses_existing"

        # Seed stale global session state to prove an ordinary call never resumes it.
        client._last_session_id = "ses_existing"

        monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report_ordinary))
        monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_ordinary))
        assert client._run_llm_cli("ordinary next task") == "fresh ok"

    observed_ordinary = json.loads(report_ordinary.read_text())
    assert "--session" not in observed_ordinary["argv"]


def test_ac006_model_override_rejected_during_continuation_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        client.set_extra_args(["--model", "openrouter/anthropic/claude-3.5-sonnet"])
        with pytest.raises(RuntimeError, match="model"):
            client.continue_session(session_id="ses_existing", prompt="continue")

    assert not sentinel.exists()


@pytest.mark.parametrize("bad_options", _BAD_OPTIONS_BEFORE_LAUNCH)
def test_ac006_incompatible_overrides_also_rejected_during_continuation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, bad_options: list[str]) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        client.set_extra_args(bad_options)
        with pytest.raises(RuntimeError):
            client.continue_session(session_id="ses_existing", prompt="continue")

    assert not sentinel.exists()


@pytest.mark.parametrize("bad_session_id", ["", "   ", None, "-abc", 123])
def test_continue_session_rejects_invalid_session_id_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, bad_session_id) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        with pytest.raises(ValueError):
            client.continue_session(session_id=bad_session_id, prompt="continue")

    assert not sentinel.exists()


def test_continued_session_id_still_reported_after_successful_continuation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    result = _continue_via_driver(tmp_path, monkeypatch, _stdout_with_answer("ok", session_id="ses_requested"), session_id="ses_requested")
    assert result == "ok"


# ---------------------------------------------------------------------------
# Issue #2126 REQ-006: a session not associated with the current execution
# directory is refused before task launch rather than risk a stale/redirected
# continuation (see docs/client-features/opencode-local-backend.md).
# ---------------------------------------------------------------------------


def test_continuation_rejected_before_launch_when_session_not_in_current_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))
    # `session list --format json` reports a *different* session than the one
    # being continued (the default fake-driver response is otherwise "[]").
    _set_known_sessions(tmp_path, monkeypatch, "ses_from_a_different_workspace")

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        with pytest.raises(RuntimeError, match="is not associated with the current execution directory"):
            client.continue_session(session_id="ses_requested", prompt="continue")

    # The `run` task itself is never submitted; only the read-only
    # `session list` preflight ran.
    assert not sentinel.exists()


def test_continuation_rejected_before_launch_when_no_sessions_known_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))
    # No OPENCODE_TEST_SESSION_LIST_RESPONSE_FILE set -> the fake driver
    # returns "[]", matching a real directory with no known sessions.

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        with pytest.raises(RuntimeError, match="is not associated with the current execution directory"):
            client.continue_session(session_id="ses_requested", prompt="continue")

    assert not sentinel.exists()


def test_fresh_execution_never_triggers_session_workspace_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """An ordinary (non-continuation) call has nothing to verify a session against."""
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_stdout_with_answer("ok", session_id="ses_fresh"))

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    # Deliberately leave OPENCODE_TEST_SESSION_LIST_RESPONSE_FILE unset ("[]"):
    # a fresh call must succeed regardless, since it never continues a session.

    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        assert client._run_llm_cli("implement") == "ok"
