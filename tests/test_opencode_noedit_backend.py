"""Production-path regression coverage for OpenCode no-edit enforcement (Issue #2125).

Fast, deterministic transport/precedence/cleanup coverage uses the same fake
`opencode` executable double as `tests/test_opencode_backend.py` (imported from
there to avoid duplicating it). Real permission enforcement, config-precedence,
and tool-exposure behavior can only be established by the actual released
OpenCode CLI against a controlled provider (a fake CLI that merely honors an
invented flag is not evidence of safety); those scenarios live in
`tests/test_opencode_noedit_live.py`, guarded by a fixture that installs/skips
based on real CLI availability.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.exceptions import AutoCoderTimeoutError
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.opencode_client import OpenCodeClient
from tests.test_opencode_backend import _driver, _event, _git, _manager, _repository


def _locked_down_debug_agent_response() -> str:
    return json.dumps(
        {
            "name": "__AGENT_NAME__",
            "tools": {
                "bash": False,
                "edit": False,
                "write": False,
                "task": False,
                "webfetch": False,
                "skill": False,
                "todowrite": False,
                "read": True,
                "glob": True,
                "grep": True,
            },
        }
    )


# ---------------------------------------------------------------------------
# REQ-004: enforcement must be verified before task launch
# ---------------------------------------------------------------------------


def test_preflight_success_reaches_real_run_with_generated_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report = tmp_path / "report.json"
    debug_response = tmp_path / "debug_agent.json"
    debug_response.write_text(_locked_down_debug_agent_response())
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "inspected"}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))

    manager = _manager(config)
    manager._is_noedit = True
    assert manager._run_llm_cli("what does this file do?") == "inspected"

    observed = json.loads(report.read_text())
    argv = observed["argv"]
    assert argv[0] == "run"
    assert "--agent" in argv
    agent_name = argv[argv.index("--agent") + 1]
    assert agent_name.startswith("autocoder-noedit-")
    # The same generated agent's config must be the one the real `run` loads.
    assert observed["opencode_config_content"] is not None
    config_content = json.loads(observed["opencode_config_content"])
    assert set(config_content["agent"].keys()) == {agent_name}
    assert config_content["agent"][agent_name]["permission"]["*"] == "deny"
    # The no-edit prompt template is used, not the edit-mode one.
    assert "read-only inspection" in observed["prompt_text"]


@pytest.mark.parametrize(
    "debug_agent_kwargs",
    [
        pytest.param({}, id="unsupported-command"),
        pytest.param({"exit_code": "1"}, id="nonzero-exit"),
    ],
)
def test_unavailable_enforcement_fails_before_task_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, debug_agent_kwargs: dict) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))
    if "exit_code" in debug_agent_kwargs:
        monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_EXIT_CODE", debug_agent_kwargs["exit_code"])

    manager = _manager(config)
    manager._is_noedit = True
    with pytest.raises(RuntimeError, match="no-edit enforcement could not be verified"):
        manager._run_llm_cli("review")
    assert not sentinel.exists()


def test_non_json_debug_agent_output_fails_before_task_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"
    debug_response = tmp_path / "debug_agent.txt"
    debug_response.write_text("Agent __AGENT_NAME__ not found, run 'opencode agent list' to get an agent list\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))

    manager = _manager(config)
    manager._is_noedit = True
    with pytest.raises(RuntimeError, match="no-edit enforcement could not be verified"):
        manager._run_llm_cli("review")
    assert not sentinel.exists()


@pytest.mark.parametrize(
    "broken_tool,broken_value",
    [
        ("bash", True),
        ("edit", True),
        ("task", True),
        ("webfetch", True),
        ("read", False),
        ("glob", False),
        ("grep", False),
    ],
)
def test_non_authoritative_policy_fails_before_task_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands, broken_tool: str, broken_value: bool) -> None:
    """REQ-004: any forbidden tool resolving open, or any required tool resolving closed, blocks launch."""
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"
    tools = json.loads(_locked_down_debug_agent_response())["tools"]
    tools[broken_tool] = broken_value
    debug_response = tmp_path / "debug_agent.json"
    debug_response.write_text(json.dumps({"name": "__AGENT_NAME__", "tools": tools}))

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))

    manager = _manager(config)
    manager._is_noedit = True
    with pytest.raises(RuntimeError, match="no-edit enforcement"):
        manager._run_llm_cli("review")
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# REQ-004/REQ-005: Auto-Coder retains authority over agent selection
# ---------------------------------------------------------------------------


def test_user_supplied_agent_override_rejected_before_task_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    sentinel = tmp_path / "launched.marker"

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5", options_for_noedit=["--agent", "some-other-agent"])})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_SENTINEL_FILE", str(sentinel))

    manager = _manager(config)
    manager._is_noedit = True
    with pytest.raises(RuntimeError, match="--agent"):
        manager._run_llm_cli("review")
    assert not sentinel.exists()


def test_configured_options_for_noedit_honored_when_compatible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    report = tmp_path / "report.json"
    debug_response = tmp_path / "debug_agent.json"
    debug_response.write_text(_locked_down_debug_agent_response())
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "ok"}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5", options_for_noedit=["--variant", "high"])})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))

    manager = _manager(config)
    manager._is_noedit = True
    assert manager._run_llm_cli("review") == "ok"
    observed = json.loads(report.read_text())
    assert "--variant" in observed["argv"] and observed["argv"][observed["argv"].index("--variant") + 1] == "high"


# ---------------------------------------------------------------------------
# REQ-003/REQ-006: a forbidden tool attempt rejects the result even on eventual "success"
# ---------------------------------------------------------------------------


def test_forbidden_tool_attempt_rejects_result_even_with_valid_looking_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    debug_response = tmp_path / "debug_agent.json"
    debug_response.write_text(_locked_down_debug_agent_response())
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(
        _event("tool_use", part={"id": "tu1", "messageID": "m1", "tool": "bash", "state": {"status": "error", "error": "Model tried to call unavailable tool"}})
        + "\n"
        + _event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "tool-calls"})
        + "\n"
        + _event("step_finish", part={"id": "sf2", "messageID": "m2", "reason": "stop"})
        + "\n"
        + _event("text", part={"id": "t1", "messageID": "m2", "text": "PASS"})
        + "\n"
    )

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))

    manager = _manager(config)
    manager._is_noedit = True
    with pytest.raises(RuntimeError, match="forbidden tool 'bash'"):
        manager._run_llm_cli("review")


def test_inspection_tool_use_does_not_reject_noedit_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    debug_response = tmp_path / "debug_agent.json"
    debug_response.write_text(_locked_down_debug_agent_response())
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(
        _event("tool_use", part={"id": "tu1", "messageID": "m1", "tool": "read", "state": {"status": "completed"}})
        + "\n"
        + _event("tool_use", part={"id": "tu2", "messageID": "m1", "tool": "grep", "state": {"status": "completed"}})
        + "\n"
        + _event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"})
        + "\n"
        + _event("text", part={"id": "t1", "messageID": "m1", "text": "found it"})
        + "\n"
    )

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))

    manager = _manager(config)
    manager._is_noedit = True
    assert manager._run_llm_cli("review") == "found it"


# ---------------------------------------------------------------------------
# REQ-002: the working tree must be provably unchanged, restored on violation
# ---------------------------------------------------------------------------


def test_bypassed_working_tree_mutation_detected_restored_and_fails_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    """Backstop: if the tool/permission boundary is somehow bypassed, Auto-Coder still detects and restores.

    Exercises OpenCodeClient directly (not through BackendManager's worktree
    isolation, which would discard the mutation anyway) so the pre/post state
    can be asserted precisely against `repo` itself.
    """
    repo = _repository(tmp_path)
    (repo / "untracked.txt").write_text("untracked-before\n")

    script = _driver(tmp_path)
    debug_response = tmp_path / "debug_agent.json"
    debug_response.write_text(_locked_down_debug_agent_response())
    body = tmp_path / "bypass_body.py"
    body.write_text(
        textwrap.dedent(
            """
            with open("tracked.txt", "w") as fh:
                fh.write("mutated by bypass\\n")
            """
        )
    )
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(_event("step_finish", part={"id": "sf1", "messageID": "m1", "reason": "stop"}) + "\n" + _event("text", part={"id": "t1", "messageID": "m1", "text": "done"}) + "\n")

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5")})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_BODY_FILE", str(body))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))

    head_before = _git(repo, "rev-parse", "HEAD")
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        with pytest.raises(RuntimeError, match="working tree"):
            client._run_llm_cli("inspect", is_noedit=True)

    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert (repo / "tracked.txt").read_text() == "before\n"
    assert (repo / "untracked.txt").read_text() == "untracked-before\n"


def test_noedit_timeout_preserves_workspace_before_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    repo = _repository(tmp_path)
    script = _driver(tmp_path)
    debug_response = tmp_path / "debug_agent.json"
    debug_response.write_text(_locked_down_debug_agent_response())
    ready_file = tmp_path / "ready"
    body = tmp_path / "timeout_body.py"
    body.write_text(
        textwrap.dedent(
            f"""
            import time
            open({str(ready_file)!r}, "w").close()
            time.sleep(5)
            """
        )
    )

    config = LLMBackendConfiguration(backends={"opencode": BackendConfig(name="opencode", backend_type="opencode", model="anthropic/claude-sonnet-4-5", timeout=1)})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_BODY_FILE", str(body))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))

    head_before = _git(repo, "rev-parse", "HEAD")
    with patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
        client = OpenCodeClient(backend_name="opencode")
        with pytest.raises(AutoCoderTimeoutError):
            client._run_llm_cli("inspect", is_noedit=True)

    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert (repo / "tracked.txt").read_text() == "before\n"
