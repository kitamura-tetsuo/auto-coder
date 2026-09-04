from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig, PRProcessingOutcome
from auto_coder.exceptions import AutoCoderRetryableBackendError
from auto_coder.pr_processor import _apply_pr_actions_directly, _process_pr_for_fixes


def test_pr_fix_transport_outage_is_deferred_without_attempt_transition():
    """The PR lifecycle preserves a classified failure instead of routing it as a bad fix."""
    github = MagicMock()
    label_context = MagicMock()
    label_context.__enter__.return_value = True
    label_context.__exit__.return_value = None
    diagnostic = "Retryable Codex backend/transport failure: reconnect attempts exhausted; " "404 Not Found at /backend-api/codex/responses"

    backend_manager = MagicMock()
    backend_manager._run_llm_cli.side_effect = AutoCoderRetryableBackendError(diagnostic)

    def production_merge_path(*_args, **_kwargs):
        return _apply_pr_actions_directly(github, "owner/repo", {"number": 1670, "body": "Closes #1673"}, AutomationConfig())

    with (
        patch("auto_coder.pr_processor.LabelManager", return_value=label_context),
        patch("auto_coder.pr_processor._handle_pr_merge", side_effect=production_merge_path),
        patch("auto_coder.pr_processor.get_llm_backend_manager", return_value=backend_manager),
        patch("auto_coder.pr_processor._get_pr_diff", return_value="diff"),
        patch("auto_coder.pr_processor.increment_attempt") as increment,
        patch("auto_coder.pr_processor._trigger_fallback_for_pr_failure") as fallback,
    ):
        result = _process_pr_for_fixes(github, "owner/repo", {"number": 1670, "body": "Closes #1673"}, AutomationConfig())

    assert result.outcome == PRProcessingOutcome.DEFERRED
    assert result.error == diagnostic
    assert result.actions_taken == [f"Deferred: {diagnostic}"]
    backend_manager._run_llm_cli.assert_called_once()
    fallback.assert_not_called()
    increment.assert_not_called()
