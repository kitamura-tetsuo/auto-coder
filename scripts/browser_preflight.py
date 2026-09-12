#!/usr/bin/env python3
"""Fail-closed preflight for the dedicated `Browser Tests` GitHub Actions workflow.

Launches a real headless Chromium instance and navigates to `about:blank`
before any `browser`-marked pytest test runs. If Chromium is missing,
corrupted, or otherwise unusable, this script exits non-zero with the
launch failure printed to stderr -- turning a broken browser environment
into an explicit CI failure instead of letting individual tests fall back to
`pytest.skip(...)` (see `tests/support/browser_launch.py`) and reporting a
misleading green `Browser Tests` check.
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto("about:blank")
                title = page.title()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - report every failure mode, then fail closed
        print(f"[browser-preflight] FAILED: could not launch a real headless Chromium: {exc}", file=sys.stderr)
        return 1

    print(f"[browser-preflight] OK: launched headless Chromium and navigated about:blank (title={title!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
