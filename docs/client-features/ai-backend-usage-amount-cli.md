# AI Backend Usage Amount CLI

  usage_amount_command:
    description: "Inspect quota utilization, rate limit windows, and remaining usage for Claude and Codex AI backends."
    implementation: |
      usage_amount in src/auto_coder/cli_commands_usage.py,
      check_claude_usage in src/auto_coder/claude_usage_checker.py,
      get_codex_weekly_usage in src/auto_coder/codex_usage_checker.py
    commands:
      usage_amount:
        usage: "auto-coder usage-amount [TARGET] [--backend all|claude|codex] [--json] [--no-cache] [--token TOKEN]"
        description: "Check AI backend quota and usage amounts for Claude (Anthropic OAuth) and Codex (ChatGPT OAuth)."
        options:
          - "TARGET: Optional positional argument ('all', 'claude', 'codex', default: 'all')"
          - "--backend, -b: Target backend option ('all', 'claude', 'codex')"
          - "--json: Output results in structured JSON format"
          - "--no-cache: Bypass in-memory cache and fetch fresh usage data"
          - "--token: Explicit Claude OAuth token to use for verification"
        behavior:
          - "Claude: Displays 5-hour window, 7-day window, model-specific windows (Sonnet/Opus/OAuth Apps), and extra usage / overage credit details."
          - "Claude credential discovery preserves explicit and environment token precedence, then supports Claude Code's platform storage (including macOS Keychain and `.credentials.json`) after verifying and refreshing an authenticated session through the Claude CLI. Authenticated credential-acquisition failures are reported separately from missing login, while unauthenticated users are directed to `claude auth login`."
          - "Codex: Displays weekly rate-limit window utilization, remaining percentage, reset timestamp, days until reset, minimum required threshold, and task execution allowance."
          - "Codex requires a CLI with app-server account/rateLimits/read support (verified with 0.154.0). A short-lived stdio process has a 15-second request deadline and is terminated and reaped after each read. Server output and credentials are not logged."
          - "Codex selects the codex bucket from rateLimitsByLimitId when supplied, otherwise the single rateLimits view. It selects the longest primary/secondary window of at least 10080 minutes; window position is not fixed. Missing/invalid durations, reset timestamps, or usage percentages make weekly quota unavailable."
          - "Read-only command that does not require lock acquisition or GitHub authentication."
