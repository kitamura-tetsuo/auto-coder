import subprocess as sp
import sys


def test_get_actions_logs_cli(_use_real_home):
    """CLIのget-actions-logsが正常終了することのみを検証する(E2E)。
    - 外部ブラウザ/ネットワーク依存(Playwright/ブラウザ起動)は一切行わない
    - URLは ?pr= クエリ付きで与える(実装はこの場合でも早期に処理できる)
    - 実際のgh呼び出しはtests/conftest.pyのスタブで安全化されている
    """
    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/16949853465/job/48039894437?pr=496"

    result = sp.run(
        [
            sys.executable,
            "-m",
            "auto_coder.cli",
            "get-actions-logs",
            "--url",
            url,
            "--github-token",
            "dummy",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    # 正常終了のみを確認(内容の詳細検証は別テストで実施済み)
    assert result.returncode == 0
