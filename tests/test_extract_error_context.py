"""_extract_error_context関数のテスト"""

import pytest

from src.auto_coder.pr_processor import _extract_error_context


def test_extract_error_context_with_playwright_error():
    """Playwrightのエラーログから十分なコンテキストを抽出できることを確認"""
    # 実際のPlaywrightエラーログに似たサンプル
    log_content = """
2025-10-27T03:25:50.0000000Z Running tests...
2025-10-27T03:25:51.0000000Z 
2025-10-27T03:25:52.0000000Z   ✓ [basic] › e2e/basic/test1.spec.ts:10:5 › Test 1 (100ms)
2025-10-27T03:25:53.0000000Z   ✓ [basic] › e2e/basic/test2.spec.ts:15:5 › Test 2 (150ms)
2025-10-27T03:25:54.0000000Z 
2025-10-27T03:25:55.0000000Z   1) [core] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links › converts plain URL to clickable link
2025-10-27T03:25:56.0000000Z 
2025-10-27T03:25:57.0000000Z     Error: expect(received).toContain(expected) // indexOf
2025-10-27T03:25:58.0000000Z 
2025-10-27T03:25:59.0000000Z     Expected substring: "<a href=\\"https://example.com\\""
2025-10-27T03:26:00.0000000Z     Received string:    "test-page-1755122947471Visit https:/example.comSecond item<!---->"
2025-10-27T03:26:01.0000000Z 
2025-10-27T03:26:02.0000000Z       47 |
2025-10-27T03:26:03.0000000Z       48 |         const firstItemHtml = await page.locator(".outliner-item").first().locator(".item-text").innerHTML();
2025-10-27T03:26:04.0000000Z     > 49 |         expect(firstItemHtml).toContain('<a href="https://example.com"');
2025-10-27T03:26:05.0000000Z          |                               ^
2025-10-27T03:26:06.0000000Z       50 |         expect(firstItemHtml).toContain(">https://example.com</a>");
2025-10-27T03:26:07.0000000Z       51 |     });
2025-10-27T03:26:08.0000000Z       52 | });
2025-10-27T03:26:09.0000000Z 
2025-10-27T03:26:10.0000000Z     at /tmp/runner/work/outliner/outliner/client/e2e/core/fmt-url-label-links-a391b6c2.spec.ts:49:31
2025-10-27T03:26:11.0000000Z 
2025-10-27T03:26:12.0000000Z   ✓ [basic] › e2e/basic/test3.spec.ts:20:5 › Test 3 (200ms)
2025-10-27T03:26:13.0000000Z   ✓ [basic] › e2e/basic/test4.spec.ts:25:5 › Test 4 (250ms)
2025-10-27T03:26:14.0000000Z 
2025-10-27T03:26:15.0000000Z   1 failed
2025-10-27T03:26:16.0000000Z   147 passed
2025-10-27T03:26:17.0000000Z   1 skipped
2025-10-27T03:26:18.0000000Z   151 did not run
2025-10-27T03:26:19.0000000Z 
2025-10-27T03:26:20.0000000Z Tests completed
"""
    
    result = _extract_error_context(log_content)
    
    # エラーメッセージが含まれていることを確認
    assert "Error: expect(received).toContain(expected)" in result
    assert "Expected substring:" in result
    assert "Received string:" in result
    assert "fmt-url-label-links-a391b6c2.spec.ts" in result
    
    # エラーの前後のコンテキストが含まれていることを確認
    assert "URL label links" in result
    assert "expect(firstItemHtml).toContain" in result
    
    # 行数が適切であることを確認（エラー行の前後10行を含む）
    result_lines = result.split('\n')
    assert len(result_lines) >= 20  # 最低でもエラー行の前後10行
    assert len(result_lines) <= 500  # 最大500行


def test_extract_error_context_with_multiple_errors():
    """複数のエラーがある場合に全てのエラーコンテキストを抽出できることを確認"""
    log_content = """
2025-10-27T03:25:50.0000000Z Running tests...
2025-10-27T03:25:51.0000000Z 
2025-10-27T03:25:52.0000000Z   1) [core] › e2e/core/test1.spec.ts:10:5 › Test 1
2025-10-27T03:25:53.0000000Z 
2025-10-27T03:25:54.0000000Z     Error: Test 1 failed
2025-10-27T03:25:55.0000000Z     Expected: true
2025-10-27T03:25:56.0000000Z     Received: false
2025-10-27T03:25:57.0000000Z 
""" + "\n".join([f"2025-10-27T03:26:{i:02d}.0000000Z   Some log line {i}" for i in range(100)]) + """
2025-10-27T03:28:00.0000000Z 
2025-10-27T03:28:01.0000000Z   2) [core] › e2e/core/test2.spec.ts:20:5 › Test 2
2025-10-27T03:28:02.0000000Z 
2025-10-27T03:28:03.0000000Z     Error: Test 2 failed
2025-10-27T03:28:04.0000000Z     Expected substring: "hello"
2025-10-27T03:28:05.0000000Z     Received string: "world"
2025-10-27T03:28:06.0000000Z 
2025-10-27T03:28:07.0000000Z   2 failed
2025-10-27T03:28:08.0000000Z   148 passed
"""
    
    result = _extract_error_context(log_content)
    
    # 両方のエラーが含まれていることを確認
    assert "Test 1 failed" in result
    assert "Test 2 failed" in result
    assert "test1.spec.ts" in result
    assert "test2.spec.ts" in result
    
    # 両方のエラーの詳細が含まれていることを確認
    assert "Expected: true" in result
    assert "Received: false" in result
    assert 'Expected substring: "hello"' in result
    assert 'Received string: "world"' in result


