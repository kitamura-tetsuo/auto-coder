"""Regression coverage for verified-origin admission at PR repair boundaries."""

from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_pr_attribution import CodexPrAttributionRepository
from auto_coder.pr_processor import _resolve_cloud_task_origin, _send_codex_cloud_error_feedback


def _accepted_run(task_id: str = "task_e_Verified") -> CloudRun:
    return CloudRun(
        "owner/repo",
        2232,
        4,
        "codex-cloud",
        task_id=task_id,
        backend_name="codex-alias",
        submission_outcome="accepted",
        launch_identity="request-2232-4",
        publication_head_repository="owner/repo",
        publication_head_ref="issue-2232-attempt-4-codex-cloud",
    )


def _pr() -> dict[str, object]:
    return {
        "number": 88,
        "body": "Closes #2232",
        "head": {
            "ref": "issue-2232-attempt-4-codex-cloud",
            "sha": "head-88",
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main", "sha": "base-88"},
    }


def test_verified_pr_publication_overrides_stale_issue_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    CloudRunRepository("owner/repo").save(_accepted_run())
    assert CloudManager("owner/repo").ensure_binding(2232, CloudTaskBinding("jules", "old-jules", "jules"))

    with patch("auto_coder.codex_cloud_client.CodexCloudClient") as client_type:
        resolution = _resolve_cloud_task_origin("owner/repo", _pr())

    assert resolution.reason == ""
    assert resolution.origin is not None
    assert (resolution.origin.provider, resolution.origin.task_id, resolution.origin.attribution_token) == (
        "codex-cloud",
        "task_e_Verified",
        "1",
    )
    client_type.assert_called_once_with(backend_name="codex-alias", repo_name="owner/repo")
    assert CloudManager("owner/repo").get_binding(2232) == CloudTaskBinding("jules", "old-jules", "jules")


def test_unverified_codex_indicator_blocks_ci_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    data = _pr()
    data["body"] = "Closes #2232\nhttps://chatgpt.com/codex/tasks/task_e_Unproven"
    data["_codex_pr_attribution_required"] = True

    with patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused") as transport:
        result = _send_codex_cloud_error_feedback("owner/repo", data, [{"name": "tests"}], AutomationConfig(), MagicMock())

    transport.assert_not_called()
    assert result.delivered is False
    assert result.retryable is True
    assert "UNRESOLVED" in result.actions[0]


def test_final_ci_admission_rejects_changed_consistency_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    CloudRunRepository("owner/repo").save(_accepted_run())
    data = _pr()

    class Authority:
        allowed = True
        reason = ""
        failure_identities = ("tests",)

        def __enter__(self):
            # Simulate a participating durable attribution update after route
            # selection but before the transport boundary.
            repository = CodexPrAttributionRepository("owner/repo")
            raw = repository._read()
            raw["revision"] = 2
            repository.path.write_text(__import__("json").dumps(raw), encoding="utf-8")
            return self

        def __exit__(self, *_args):
            return False

    with (
        patch("auto_coder.pr_processor.current_ci_failure_authority", return_value=Authority()),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused") as transport,
    ):
        result = _send_codex_cloud_error_feedback("owner/repo", data, [{"name": "tests"}], AutomationConfig(), MagicMock())

    transport.assert_not_called()
    assert result.delivered is False
    assert "consistency token" in result.actions[-1]
