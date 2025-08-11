import json
from unittest.mock import Mock, patch

from src.auto_coder.automation_engine import AutomationEngine


def test_get_github_actions_logs_fallback_to_text_when_zip_fails(mock_github_client, mock_gemini_client):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
    repo_name = 'kitamura-tetsuo/outliner'
    pr_data = {
        'number': 457,
        'title': 'Test PR',
        'head_branch': 'codex/add-comprehensive-tests-for-component'
    }

    def fake_run(cmd, capture_output=True, text=False, timeout=60, cwd=None):
        # run list -> 失敗 run
        if cmd[:3] == ['gh', 'run', 'list']:
            runs = [{
                'databaseId': 16818157306,
                'headBranch': pr_data['head_branch'],
                'conclusion': 'failure',
                'createdAt': '2025-08-09T00:00:00Z',
                'status': 'completed',
                'displayTitle': 'CI',
                'url': 'https://example.com/run/16818157306'
            }]
            return Mock(returncode=0, stdout=json.dumps(runs), stderr='')
        # run view -> 失敗 job
        if cmd[:3] == ['gh', 'run', 'view'] and '--json' in cmd:
            jobs_obj = {'jobs': [{'databaseId': 47639576037, 'name': 'test', 'conclusion': 'failure'}]}
            return Mock(returncode=0, stdout=json.dumps(jobs_obj), stderr='')
        # job ZIP -> 失敗
        if cmd[:2] == ['gh', 'api'] and 'actions/jobs' in cmd[2]:
            return Mock(returncode=1, stdout=b'', stderr=b'')
        # フォールバック: ジョブのテキストログ（Playwright系のエラーフォーマットを含む）
        if cmd[:3] == ['gh', 'run', 'view'] and '--job' in cmd and '--log' in cmd:
            ui_log = (
                'INFO ok\n'
                'Error:   1) [new] \u203a e2e/new/cnt-shared-container-store-12ee98aa.spec.ts:14:5 \u203a '
                'CNT-12ee98aa: Shared Container Store \u203a container selector lists projects from store\n'
            )
            return Mock(returncode=0, stdout=ui_log, stderr='')
        return Mock(returncode=1, stdout='', stderr='unknown')

    with patch('subprocess.run', side_effect=fake_run):
        out = engine._get_github_actions_logs(repo_name, pr_data, [{'name': 'ci', 'conclusion': 'failure'}])

    assert '=== Job test (47639576037) ===' in out
    # 具体的な Playwright の失敗見出し行が抽出されること
    assert '[new] › e2e/new/cnt-shared-container-store-12ee98aa.spec.ts:14:5 › CNT-12ee98aa' in out

