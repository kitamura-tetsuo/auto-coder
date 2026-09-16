# Early Loop Resume on PR Merge/Close & Jules Enumeration Skip

  producer_early_wake_on_pr_event:
    description: "Cuts short the main producer loop sleep duration (60s) upon PR merge or close events and resumes maintenance immediately while skipping Jules session enumeration."
    implementation: |
      notify_pr_merged_or_closed, _sleep_or_wake, _check_if_pr_merged_or_closed,
      _producer_loop, _worker_loop in src/auto_coder/automation_engine.py,
      process_github_payload in src/auto_coder/webhook_server.py
    behavior:
      - "When a PR is merged or closed (internally by a worker, via branch cleanup, or externally received via GitHub webhook), signals the engine to cut short the active wait time in _producer_loop."
      - "On early resume, bypasses time-consuming Jules session listing and checks (invalidate_jules_sessions_cache, check_and_resume_or_archive_sessions, handle_stale_jules_issue_sessions, check_and_start_recurrent_jules_tasks_async) for that single iteration."
      - "Every resumed iteration still performs the update check, but generic recurring maintenance never synchronizes or repairs the monitored checkout; checkout mutation remains owned by explicit branch-processing operations."
      - "Subsequent scheduled iterations (when sleep completes normally) continue to enumerate and check Jules sessions as usual."
