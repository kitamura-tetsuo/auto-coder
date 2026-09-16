# Test Watcher

  test_watcher:
    playwright_safety:
      - "Skips Playwright execution under pytest or when AC_DISABLE_PLAYWRIGHT=1; returns synthetic report"
      - "Quick availability probe: 'npx playwright --version' with 5s timeout"
      - "Process timeout: 120s communicate() timeout; force-kill on timeout"
    watchdog_daemon: "Observer thread is daemonized; does not block interpreter exit"
    concurrency:
      - "Run-on-change spawns a daemon thread for Playwright"
