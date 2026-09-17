"""Production-path regression coverage for the OpenCode local backend (Issue #2124)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path
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
_DRIVER_SOURCE = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import hashlib
    import json
    import os
    import sys

    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print("opencode 1.18.31")
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
    """
).strip()


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


@pytest.mark.parametrize(
    "bad_options",
    [
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
    ],
)
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
    body.write_text(
        textwrap.dedent(
            """
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
            """
            % {"report": str(report)}
        )
    )

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
    body.write_text(
        textwrap.dedent(
            f"""
            import subprocess

            with open("bypassed.txt", "w") as fh:
                fh.write("mutated\\n")
            subprocess.run([{real_git!r}, "add", "bypassed.txt"], check=True)
            subprocess.run([{real_git!r}, "commit", "-m", "bypassed commit"], check=True)
            """
        )
    )

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
    body.write_text(
        textwrap.dedent(
            f"""
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
            """
        )
    )

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


def test_client_never_exposes_a_resumable_session_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _driver(tmp_path)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
    assert client.get_last_session_id() is None
    with pytest.raises(NotImplementedError):
        client.continue_session(session_id="anything", prompt="implement")


def test_noedit_request_fails_before_task_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))

    manager = _manager(config)
    manager._is_noedit = True

    with pytest.raises(RuntimeError, match="no-edit"):
        manager._run_llm_cli("review")

    assert not sentinel.exists()
