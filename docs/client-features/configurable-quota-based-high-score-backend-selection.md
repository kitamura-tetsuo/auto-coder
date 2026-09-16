# Configurable Quota-Based High-Score Backend Selection

  quota_surplus_high_score_selection:
    description: "Selects candidates using the configured surplus (default) or burst quota strategy."
    implementation: |
      evaluate_backend_quota, rank_high_score_backends_by_quota, linear_planned_remaining_ratio,
      calculate_quota_surplus in src/auto_coder/quota_selector.py,
      create_high_score_backend_manager, create_high_score_cloud_backend_manager in src/auto_coder/cli_helpers.py,
      _process_issue_high_score_cloud in src/auto_coder/issue_processor.py
    behavior:
      - "Calculates planned remaining quota ratio using a weekly consumption curve (planned_remaining_ratio = time_until_reset / quota_period)."
      - "Computes quota surplus as (actual_remaining_ratio - planned_remaining_ratio) independently for each candidate backend using its own reset timestamp."
      - "Prioritizes eligible backends with the largest positive quota surplus (consuming slower than planned), avoiding premature quota exhaustion."
      - "Preserves existing capability and availability checks; ineligible backends (e.g. usage limit reached, insufficient minimum remaining percentage) are filtered out."
      - "Unmetered backends or backends without weekly quota metrics retain stable fallback ordering."
      - "Backends whose usage metrics cannot be retrieved (e.g. rate-limited token refresh or API errors) have their priority lowered below measured and unmetered backends."
      - "For local Codex, unavailable weekly usage is treated as a retrieval failure unless app-server account/read confirms an apiKey account. Missing/expired auth.json is not evidence of unmetered API-key usage. The direct OAuth loader remains in use only for WHAM operations."
      - "Configure `[quota_selection] strategy = \"burst\"` to rank usable subscription backends in an explicitly equal-priority group by earliest reset; ordered priority boundaries are never crossed. Nested order arrays declare an equal-priority group, for example `order = [[\"codex-cloud\", \"claude-routine\"], \"muse\"]`."
      - "Burst admission consumes confirmed non-zero Codex subscription quota even below the conservative reserve threshold; surplus admission retains the existing reserve threshold, and exhausted or unavailable quota remains guarded at task execution."
      - "Codex usage parsing and `usage-amount` expose `rateLimitResetCredits.availableCount` from the app-server response. Missing, malformed, or failed usage remains unavailable rather than being reported as zero, and credits are never consumed automatically."
