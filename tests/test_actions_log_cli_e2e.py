import importlib
import os
import subprocess
import sys

# Set Playwright browsers path before importing playwright
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/home/node/.cache/ms-playwright"


def test_get_actions_logs_cli():
    sp = importlib.reload(subprocess)
    from playwright.sync_api import sync_playwright

    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/16949853465/job/48039894437?pr=496"

    sp.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)
        assert "GitHub" in page.title()

    # Run the CLI command with the correct environment
    env = os.environ.copy()
    # Use actual home directory path, not ~ which pytest expands to temp dir
    pythonpath = f"/home/node/.local/lib/python3.11/site-packages:{os.path.abspath('src')}"
    env["PYTHONPATH"] = pythonpath

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
        env=env,
    )
    assert result.returncode == 0