def test_extract_error_context_with_long_log():
    """長いログから重要な部分のみを抽出できることを確認"""
    # 1000行のログを生成（最初の100行、エラー部分、最後の100行）
    log_lines = []
    
    # 最初の100行（エラーなし）
    for i in range(100):
        log_lines.append(f"2025-10-27T03:25:{i:02d}.0000000Z   Setup line {i}")
    
    # エラー部分
    log_lines.extend([
        "2025-10-27T03:26:00.0000000Z   1) [core] › e2e/core/critical-test.spec.ts:50:5 › Critical Test",
        "2025-10-27T03:26:01.0000000Z ",
        "2025-10-27T03:26:02.0000000Z     Error: Critical failure",
        "2025-10-27T03:26:03.0000000Z     Expected substring: \"important data\"",
        "2025-10-27T03:26:04.0000000Z     Received string: \"wrong data\"",
        "2025-10-27T03:26:05.0000000Z ",
        "2025-10-27T03:26:06.0000000Z     at critical-test.spec.ts:50:31",
        "2025-10-27T03:26:07.0000000Z ",
    ])
    
    # 中間の700行（エラーなし）
    for i in range(700):
        log_lines.append(f"2025-10-27T03:27:{i%60:02d}.0000000Z   Middle line {i}")
    
    # 最後の100行（エラーなし）
    for i in range(100):
        log_lines.append(f"2025-10-27T03:28:{i:02d}.0000000Z   Cleanup line {i}")
    
    log_content = "\n".join(log_lines)
    
    result = _extract_error_context(log_content, max_lines=500)
    
    # エラー部分が含まれていることを確認
    assert "Critical failure" in result
    assert "critical-test.spec.ts" in result
    assert 'Expected substring: "important data"' in result
    assert 'Received string: "wrong data"' in result
    
    # 結果が最大行数以下であることを確認
    result_lines = result.split('\n')
    assert len(result_lines) <= 500
    
    # エラーの前後のコンテキストが含まれていることを確認
    assert "Critical Test" in result


def test_extract_error_context_no_errors():
    """エラーがない場合は最初の部分を返すことを確認"""
    log_content = "\n".join([
        f"2025-10-27T03:25:{i:02d}.0000000Z   Test line {i}"
        for i in range(600)
    ])
    
    result = _extract_error_context(log_content, max_lines=500)
    
    # 最大行数以下であることを確認
    result_lines = result.split('\n')
    assert len(result_lines) <= 500
    
    # 最初の部分が含まれていることを確認
    assert "Test line 0" in result
    assert "Test line 1" in result


def test_extract_error_context_empty_log():
    """空のログを処理できることを確認"""
    result = _extract_error_context("")
    assert result == ""
    
    result = _extract_error_context(None)
    assert result == ""


def test_extract_error_context_preserves_important_context():
    """エラーの重要なコンテキストが保持されることを確認"""
    log_content = """
2025-10-27T03:25:50.0000000Z Test setup started
2025-10-27T03:25:51.0000000Z Navigating to page
2025-10-27T03:25:52.0000000Z Page loaded
2025-10-27T03:25:53.0000000Z Clicking button
2025-10-27T03:25:54.0000000Z Waiting for response
2025-10-27T03:25:55.0000000Z 
2025-10-27T03:25:56.0000000Z   1) [core] › e2e/core/button-click.spec.ts:30:5 › Button click test
2025-10-27T03:25:57.0000000Z 
2025-10-27T03:25:58.0000000Z     Error: expect(received).toContain(expected)
2025-10-27T03:25:59.0000000Z 
2025-10-27T03:26:00.0000000Z     Expected substring: "Success message"
2025-10-27T03:26:01.0000000Z     Received string: "Error: Network timeout"
2025-10-27T03:26:02.0000000Z 
2025-10-27T03:26:03.0000000Z     at button-click.spec.ts:30:31
2025-10-27T03:26:04.0000000Z 
2025-10-27T03:26:05.0000000Z Test teardown completed
"""
    
    result = _extract_error_context(log_content)
    
    # エラーメッセージとその前後のコンテキストが含まれていることを確認
    assert "Button click test" in result
    assert "Error: expect(received).toContain(expected)" in result
    assert 'Expected substring: "Success message"' in result
    assert 'Received string: "Error: Network timeout"' in result
    
    # エラーの前のセットアップ情報も含まれていることを確認（前10行）
    assert "Waiting for response" in result or "Clicking button" in result
    
    # エラーの後の情報も含まれていることを確認（後10行）
    assert "button-click.spec.ts:30:31" in result

