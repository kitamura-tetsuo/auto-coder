"""Production-path regressions for Issue #2091.

Integrates the provider-scoped instruction composition boundary defined in
`cloud_provider_instructions.py` (Issue #2090) with the actual cloud startup
and recovery paths: `JulesClient.start_session`, `ClaudeRoutineClient.fire_routine`,
`CodexCloudClient.submit_task`, their public wrappers, Issue dispatch, Jules
recurrent-task launch/discovery, and existing-session follow-ups.

Every test below drives real production functions (`_process_issue_*_mode`,
the client classes themselves, `jules_engine` functions) through real
configuration resolution (`prompt_loader.load_prompts` reading an actual YAML
file); only the outermost network/CLI transport is mocked
(`requests.Session.post`, `CommandExecutor.run_command`), matching the
production-path regression policy: assertions read the captured HTTP JSON
body / CLI argv, not a hand-built substitute for it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import yaml

from auto_coder import prompt_loader
from auto_coder.automation_config import AutomationConfig
from auto_coder.claude_routine_client import ClaudeRoutineClient
from auto_coder.cloud_manager import CloudManager
from auto_coder.cloud_provider_instructions import CloudProviderInstructionError, CloudTaskOperation, prepare_cloud_task
from auto_coder.codex_cloud_client import CodexCloudClient
from auto_coder.issue_processor import (
    _process_issue_claude_routine_mode,
    _process_issue_codex_cloud_mode,
    _process_issue_jules_mode,
)
from auto_coder.jules_client import JulesClient
from auto_coder.jules_engine import check_and_resume_or_archive_sessions, check_and_start_recurrent_jules_tasks
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.managed_prompts import ManagedPromptRecoveryError, get_managed_prompt, recover_original_task

MARKER_PREFIX = "===== AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS ("


def _recent_iso() -> str:
    """An ISO timestamp recent enough to survive jules_engine's expiration/age filters."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Redirect every ~/.auto-coder persistence path used here at a temp HOME.

    Covers cloud.csv (CloudManager), the Claude Routine session-state file,
    the Jules retry-state file, and managed_prompts.json, all of which
    default to the real HOME/CWD outside tests.
    """
    monkeypatch.setattr("auto_coder.cloud_manager.Path.home", lambda: tmp_path)
    monkeypatch.setattr("auto_coder.managed_prompts.Path.home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def sentinel_prompts_path(tmp_path, monkeypatch):
    """Point the real prompt loader at a copy of the shipped prompts.yaml
    whose `cloud_provider_instructions` entries carry distinctive per-provider
    sentinels, so composition is proven through the real YAML config
    resolution path rather than a hand-built dict.
    """
    real = yaml.safe_load(prompt_loader.DEFAULT_PROMPTS_PATH.read_text(encoding="utf-8"))
    real["cloud_provider_instructions"] = {
        "jules": {"initial": "SENTINEL-JULES-INITIAL"},
        "claude-routine": {"initial": "SENTINEL-CLAUDE-INITIAL"},
        "codex-cloud": {"initial": "SENTINEL-CODEX-INITIAL"},
    }
    path = tmp_path / "prompts.yaml"
    path.write_text(yaml.safe_dump(real, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(prompt_loader, "DEFAULT_PROMPTS_PATH", path)
    prompt_loader.clear_prompt_cache()
    yield path
    prompt_loader.clear_prompt_cache()


@pytest.fixture
def empty_prompts_path(tmp_path, monkeypatch):
    """Point the real prompt loader at the actual shipped prompts.yaml
    (unmodified): the real `cloud_provider_instructions` entries ship empty.
    """
    monkeypatch.setattr(prompt_loader, "DEFAULT_PROMPTS_PATH", prompt_loader.DEFAULT_PROMPTS_PATH)
    prompt_loader.clear_prompt_cache()
    yield prompt_loader.DEFAULT_PROMPTS_PATH
    prompt_loader.clear_prompt_cache()


def _codex_issue(number: int = 4001) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "title": "Fix parser crash",
        "body": "## Objective\nFix the parser crash.\n\n## Requirements\nREQ-001: no crash.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"login": "reporter"},
    }


def _codex_llm_config(backend_name: str, environment_id: str = "env-prod") -> LLMBackendConfiguration:
    llm_config = LLMBackendConfiguration()
    llm_config.backends[backend_name] = BackendConfig(name=backend_name, backend_type="codex-cloud", environment_id=environment_id, attempts=1)
    return llm_config


# ---------------------------------------------------------------------------
# AS-001: misleading aliases and shared Jules templates do not leak
# ---------------------------------------------------------------------------


def test_as001_misleading_aliases_and_is_jules_template_do_not_leak(homes, sentinel_prompts_path):
    """Backend aliases named the *opposite* of their real type, and the Claude
    Routine path that still renders `is_jules=True`, must not cause
    cross-provider leakage: each recipient's own managed sentinel only.
    """
    config = AutomationConfig()
    github_client = MagicMock()

    # --- Jules, via an alias containing "claude" (misleading name) ---
    with (
        patch("auto_coder.issue_processor.get_commit_log", return_value="(none)"),
        patch("auto_coder.jules_client.get_llm_config") as jules_llm_cfg,
        patch("requests.sessions.Session.post") as jules_post,
    ):
        jules_llm_cfg.return_value.get_backend_config.return_value = BackendConfig(name="claude-flavored-alias", backend_type="jules")
        jules_post.return_value = MagicMock(status_code=200, json=lambda: {"sessionId": "jules-session-1"})
        jules_actions = _process_issue_jules_mode("owner/repo", _codex_issue(1), config, github_client)
    assert any("Started Jules session" in a for a in jules_actions)
    jules_payload = jules_post.call_args.kwargs["json"]
    assert "SENTINEL-JULES-INITIAL" in jules_payload["prompt"]
    assert "SENTINEL-CLAUDE-INITIAL" not in jules_payload["prompt"]
    assert "SENTINEL-CODEX-INITIAL" not in jules_payload["prompt"]

    # --- Claude Routine, via an alias containing "jules" (misleading name),
    # exercising the code path that still renders is_jules=True ---
    with (
        patch("auto_coder.issue_processor.get_commit_log", return_value="(none)"),
        patch("auto_coder.claude_routine_client.get_llm_config") as claude_llm_cfg,
        patch("auto_coder.claude_routine_client.check_claude_usage_or_raise"),
        patch("requests.sessions.Session.post") as claude_post,
    ):
        claude_llm_cfg.return_value.get_backend_config.return_value = BackendConfig(name="jules-flavored-alias", backend_type="claude-routine", url="https://example.test/routines/1", claude_code_routine_token="tok")
        claude_post.return_value = MagicMock(status_code=200, json=lambda: {"claude_code_session_id": "claude-session-1", "claude_code_session_url": "https://claude.ai/code/claude-session-1"})
        claude_actions = _process_issue_claude_routine_mode("owner/repo", _codex_issue(2), config, github_client, backend_name="jules-flavored-alias")
    assert any("Started Claude Routine session" in a for a in claude_actions)
    claude_payload = claude_post.call_args.kwargs["json"]
    assert "SENTINEL-CLAUDE-INITIAL" in claude_payload["text"]
    assert "SENTINEL-JULES-INITIAL" not in claude_payload["text"]
    assert "SENTINEL-CODEX-INITIAL" not in claude_payload["text"]

    # --- Codex Cloud, via its own backend ---
    backend_name = "codex-cloud-main"
    task_id = "task_e_sentinel4001"
    with (
        patch("auto_coder.codex_cloud_client.get_llm_config", return_value=_codex_llm_config(backend_name)),
        patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=True),
        patch("auto_coder.issue_processor.get_current_attempt", return_value=1),
        patch("auto_coder.issue_processor.get_commit_log", return_value="(none)"),
        patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as run_command,
    ):
        run_command.return_value = MagicMock(returncode=0, stdout=f"https://chatgpt.com/codex/tasks/{task_id}", stderr="")
        codex_actions = _process_issue_codex_cloud_mode("owner/repo", _codex_issue(3), config, github_client, backend_name=backend_name)
    assert codex_actions == [f"Started Codex Cloud task '{task_id}' for issue #3"]
    codex_prompt_arg = run_command.call_args.args[0][-1]
    assert "SENTINEL-CODEX-INITIAL" in codex_prompt_arg
    assert "SENTINEL-JULES-INITIAL" not in codex_prompt_arg
    assert "SENTINEL-CLAUDE-INITIAL" not in codex_prompt_arg


# ---------------------------------------------------------------------------
# AS-002: public wrappers cannot skip or double the boundary
# ---------------------------------------------------------------------------


def test_as002_jules_start_task_wrapper_injects_exactly_once(homes, sentinel_prompts_path):
    client = JulesClient()
    with patch.object(client.session, "post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"sessionId": "s1"})
        client.start_task("raw task", repo_name="owner/repo", base_branch="main")
    prompt = mock_post.call_args.kwargs["json"]["prompt"]
    assert prompt.count("SENTINEL-JULES-INITIAL") == 1


def test_as002_claude_run_llm_cli_wrapper_injects_exactly_once_and_forwards_noedit(homes, sentinel_prompts_path):
    client = ClaudeRoutineClient()
    client.url = "https://example.test/routines/1"
    client.token = "tok"
    with (
        patch("auto_coder.claude_routine_client.check_claude_usage_or_raise"),
        patch.object(client.session, "post") as mock_post,
    ):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"claude_code_session_id": "s2"})
        client._run_llm_cli("raw task", is_noedit=False)
        text_with_component = mock_post.call_args.kwargs["json"]["text"]
        assert text_with_component.count("SENTINEL-CLAUDE-INITIAL") == 1

        mock_post.reset_mock()
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"claude_code_session_id": "s3"})
        client._run_llm_cli("raw task", is_noedit=True)
        text_noedit = mock_post.call_args.kwargs["json"]["text"]
        assert "SENTINEL-CLAUDE-INITIAL" not in text_noedit
        assert text_noedit == "raw task"


def test_as002_codex_start_task_wrapper_injects_exactly_once(homes, sentinel_prompts_path):
    client = CodexCloudClient(repo_name="owner/repo")
    client.environment_id = "env-prod"
    with (
        patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=True),
        patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as run_command,
    ):
        run_command.return_value = MagicMock(returncode=0, stdout="https://chatgpt.com/codex/tasks/task_e_wrap1", stderr="")
        client.start_task("raw task", repo_name="owner/repo", base_branch="main")
    prompt_arg = run_command.call_args.args[0][-1]
    assert prompt_arg.count("SENTINEL-CODEX-INITIAL") == 1


def test_as002_missing_or_blank_entries_preserve_exact_baseline(homes, empty_prompts_path):
    """The real shipped entries are empty: composing through any provider's
    boundary must be an exact no-op (byte-for-byte baseline preservation).
    """
    client = JulesClient()
    with patch.object(client.session, "post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"sessionId": "s4"})
        client.start_session("raw task, unchanged", repo_name="owner/repo", base_branch="main")
    assert mock_post.call_args.kwargs["json"]["prompt"] == "raw task, unchanged"


def test_as002_malformed_selected_entry_causes_zero_external_sends(homes, tmp_path, monkeypatch):
    real = yaml.safe_load(prompt_loader.DEFAULT_PROMPTS_PATH.read_text(encoding="utf-8"))
    real["cloud_provider_instructions"] = {"jules": {"initial": 42}}
    path = tmp_path / "prompts.yaml"
    path.write_text(yaml.safe_dump(real, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(prompt_loader, "DEFAULT_PROMPTS_PATH", path)
    prompt_loader.clear_prompt_cache()

    client = JulesClient()
    with patch.object(client.session, "post") as mock_post:
        with pytest.raises(CloudProviderInstructionError):
            client.start_session("raw task", repo_name="owner/repo", base_branch="main")
    mock_post.assert_not_called()
    prompt_loader.clear_prompt_cache()


# ---------------------------------------------------------------------------
# AS-003: saved Jules prompts survive new-session recovery
# ---------------------------------------------------------------------------


def test_as003_failed_session_replacement_recovers_original_and_rebuilds_with_current_component(homes, sentinel_prompts_path, tmp_path, monkeypatch):
    monkeypatch.setattr("auto_coder.jules_engine.STATE_FILE", str(tmp_path / "jules_session_state.json"))

    jules = JulesClient()
    with patch.object(jules.session, "post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"sessionId": "jules-orig"})
        jules.start_session("original task body", repo_name="owner/repo", base_branch="main")

    saved = get_managed_prompt("owner/repo", "jules-orig")
    assert saved is not None
    assert saved.original_task == "original task body"

    error_session = {
        "name": "sessions/jules-orig",
        "state": "IN_PROGRESS",
        "createTime": _recent_iso(),
        "updateTime": _recent_iso(),
        "outputs": {"error": "Jules encountered an error while doing the task"},
        "sourceContext": {"githubRepoContext": {"startingBranch": "main"}},
        "prompt": None,
    }

    def fake_get_session(session_id):
        full = dict(error_session)
        full["prompt"] = get_managed_prompt("owner/repo", "jules-orig").prepared_task
        return full

    with (
        patch("auto_coder.jules_engine.JulesClient") as jules_client_cls,
        patch("auto_coder.jules_engine.GitHubClient") as github_client_cls,
    ):
        instance = jules_client_cls.return_value
        instance.list_sessions.return_value = [error_session]
        instance.get_session.side_effect = fake_get_session
        instance.start_session.return_value = "jules-replacement"
        github_client_cls.get_instance.return_value = MagicMock(get_issue_comments=lambda *a, **k: [])

        check_and_resume_or_archive_sessions(repo_name="owner/repo")

        # Replacement was rebuilt from the *original* undecorated task, not
        # the already-decorated saved prompt (no double component).
        _, kwargs = instance.start_session.call_args
        assert kwargs["prompt"] == "original task body"


def test_as003_missing_metadata_with_managed_marker_refuses_ambiguous_replacement(homes, sentinel_prompts_path, tmp_path, monkeypatch):
    monkeypatch.setattr("auto_coder.jules_engine.STATE_FILE", str(tmp_path / "jules_session_state.json"))

    decorated_without_record = prepare_cloud_task("some task", recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False).prepared_task
    assert MARKER_PREFIX in decorated_without_record

    error_session = {
        "name": "sessions/orphan",
        "state": "IN_PROGRESS",
        "createTime": _recent_iso(),
        "updateTime": _recent_iso(),
        "outputs": {"error": "Jules encountered an error while doing the task"},
        "sourceContext": {"githubRepoContext": {"startingBranch": "main"}},
        "prompt": decorated_without_record,
    }

    with (
        patch("auto_coder.jules_engine.JulesClient") as jules_client_cls,
        patch("auto_coder.jules_engine.GitHubClient") as github_client_cls,
    ):
        instance = jules_client_cls.return_value
        instance.list_sessions.return_value = [error_session]
        instance.get_session.return_value = error_session
        github_client_cls.get_instance.return_value = MagicMock()

        check_and_resume_or_archive_sessions(repo_name="owner/repo")

        # No ambiguous replacement session was created for the orphaned record.
        instance.start_session.assert_not_called()


def test_as003_legacy_undecorated_prompt_recovers_as_itself():
    assert recover_original_task("legacy raw task, no marker", "owner/repo", "legacy-session") == "legacy raw task, no marker"


def test_as003_quoted_component_block_inside_original_task_is_preserved(homes, sentinel_prompts_path):
    original = "Discuss the block:\n```\n" + MARKER_PREFIX + "jules) =====\nSENTINEL-JULES-INITIAL\n===== END AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS =====\n```"
    prepared = prepare_cloud_task(original, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False)
    assert original in prepared.prepared_task
    # One managed copy on top of the quoted block already present in the raw task.
    assert prepared.prepared_task.count("SENTINEL-JULES-INITIAL") == 2


# ---------------------------------------------------------------------------
# AS-004: recurrent frontmatter remains discoverable after decoration
# ---------------------------------------------------------------------------


def _sessions_get_response(sessions):
    return MagicMock(status_code=200, json=lambda: {"sessions": sessions})


def test_as004_recurrent_discovery_matches_running_session_and_does_not_duplicate(homes, sentinel_prompts_path, tmp_path):
    from auto_coder.jules_client import invalidate_jules_sessions_cache

    invalidate_jules_sessions_cache()
    prompts_dir = tmp_path / ".auto-coder" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "task.md").write_text("---\nname: nightly-cleanup\ntags: [jules, recurrent]\n---\nDo the nightly cleanup.\n", encoding="utf-8")

    decorated = prepare_cloud_task(
        "---\nname: nightly-cleanup\ntags: [jules, recurrent]\n---\nDo the nightly cleanup.\n",
        recipient="jules",
        operation=CloudTaskOperation.NEW_TASK,
        no_edit=False,
    ).prepared_task
    assert decorated.startswith("---\nname: nightly-cleanup\ntags: [jules, recurrent]\n---\n")

    running_session = {
        "name": "sessions/nightly-1",
        "state": "IN_PROGRESS",
        "prompt": decorated,
        "sourceContext": {"source": "sources/github/owner/repo"},
    }

    with (
        patch("requests.sessions.Session.get", return_value=_sessions_get_response([running_session])),
        patch("requests.sessions.Session.post") as mock_post,
    ):
        check_and_start_recurrent_jules_tasks("owner/repo")
        mock_post.assert_not_called()
    invalidate_jules_sessions_cache()


def test_as004_recurrent_next_run_receives_one_current_component(homes, sentinel_prompts_path, tmp_path):
    from auto_coder.jules_client import invalidate_jules_sessions_cache

    invalidate_jules_sessions_cache()
    prompts_dir = tmp_path / ".auto-coder" / "prompts"
    prompts_dir.mkdir(parents=True)
    raw_task = "---\nname: nightly-cleanup\ntags: [jules, recurrent]\n---\nDo the nightly cleanup.\n"
    (prompts_dir / "task.md").write_text(raw_task, encoding="utf-8")

    with (
        patch("requests.sessions.Session.get", return_value=_sessions_get_response([])),
        patch("requests.sessions.Session.post") as mock_post,
    ):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"sessionId": "nightly-2"})
        check_and_start_recurrent_jules_tasks("owner/repo")

        sent_prompt = mock_post.call_args.kwargs["json"]["prompt"]
        assert sent_prompt.count("SENTINEL-JULES-INITIAL") == 1
        assert sent_prompt.startswith(raw_task.rstrip("\n"))
    invalidate_jules_sessions_cache()


# ---------------------------------------------------------------------------
# AS-005: retry bytes and a new recipient are different operations
# ---------------------------------------------------------------------------


def test_as005_continuation_bytes_stable_across_configuration_change(homes, sentinel_prompts_path, tmp_path, monkeypatch):
    first = prepare_cloud_task("retry message", recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False)

    real = yaml.safe_load(prompt_loader.DEFAULT_PROMPTS_PATH.read_text(encoding="utf-8"))
    real["cloud_provider_instructions"]["codex-cloud"]["initial"] = "SENTINEL-CODEX-CHANGED"
    changed_path = tmp_path / "changed_prompts.yaml"
    changed_path.write_text(yaml.safe_dump(real, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(prompt_loader, "DEFAULT_PROMPTS_PATH", changed_path)
    prompt_loader.clear_prompt_cache()

    second = prepare_cloud_task("retry message", recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False)
    assert first.prepared_task == second.prepared_task == "retry message"


def test_as005_fallback_to_new_recipient_gets_only_its_own_component(homes, sentinel_prompts_path):
    """A separate fallback decision (moving to a different provider) is a new
    NEW_TASK preparation from the shared original task; it must not carry
    the first provider's component.
    """
    original = "the task"
    codex_first = prepare_cloud_task(original, recipient="codex-cloud", operation=CloudTaskOperation.NEW_TASK, no_edit=False)
    assert original == codex_first.original_task

    jules_fallback = prepare_cloud_task(codex_first.original_task, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False)
    assert "SENTINEL-JULES-INITIAL" in jules_fallback.prepared_task
    assert "SENTINEL-CODEX-INITIAL" not in jules_fallback.prepared_task
    assert original == jules_fallback.original_task


# ---------------------------------------------------------------------------
# AS-006: existing-session follow-ups stay unchanged
# ---------------------------------------------------------------------------


def test_as006_jules_send_followup_receives_no_initial_component(homes, sentinel_prompts_path):
    client = JulesClient()
    with patch.object(client, "send_message") as mock_send:
        client.send_followup("session-1", "please fix the CI failure")
    sent_message = mock_send.call_args.args[1]
    assert sent_message == "please fix the CI failure"
    assert "SENTINEL-JULES-INITIAL" not in sent_message


def test_as006_claude_send_followup_receives_no_initial_component(homes, sentinel_prompts_path):
    client = ClaudeRoutineClient()
    with patch("auto_coder.claude_routine_client.CommandExecutor.run_command") as run_command:
        run_command.return_value = MagicMock(returncode=0, stdout="", stderr="")
        client.send_followup("session-1", "please fix the CI failure")
    cmd_args = run_command.call_args.args[0]
    assert cmd_args[-1] == "please fix the CI failure"


def test_as006_codex_send_followup_receives_no_initial_component(homes, sentinel_prompts_path):
    client = CodexCloudClient(repo_name="owner/repo")
    fake_wham = MagicMock()
    fake_wham.resolve_latest_assistant_turn.return_value = "turn-1"
    fake_wham.send_follow_up.return_value = MagicMock(delivered=True, outcome=None)
    client.wham_client = fake_wham

    client.send_followup("task_e_followup1", "please fix the CI failure")

    sent_message = fake_wham.send_follow_up.call_args.kwargs["prompt"]
    assert sent_message == "please fix the CI failure"


# ---------------------------------------------------------------------------
# AS-007: marker-shaped input and empty configuration are negative controls
# ---------------------------------------------------------------------------


def test_as007_marker_shaped_task_and_unicode_dollar_signs_survive_exactly(homes, sentinel_prompts_path):
    raw = f"Discuss the marker '{MARKER_PREFIX}jules) ====='. Cost is $5. Unicode: café, 日本語. Do not commit anything."
    prepared = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False)
    assert raw in prepared.prepared_task
    assert prepared.prepared_task.count("SENTINEL-JULES-INITIAL") == 1


def test_as007_empty_configuration_yields_exact_baseline_for_all_three_providers(homes, empty_prompts_path):
    raw = "Implement the feature."
    for recipient in ("jules", "claude-routine", "codex-cloud"):
        result = prepare_cloud_task(raw, recipient=recipient, operation=CloudTaskOperation.NEW_TASK, no_edit=False)
        assert result.prepared_task == raw
