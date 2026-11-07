# Test Watcher

  test_watcher:
    playwright_safety:
      - "Skips Playwright execution under pytest or when AC_DISABLE_PLAYWRIGHT=1; returns synthetic report"
      - "Quick availability probe: 'npx playwright --version' with 5s timeout"
      - "Process timeout: 120s communicate() timeout; force-kill on timeout"
    watchdog_daemon: "Observer thread is daemonized; does not block interpreter exit"
    concurrency:
      - "Run-on-change spawns a daemon thread for Playwright"

`GitIgnoreFileHandler` suppresses repeated modification events for the same path
inside its 0.5-second window. An event exactly at the window boundary is eligible
again; a different file in the same directory is independently eligible. The
directory-grouping helper is separate from this native event-handler behavior.

`tests/test_shared_watcher_performance.py` verifies the real handler-to-tool
callback path with controlled event timestamps and exact downstream invocation
counts, including the window boundary and distinct-file delivery. This replaces
a direct-callback elapsed-time check that bypassed debouncing and could pass
even with all callbacks disabled. The adjacent 10-callback/1-second processing
guard and all other existing performance thresholds remain unchanged. No
production debounce or execution behavior is changed by this test correction.
