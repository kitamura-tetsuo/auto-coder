import os

import pytest

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.github_client import GitHubClient


@pytest.mark.e2e
@pytest.mark.headless
class TestPRChecksNoChecks:
    def test_pr_with_no_checks_reports_as_success(self):
        token = os.environ.get("GITHUB_TOKEN", "placeholder-token")
        github_client = GitHubClient(token)
        engine = AutomationEngine(github_client, None)
        pr_data = {"number": 515}
        result = engine._check_github_actions_status("kitamura-tetsuo/outliner", pr_data)
        assert result["success"] is True
        assert len(result["failed_checks"]) == 0
