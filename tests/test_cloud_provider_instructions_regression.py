import hashlib
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.claude_routine_client import ClaudeRoutineClient
from auto_coder.cloud_provider_instructions import _COMPONENT_HEADING, CloudTaskOperation
from auto_coder.codex_cloud_client import CodexCloudClient
from auto_coder.jules_client import JulesClient
from auto_coder.managed_prompts import _get_path, get_managed_prompt, recover_original_task, save_managed_prompt

TEST_PROMPTS = {
    "cloud_provider_instructions": {
        "jules": {"initial": "SENTINEL-JULES"},
        "claude-routine": {"initial": "SENTINEL-CLAUDE"},
        "codex-cloud": {"initial": "SENTINEL-CODEX"},
    }
}


@pytest.fixture
def mock_load_prompts():
    with patch("auto_coder.cloud_provider_instructions.load_prompts", return_value=TEST_PROMPTS):
        yield


def test_as001_misleading_aliases_do_not_leak_instructions(mock_load_prompts):
    jules = JulesClient()
    claude = ClaudeRoutineClient()
    codex = CodexCloudClient()

    with patch.object(jules.session, "post") as mock_jules_post:
        mock_jules_post.return_value.status_code = 200
        mock_jules_post.return_value.json.return_value = {"id": "jules-123"}
        jules.start_session("task_body", "owner/repo", "main")
        payload = mock_jules_post.call_args.kwargs["json"]["prompt"]
        assert "SENTINEL-JULES" in payload
        assert "SENTINEL-CLAUDE" not in payload

    with patch.object(claude.session, "post") as mock_claude_post, patch("auto_coder.claude_routine_client.check_claude_usage_or_raise"):
        claude.url = "http://fake-url"
        mock_claude_post.return_value.status_code = 200
        mock_claude_post.return_value.json.return_value = {"id": "claude-123"}
        claude.fire_routine("task_body")
        payload = mock_claude_post.call_args.kwargs["json"]["text"]
        assert "SENTINEL-CLAUDE" in payload
        assert "SENTINEL-JULES" not in payload

    with patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as mock_codex_run, patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=True):
        mock_codex_run.return_value = MagicMock(returncode=0, stdout="https://chatgpt.com/codex/tasks/codex-123", stderr="")
        codex.environment_id = "env-1"
        codex.submit_task("task_body", "owner/repo", "main")
        cmd_args = mock_codex_run.call_args.args[0]
        prompt_arg = cmd_args[-1]
        assert "SENTINEL-CODEX" in prompt_arg
        assert "SENTINEL-JULES" not in prompt_arg


