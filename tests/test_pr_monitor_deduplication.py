import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.dispatch_claim_store import DispatchClaimStore, DispatchOutcome
from src.auto_coder.entity_invalidation import DurableInvalidationQueue
from src.auto_coder.pr_processor import _handle_pr_merge
from src.auto_coder.util.github_action import WorkflowDispatchResult


def test_dispatch_creates_restart_safe_watch_and_claim():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        claims = DispatchClaimStore(root / "claims.db")
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        pr = {"number": 123, "head": {"sha": "abc1234", "ref": "feature"}, "labels": []}
        with (
            patch.dict(os.environ, {"AUTO_CODER_INVALIDATION_DB": str(root / "invalidations.db")}),
            patch("src.auto_coder.pr_processor.get_dispatch_claim_store", return_value=claims),
            patch("src.auto_coder.pr_processor._check_github_actions_status", return_value=MagicMock(ids=[], error=None)),
            patch("src.auto_coder.pr_processor.get_detailed_checks_from_history"),
            patch("src.auto_coder.pr_processor.LabelManager"),
            patch("auto_coder.util.github_action.trigger_workflow_dispatch", return_value=WorkflowDispatchResult(outcome=DispatchOutcome.ACCEPTED)) as dispatch,
        ):
            actions = _handle_pr_merge(client, "owner/repo", pr, AutomationConfig(), {})
            _handle_pr_merge(client, "owner/repo", pr, AutomationConfig(), {})

        assert dispatch.call_count == 1
        assert "Created durable CI watch for ci.yml" in actions
        reopened = DurableInvalidationQueue(root / "invalidations.db")
        assert reopened.seconds_until_next_ci("owner/repo") == 0
