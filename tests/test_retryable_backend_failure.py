from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig, PRProcessingOutcome
from auto_coder.exceptions import AutoCoderRetryableBackendError
from auto_coder.pr_processor import _process_pr_for_fixes


def test_pr_fix_transport_outage_is_deferred_without_attempt_transition():
    """The PR lifecycle preserves a classified failure instead of routing it as a bad fix."""
    github = MagicMock()
    label_context = MagicMock()
    label_context.__enter__.return_value = True
    label_context.__exit__.return_value = None
    diagnostic = "Retryable Codex backend/transport failure: reconnect attempts exhausted; " "404 Not Found at /backend-api/codex/responses"

    with (
        patch("auto_coder.pr_processor.LabelManager", return_value=label_context),
        patch("auto_coder.pr_processor._take_pr_actions", side_effect=AutoCoderRetryableBackendError(diagnostic)),
        patch("auto_coder.pr_processor.increment_attempt") as increment,
    ):
        result = _process_pr_for_fixes(github, "owner/repo", {"number": 1670}, AutomationConfig())

    assert result.outcome == PRProcessingOutcome.DEFERRED
    assert result.error == diagnostic
    assert result.actions_taken == [f"Deferred: {diagnostic}"]
    increment.assert_not_called()
