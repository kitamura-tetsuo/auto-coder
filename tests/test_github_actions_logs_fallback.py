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
                'Error:   1) [core] \u203a e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 \u203a '
                'URL label links \u203a converts plain URL to clickable link \n\n'
                '    Error: expect(received).toContain(expected) // indexOf\n\n'
                '    Expected substring: "<a href=\\"https://example.com\\""\n'
                '    Received string:    "test-page-1755122947471Visit https:/example.comSecond item<!---->"\n\n'
                '      47 |\n'
                '      48 |         const firstItemHtml = await page.locator(".outliner-item").first().locator(".item-text").innerHTML();\n'
                '    > 49 |         expect(firstItemHtml).toContain(\'<a href="https://example.com"\');\n'
                '         |                               ^\n'
                '      50 |         expect(firstItemHtml).toContain(">https://example.com</a>");\n'
                '      51 |     });\n'
                '      52 | });\n'
            )
            return Mock(returncode=0, stdout=ui_log, stderr='')
        return Mock(returncode=1, stdout='', stderr='unknown')

    with patch('subprocess.run', side_effect=fake_run):
        out = engine._get_github_actions_logs(repo_name, pr_data, [{'name': 'ci', 'conclusion': 'failure'}])

    assert '=== Job test (47639576037) ===' in out
    # 具体的な Playwright の失敗見出し行が抽出されること
    assert '[core] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links' in out
    # 期待/受領メッセージが含まれること
    assert 'Expected substring: "<a href="https://example.com"' in out
    assert 'Received string:' in out

def test_extract_playwright_heading_without_error_prefix(mock_github_client, mock_gemini_client):
    """Playwright の見出しに "Error:" 接頭辞が無い場合でも検出できることを確認。"""
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
    repo_name = 'kitamura-tetsuo/outliner'
    pr_data = {
        'number': 999,
        'title': 'Test PR',
        'head_branch': 'codex/ensure-playwright-header-detection'
    }

    def fake_run(cmd, capture_output=True, text=False, timeout=60, cwd=None):
        if cmd[:3] == ['gh', 'run', 'list']:
            runs = [{
                'databaseId': 17000000000,
                'headBranch': pr_data['head_branch'],
                'conclusion': 'failure',
                'createdAt': '2025-08-10T00:00:00Z',
                'status': 'completed',
                'displayTitle': 'CI',
                'url': 'https://example.com/run/17000000000'
            }]
            return Mock(returncode=0, stdout=json.dumps(runs), stderr='')
        if cmd[:3] == ['gh', 'run', 'view'] and '--json' in cmd:
            jobs_obj = {'jobs': [{'databaseId': 49000000000, 'name': 'test', 'conclusion': 'failure'}]}
            return Mock(returncode=0, stdout=json.dumps(jobs_obj), stderr='')
        # ZIP ダウンロードは失敗させる
        if cmd[:2] == ['gh', 'api'] and 'actions/jobs' in cmd[2]:
            return Mock(returncode=1, stdout=b'', stderr=b'')
        # テキストログ（先頭に Error: が無い Playwright 見出し）
        if cmd[:3] == ['gh', 'run', 'view'] and '--job' in cmd and '--log' in cmd:
            ui_log = (
                'info misc\n'
                '  1) [basic] \u203a e2e/basic/sea-page-title-search-box-a3674e4f-dce0-4543-9e85-1f1899f97f73.spec.ts:16:5 \u203a '
                'SEA-0001: page title search box \u203a search box navigates to another page\n\n'
                '    Error: expect(received).toContain(expected) // indexOf\n\n'
                '    Expected substring: "Some Title"\n'
                '    Received string:    "Other Title"\n'
            )
            return Mock(returncode=0, stdout=ui_log, stderr='')
        return Mock(returncode=1, stdout='', stderr='unknown')

    with patch('subprocess.run', side_effect=fake_run):
        out = engine._get_github_actions_logs(repo_name, pr_data, [{'name': 'ci', 'conclusion': 'failure'}])

    # 見出しが含まれること（Error: なしでも）
    assert '[basic] › e2e/basic/sea-page-title-search-box' in out
    assert 'SEA-0001: page title search box' in out
    assert 'Expected substring: "Some Title"' in out
    assert 'Received string:    "Other Title"' in out
