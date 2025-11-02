import importlib
import subprocess
import sys


def test_get_actions_logs_cli(_use_real_home, _use_real_commands):
    sp = importlib.reload(subprocess)

    # Try to import playwright, if not available try to install it
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not found, attempting to install...")
        sp.run([sys.executable, "-m", "pip", "install", "playwright>=1.40.0"], check=False)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Failed to install playwright, skipping test")
            return  # Skip the test if playwright installation fails

    # Install browser if not already installed
    sp.run(["playwright", "install", "chromium"], check=False)

    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/16949853465/job/48039894437?pr=496"

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
