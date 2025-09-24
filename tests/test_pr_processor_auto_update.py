from types import SimpleNamespace

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder import pr_processor as pr_module


class _DummyGitHubClient:
    """Simple stub client to exercise PR processing flows."""

    def __init__(self):
        self.details_calls: list[int] = []

    def get_open_pull_requests(self, repo_name, limit=None):
        return [SimpleNamespace(number=1)]

    def get_pr_details(self, pr):
        self.details_calls.append(pr.number)
        return {
            'number': pr.number,
            'title': f'Stub PR {pr.number}',
            'mergeable': True,
        }


def _config_with_pr_limit() -> AutomationConfig:
    cfg = AutomationConfig()
    cfg.max_prs_per_run = -1
    return cfg


def test_process_pull_requests_checks_update_before_merge_pass(monkeypatch):
    client = _DummyGitHubClient()

    call_count = {'value': 0}

    def _fake_check():
        call_count['value'] += 1
        raise SystemExit(0)

    monkeypatch.setattr(pr_module, 'check_for_updates_and_restart', _fake_check)

    cfg = _config_with_pr_limit()

    with pytest.raises(SystemExit):
        pr_module.process_pull_requests(client, cfg, dry_run=True, repo_name='owner/repo')

    assert call_count['value'] == 1
    assert client.details_calls == []


def test_process_pull_requests_second_pass_checks_update(monkeypatch):
    client = _DummyGitHubClient()

    call_order: list[str] = []

    def _fake_check():
        call_order.append('check')
        if len(call_order) == 2:
            raise SystemExit(0)

    monkeypatch.setattr(pr_module, 'check_for_updates_and_restart', _fake_check)
    monkeypatch.setattr(
        pr_module,
        '_check_github_actions_status',
        lambda repo, pr_data, config: {'success': False},
    )

    cfg = _config_with_pr_limit()

    with pytest.raises(SystemExit):
        pr_module.process_pull_requests(client, cfg, dry_run=True, repo_name='owner/repo')

    assert call_order == ['check', 'check']
    assert client.details_calls == [1]


def test_fix_pr_issues_with_testing_checks_updates_each_iteration(monkeypatch):
    check_calls: list[int] = []

    def _fake_check():
        check_calls.append(len(check_calls))
        if len(check_calls) == 2:
            raise SystemExit(0)

    monkeypatch.setattr(pr_module, 'check_for_updates_and_restart', _fake_check)
    monkeypatch.setattr(pr_module, '_apply_github_actions_fix', lambda *args, **kwargs: [])
    monkeypatch.setattr(pr_module, '_apply_local_test_fix', lambda *args, **kwargs: [])

    test_results = iter([
        {
            'success': False,
            'output': '',
            'errors': '',
            'command': 'bash scripts/test.sh',
        }
    ])

    def _fake_run_local_tests(config):
        return next(test_results)

    monkeypatch.setattr(pr_module, 'run_local_tests', _fake_run_local_tests)

    cfg = AutomationConfig()
    pr_data = {'number': 7}

    with pytest.raises(SystemExit):
        pr_module._fix_pr_issues_with_testing('owner/repo', pr_data, cfg, dry_run=True, github_logs='')

    assert check_calls == [0, 1]
