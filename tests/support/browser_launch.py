"""Shared real-Chromium launch helper for `browser`-marked regression tests.

Both `tests/test_dashboard_detail_scroll_stability.py` and
`tests/test_dashboard_slots_scroll_stability.py` drive a real headless
Chromium tab against a real uvicorn server; this module centralizes the one
piece of launch behavior that differs between an ordinary/local run (where a
missing browser should not block unrelated work) and the dedicated
`Browser Tests` CI workflow (where a missing or broken browser must fail the
check rather than silently skip -- see Issue "Run browser tests in a
dedicated GitHub Actions workflow").

Set `AUTO_CODER_REQUIRE_BROWSER=1` (done by `.github/workflows/browser-tests.yml`)
to turn an unusable Chromium/launch environment into a raised exception
(test error) instead of `pytest.skip(...)`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import pytest
from playwright.sync_api import Browser, Page, sync_playwright


def resolve_chromium_executable() -> Optional[str]:
    """Prefer a pre-installed Chromium under PLAYWRIGHT_BROWSERS_PATH if the
    default revision-matched lookup would miss it (as happens in some
    sandboxed environments that ship one fixed Chromium revision); otherwise
    let Playwright resolve its own default installation.

    Playwright itself already honors `PLAYWRIGHT_BROWSERS_PATH` at both
    install and launch time (independent of `$HOME`), so setting that
    environment variable to a stable absolute path is the actual fix for the
    per-test `HOME` rewrite; this function only adds a narrow, harmless
    fallback for the exact-executable case.
    """
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path:
        candidate = Path(browsers_path) / "chromium"
        if candidate.exists():
            return str(candidate)
    return None


@contextmanager
def headless_page(*, viewport: dict) -> Iterator[Page]:
    """Launch a real headless Chromium page for the duration of the `with` block.

    Raises when the browser is unusable and `AUTO_CODER_REQUIRE_BROWSER=1` is
    set (the dedicated Browser Tests workflow); otherwise falls back to
    `pytest.skip(...)` so ordinary/local runs without a provisioned browser
    are not blocked by this file.
    """
    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
        executable_path = resolve_chromium_executable()
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        try:
            browser: Browser = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # pragma: no cover - environment without a usable browser
            if os.environ.get("AUTO_CODER_REQUIRE_BROWSER") == "1":
                raise
            pytest.skip(f"no usable headless Chromium in this environment: {exc}")
        try:
            page = browser.new_page(viewport=viewport)
            yield page
        finally:
            browser.close()
