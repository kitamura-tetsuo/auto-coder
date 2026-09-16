# Security & Access Control

  github_author_allowlist:
    description: "Restrict automatic processing of GitHub Issues and Pull Requests to explicitly trusted GitHub users based on numeric user IDs."
    implementation: |
      get_issue_allowlist_from_config / get_pr_allowlist_from_config in src/auto_coder/llm_backend_config.py,
      ISSUE_ALLOWLIST / PR_ALLOWLIST in src/auto_coder/automation_config.py,
      _is_issue_author_allowed / _is_pr_author_allowed in src/auto_coder/automation_engine.py,
      author_id extraction in src/auto_coder/util/gh_cache.py
    configuration:
      file: "~/.auto-coder/config.toml or .auto-coder/config.toml"
      example: |
        [github]
        issue_allowlist = [12345678, 87654321]
        pr_allowlist = [12345678, 87654321]
    behavior:
      - "Supports separate allowlists for Issues (`issue_allowlist`) and Pull Requests (`pr_allowlist`)."
      - "Stores GitHub numeric user IDs rather than usernames because user IDs are immutable."
      - "When an allowlist is configured, Issues/PRs authored by non-allowlisted users are completely and silently ignored."
      - "No LLM calls are invoked, no CI checks or comments are made, and no labels or branches are modified for untrusted authors."
      - "Allowlist check is performed as early as possible before processing any issue or PR content."
      - "When an allowlist is not set (None), all authors are allowed by default for backward compatibility."
      - "When an allowlist is explicitly set to an empty list `[]`, all authors are denied."
      - "Supports environment variable overrides via `AUTO_CODER_ISSUE_ALLOWLIST` and `AUTO_CODER_PR_ALLOWLIST` (JSON array or comma-separated integers)."
