import importlib
import subprocess
import sys
import pytest


@pytest.mark.e2e
def test_get_actions_logs_cli():
    sp = importlib.reload(subprocess)
    from playwright.sync_api import sync_playwright

    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/16949853465/job/48039894437?pr=496"

    sp.run(["playwright", "install", "chromium"], check=False)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)
        assert "GitHub" in page.title()

    result = sp.run(
        [
            sys.executable,
            "-m",
            "src.auto_coder.cli",
            "get-actions-logs",
            "--url",
            url,
            "--github-token",
            "dummy",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