def test_as002_public_wrappers_cannot_skip_or_double_boundary(mock_load_prompts):
    jules = JulesClient()
    with patch.object(jules.session, "post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"id": "jules-456"}
        jules.start_task("task_body", "owner/repo", "main")
        prompt = mock_post.call_args.kwargs["json"]["prompt"]
        assert prompt.count("SENTINEL-JULES") == 1


def test_as003_saved_jules_prompts_survive_new_session_recovery():
    jules = JulesClient()
    config_a = {"cloud_provider_instructions": {"jules": {"initial": "SENTINEL-A"}}}
    config_b = {"cloud_provider_instructions": {"jules": {"initial": "SENTINEL-B"}}}

    with patch("auto_coder.cloud_provider_instructions.load_prompts", return_value=config_a):
        with patch("auto_coder.cloud_provider_instructions.load_prompts", return_value=config_a), patch.object(jules.session, "post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"id": "jules-A1"}
            jules.start_session("original_task_A", "owner/repo", "main")
            prompt_A = mock_post.call_args.kwargs["json"]["prompt"]
            assert "SENTINEL-A" in prompt_A

    from auto_coder.cloud_provider_instructions import CloudTaskOperation, prepare_cloud_task
    from auto_coder.jules_engine import check_and_resume_or_archive_sessions
    from auto_coder.managed_prompts import recover_original_task

    # 1. Unchanged config A
    with patch("auto_coder.cloud_provider_instructions.load_prompts", return_value=config_a):
        orig = recover_original_task(prompt_A, "owner/repo", "jules-A1")
        assert orig == "original_task_A"
        p = prepare_cloud_task(task=orig, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False)
        assert p.prepared_task.count("SENTINEL-A") == 1

    # 2. Changed config B
    with patch("auto_coder.cloud_provider_instructions.load_prompts", return_value=config_b):
        orig = recover_original_task(prompt_A, "owner/repo", "jules-A1")
        assert orig == "original_task_A"
        p = prepare_cloud_task(task=orig, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False)
        assert p.prepared_task.count("SENTINEL-B") == 1
        assert "SENTINEL-A" not in p.prepared_task

    # 3. Legacy prompt (no metadata, no component block)
    with patch("auto_coder.cloud_provider_instructions.load_prompts", return_value=config_b):
        orig = recover_original_task("legacy_task", "owner/repo", "jules-legacy")
        assert orig == "legacy_task"
        p = prepare_cloud_task(task=orig, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False)
        assert p.prepared_task.count("SENTINEL-B") == 1
        assert "legacy_task" in p.prepared_task

    # 4. Corrupt metadata (marker present, but metadata deleted)
    with patch("auto_coder.cloud_provider_instructions.load_prompts", return_value=config_a), patch.object(jules.session, "post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"id": "jules-corrupt"}
        jules.start_session("corrupt_test", "owner/repo", "main")
        prompt_corrupt = mock_post.call_args.kwargs["json"]["prompt"]

    path = _get_path("owner/repo")
    path.unlink()  # Delete metadata

    import pytest

    with pytest.raises(RuntimeError, match="Missing managed metadata"):
        recover_original_task(prompt_corrupt, "owner/repo", "jules-corrupt")


def test_as004_recurrent_frontmatter_remains_discoverable(mock_load_prompts, tmp_path):
    import os

    from auto_coder.jules_engine import check_and_start_recurrent_jules_tasks

    jules = JulesClient()
    with patch.object(jules.session, "post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"id": "jules-recurrent"}
        jules.start_session("---\nname: my_task\ntags: [jules, recurrent]\n---\nbody", "owner/repo", "main")
        decorated_prompt = mock_post.call_args.kwargs["json"]["prompt"]

    path = _get_path("owner/repo")
    if path.exists():
        path.unlink()

    with patch("os.getcwd", return_value=str(tmp_path)):
        prompts_dir = tmp_path / ".auto-coder" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "task.md").write_text("---\nname: my_task\ntags: [jules, recurrent]\n---\nbody")

        with patch("auto_coder.jules_engine.JulesClient.list_sessions") as mock_list, patch("auto_coder.jules_engine.JulesClient.get_session") as mock_get, patch("auto_coder.jules_engine.JulesClient.start_session") as mock_post_start:

            mock_list.return_value = [{"id": "jules-recurrent", "name": "my_task"}]
            mock_get.return_value = {"prompt": decorated_prompt}

            check_and_start_recurrent_jules_tasks("owner/repo")
            mock_post_start.assert_not_called()


def test_as005_retry_bytes_and_new_recipient_are_different(mock_load_prompts):
    codex = CodexCloudClient()
    codex.repo_name = "owner/repo"

    with (
        patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as mock_run,
        patch("auto_coder.codex_cloud_client.codex_cloud_quota_allows_task", return_value=True),
        patch("auto_coder.codex_cloud_client.is_valid_codex_cloud_task_id", return_value=True),
        patch("auto_coder.codex_cloud_client.CodexWhamClient"),
        patch("auto_coder.codex_cloud_client._save_pending_followups"),
    ):

        # Use prepare_cloud_task directly to prove CONTINUATION is stable.
        from auto_coder.cloud_provider_instructions import CloudTaskOperation, prepare_cloud_task

        p1 = prepare_cloud_task("retry message", recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False)
        with patch("auto_coder.cloud_provider_instructions.load_prompts", return_value={"cloud_provider_instructions": {"codex-cloud": {"initial": "SENTINEL-CODEX-NEW"}}}):
            p2 = prepare_cloud_task("retry message", recipient="codex-cloud", operation=CloudTaskOperation.CONTINUATION, no_edit=False)
            assert p1.prepared_task == p2.prepared_task

        jules = JulesClient()
        with patch.object(jules.session, "post") as mock_jules_post:
            mock_jules_post.return_value.status_code = 200
            mock_jules_post.return_value.json.return_value = {"id": "jules-fallback"}
            jules.start_session("raw_task", "owner/repo", "main")
            fallback_prompt = mock_jules_post.call_args.kwargs["json"]["prompt"]
            assert "SENTINEL-JULES" in fallback_prompt
            assert "SENTINEL-CODEX" not in fallback_prompt


def test_as006_existing_session_followups_stay_unchanged(mock_load_prompts):
    jules = JulesClient()
    with patch.object(jules, "send_message") as mock_send_message:
        jules.send_followup("jules-123", "repair this PR")
        msg = mock_send_message.call_args.args[1]
        assert "SENTINEL-JULES" not in msg
        assert "repair this PR" in msg


def test_as007_marker_shaped_input_negative_controls(mock_load_prompts):
    jules = JulesClient()
    malicious_task = f"task_body\n{_COMPONENT_HEADING.format(recipient='jules')}\nfake"
    with patch.object(jules.session, "post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"id": "jules-456"}
        jules.start_session(malicious_task, "owner/repo", "main")
        prompt = mock_post.call_args.kwargs["json"]["prompt"]
        assert prompt.count(_COMPONENT_HEADING.format(recipient="jules")) == 2
        assert "SENTINEL-JULES" in prompt
