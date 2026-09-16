# Repository-Scoped Partial Configuration Overrides

Auto-Coder supports repository-scoped partial overrides for both `llm_config.toml` and `config.toml` settings. When processing a task for a repository (e.g. `kitamura-tetsuo/auto-coder`), settings from a repository-specific configuration file are merged recursively on top of the base configuration.
- **Base Configuration Files**:
  - `llm_config.toml`: `~/.auto-coder/llm_config.toml` (or `.auto-coder/llm_config.toml` in the working directory / `AUTO_CODER_CONFIG_PATH`).
  - `config.toml`: `~/.auto-coder/config.toml` (or `.auto-coder/config.toml` in the working directory).
  - **Tables / Dictionaries**: Recursively merged so sibling keys not specified in the override remain inherited from the base configuration.
  - **Lists / Arrays**: Entire list is replaced by the override (no concatenation).
  - **Missing keys**: Retain base configuration values.
  - **Incompatible types / Invalid TOML**: Fails safely with `ValueError`.
- **Repository Isolation**: Each repository resolution is isolated and evaluated in its own context without mutating global base configuration state. Sequential tasks across multiple repositories maintain strict isolation.
- **Examples**:
  - **`llm_config.toml` Example**:
    Base `~/.auto-coder/llm_config.toml`:
    ```toml
    [backend]
    default = "codex"
    order = ["codex", "claude"]

    [backends.codex_cloud]
    environment = "env-autocoder-999"
    attempts = 3
    ```
    Result for `kitamura-tetsuo/auto-coder`: `environment = "env-autocoder-999"`, `attempts = 3`, and `model = "codex-base"` (inherited from base).

  - **`config.toml` Example**:
    Base `~/.auto-coder/config.toml`:
    ```toml
    [jules]
    enabled = true
    wait_timeout_hours = 2

    [github_action]

  - Example:
    ```toml
    [backends.gemini]
    model = "gemini-2.5-pro"
    usage_markers = ["rate limit", "quota exceeded", "custom error message"]

    [backends.claude]
    model = "sonnet"
    usage_markers = ["api error: 429", "usage limit exceeded", "5-hour limit reached"]

    [backends.codex]
    model = "codex"
    usage_markers = ["rate limit", "usage limit", "upgrade to pro"]

    [backends.auggie]
    model = "GPT-5"
    usage_markers = ["rate limit", "quota"]

    [backends.qwen]
    model = "qwen3-coder-plus"
    usage_markers = ["rate limit", "quota", "too many requests", "custom marker"]
    ```
  - Behavior:
    - When configured, the specified markers are used to detect usage limit errors
    - When not configured, clients fall back to their built-in default markers
    - Markers are matched case-insensitively against the CLI output
    - JSON markers are matched as partial JSON (subset) against any JSON payload found in the CLI output, including structured log lines
    - Independently of configured/default markers, `qwen`, `auggie`, and `muse` backends also detect an unambiguous HTTP 429 (Too Many Requests) status reference (e.g. "HTTP 429", "Error 429:", "status: 429", "429 Too Many Requests") via `has_http_429_marker` in `usage_marker_utils.py`. This requires context around the number (a preceding "http"/"error"/"status"/"code" label, or a following "too many requests"), unlike a bare `"429"` usage marker, so it does not misdetect unrelated numbers such as line numbers, ports, or counts.
  - Configuration file: "~/.auto-coder/llm_config.toml"
  - Optional field: Backward compatible with existing configurations

- **backend_with_high_score**: Fallback backend configuration for PRs when attempt count reaches 3
  - Purpose: Automatically switches to a different backend when a PR has been attempted 3 times without success
  - Trigger: When any linked issue in a PR has an attempt count >= 3, the system switches to this fallback backend for all subsequent LLM operations on that PR
  - Configuration section: `[backend_with_high_score]` in the TOML config file
  - Example:
    ```toml
    [backend_with_high_score]
    name = "antigravity"
    model = "gemini-2.5-flash"
    api_key = "your-api-key"
    ```
  - Behavior:
    - Checks attempt count for all issues linked in the PR body (using keywords like "close", "fix", "resolve")
    - Uses max attempt count across all linked issues
    - When max attempt >= 3, switches to the fallback backend before making any LLM calls
    - Supports all standard backend configuration fields (model, api_key, base_url, etc.)
    - If no fallback is configured, processing continues with the current backend (no error)
  - Configuration file: "~/.auto-coder/llm_config.toml"
- **backend_cloud**: Cloud backend configuration for standard (non-difficult) tasks
    [backends.codex-cloud-luna]
  - Purpose: Automatically delegates difficult issues to high-scoring cloud coding agents (like Claude Routine or Jules)
  - Trigger: When an issue has the `difficult` label, the system skips Jules mode and delegates directly to `backend_with_high_score_cloud`
  - Configuration section: `[backend_with_high_score_cloud]` in the TOML config file
  - Example:
    ```toml
    [backend_with_high_score_cloud]
    order = ["codex-cloud", "claude-opus-routine"]

    [backends.codex-cloud]
    backend_type = "codex-cloud"
    # Codex Cloud selects the model server-side; omit model for this backend.
    environment_id = "<Codex Cloud environment ID>"
    attempts = 1

    [backends.claude-opus-routine]
    backend_type = "claude-routine"
    url = "https://api.anthropic.com/v1/claude_code/routines/trig_01WSZRQzV8M7sWXAoTGQf2sn/fire"
    claude_code_routine_token = "sk-ant-oat01-..."
    ```
  - Behavior:
    - `backend_cloud.order` remains a strict backend priority list. Alternatively,
      `backend_cloud.priority_groups` accepts an ordered array of non-empty
      backend-name arrays. Members of one inner array have equal priority and are
      quota-ranked, while no member of a later group can cross an earlier group.
      The two settings are mutually exclusive, including after repository-specific
      configuration overrides are merged; invalid group shapes fail configuration
      loading.
    - Backend names in an `order` array retain their declared priority after
      quota eligibility checks; an ineligible entry may be skipped without
      reordering the remaining entries. Quota-surplus ranking applies only to
      backends explicitly placed in the same equal-priority set/group and
      cannot promote a later candidate or group across an earlier priority
      boundary.
    - "Explicit Issue restart: `auto-coder process-issues --only <issue> --force --retry` authorizes a new implementation using current repository backend configuration even when provider sessions are retained. `--retry` requires both other flags and rejects PR targets. Readiness, specification, hierarchy, dependencies, and live local dispatch serialization still apply. The accepted session replaces cloud.csv tracking and is added to the same Issue slot; previous session membership remains because retry does not cancel remote work. Failed submission preserves the previous binding. Codex Cloud uses a new durable attempt and retains earlier CloudRuns. Ordinary runs and --force without --retry retain duplicate protection. The process-local issue.manual-retry stage records authorization, not successful implementation."
    - Single-item processing (`process-issues --only`) honors the repository's cloud/Jules mode configuration, just like continuous issue processing.
    - When `backend_type = "codex-cloud"` is used, the system invokes `codex cloud exec --env <environment_id> --branch <base_branch>` and records the returned task ID.
    - Before creating a Codex Cloud task, weekly usage is read through `codex app-server` using `initialize`, `initialized`, and `account/rateLimits/read` over stdio. Codex manages authentication and refresh using its own configuration and `CODEX_HOME`; Auto-Coder does not read OAuth tokens or call the usage HTTP endpoint directly. Surplus admission requires remaining percentage of at least `(days_until_reset + 1) * 5`; burst requires nonzero remaining weekly quota. Missing CLI/authentication, RPC errors, timeouts, and malformed responses leave quota unavailable and prevent Cloud submission. No LLM turn is started and no reset credit is consumed.
    - Codex Cloud supports `continue_if_paused()` via the internal WHAM backend API (`POST https://chatgpt.com/backend-api/wham/tasks`). When CI checks fail on a Codex Cloud-created PR, auto-coder resolves the latest assistant turn ID via WHAM turn APIs and sends a follow-up continuation prompt using Codex OAuth credentials (never the OpenAI API key), allowing Codex to resume and fix the PR without spawning new tasks. This cloud routing is preserved when label checks are disabled (including `--only` and WIP resumption) and when the local checkout already matches the PR branch; Auto-Coder does not check out or repair the cloud PR locally. A failed continuation delivery is reported as retryable and is never described as successfully handled, while local repair remains disabled. Repeated resumptions do not post duplicate informational PR comments; Jules fix-request notifications use the same deduplication rule.
    - Cloud clients may independently implement `send_followup()` for assigning new work to an existing task after it has created a PR; this capability is distinct from paused-task continuation. When a Codex Cloud or Claude cloud PR is still conflicting, Auto-Coder sends the existing session a repair instruction using the PR's actual head and base branches before local resolution. A provider-confirmed delivery adds a task-, PR-, purpose-, and head-specific action and an idempotent informational PR comment; comment publication is retried independently and can never cause the non-idempotent provider request to be resent. Successful delivery stops that processing pass, while failed or unsupported delivery retains the prior fallback. Durable deduplication keys include repository/PR identity plus head and base states, with explicit pending and confirmed states: unchanged confirmed work is not resent, an unconfirmed reservation is deferred without being falsely reported as delegated, and changed heads or bases may be delegated again. Delivery never marks the PR mergeable or bypasses normal CI, review-thread, adversarial-validation, or mergeability gates.
    - Codex Cloud pull requests whose remote head is exactly `work` are closed before diff, CI, merge, or checkout processing because that shared branch is not a safe task identity. The existing cloud task is instructed to preserve its implementation and publish a replacement PR from a unique issue/task branch without reusing or force-pushing `work`. Successful delivery keeps the linked issue in its current cloud flow; failed resolution or delivery releases it through the normal attempt/retry policy.
    - Codex Cloud selects its model server-side. If `model` is configured, Auto-Coder warns that user-selected models are unsupported, ignores the value, and starts the task without a model CLI override.
    - `environment_id` is required for Codex Cloud; `attempts` optionally configures best-of-N execution and defaults to 1.
    - When `backend_type = "claude-routine"` is used, the system triggers the Claude routine via HTTP POST to the configured fire endpoint.
    - Saves the returned session ID in `cloud.csv` together with the independent provider family and exact configured backend name, so Claude Routine follow-ups retain the same URL, token, and options after restart; legacy Claude Routine rows use the historical `claude-routine` backend identity.
    - Comments on the issue with the session link and retains the `@auto-coder` label.
    - Subsequent pull requests opened by Claude routines are tracked and linked to the issue.
    - Parent-issue verification uses the same lifecycle-aware dispatch: asynchronous task/session IDs are persisted and reported on the issue, and `@auto-coder` remains until the existing cloud completion lifecycle releases it. Local high-score backends retain synchronous processing.
  - Configuration file: "~/.auto-coder/llm_config.toml"


- **Claude / Claude-Routine Usage Quota Pre-Check & Failover**:
  - Purpose: Automatically checks remaining rate limit and usage allowances before calling Claude or Claude-Routine backends.
  - Trigger & Thresholds:
    - 5-hour rolling session limit has **<= 20% remaining** (utilization >= 80%), OR
    - 7-day weekly limit (including model-specific limits like `seven_day_sonnet`, `seven_day_opus`, `seven_day_oauth_apps`) has **<= 5% remaining** (utilization >= 95%), OR
    - Queries `https://api.anthropic.com/api/oauth/usage` before invoking the LLM CLI or firing a routine.
    - If remaining quota is at or below the threshold or rate limits are reached, raises `AutoCoderUsageLimitError` with the reset timestamp and reason.
    - Seamlessly triggers standard `BackendManager` rotation to the next backend in the configured order (including failover in `backend_with_high_score_cloud_order`) or defers until reset.
    - Automatically discovers OAuth tokens from backend configuration (`claude_code_oauth_token`, `claude_code_routine_token`), environment variables, or credentials file (`~/.claude/.credentials.json`).
    - Proactively checks `expiresAt` timestamp and exchanges refresh tokens via JSON payload with `client_id` to prevent authorization failures.
    - Accurately captures HTTP 429 rate limits from token refresh and usage endpoints to avoid runaway task spawning.
    - Handles account-wide weekly limit entries whose API scope has no model, while preserving threshold detection and reset timestamps.
  - Default: `[]` (empty list)
  - Usage: Applied when the LLM is invoked for analyzing issues, generating code, fixing bugs, and other file-modifying operations
  - Example:
    ```toml
    [backends.codex]
    model = "codex"
    options = ["--dangerously-bypass-approvals-and-sandbox"]
    ```
  - Behavior:
    - The system automatically adds required flags for each backend type during execution (e.g., `--dangerously-bypass-approvals-and-sandbox` for Codex)
    - Custom options specified here are appended to the automatically-added flags
    - Different backends may interpret options differently
    - Supports placeholder replacement for dynamic values (see Placeholder Replacement System below)
  - Configuration file: "~/.auto-coder/llm_config.toml"
  - Optional field: Backward compatible with existing configurations

- **`options_for_noedit` field**: CLI options for message generation (non-editing operations)
  - Purpose: Pass additional command-line arguments to backend CLIs when generating commit messages, PR descriptions, and other non-code operations
  - Type: List[str]
  - Default: `[]` (empty list)
  - Usage: Applied when the LLM is invoked for message generation operations (not code editing)
  - Example:
    ```toml
    [backends.gemini]
    model = "gemini-2.5-pro"
    options = ["--debug"]
    options_for_noedit = ["--silent"]
    ```
  - Behavior:
    - If not specified or empty, falls back to using `options` value
    - Allows different configurations for editing vs message generation
    - The system automatically adds required flags for each backend type during execution
    - Useful for optimizing different operations with different option sets
  - Configuration file: "~/.auto-coder/llm_config.toml"
  - Optional field: Backward compatible with existing configurations

- **`backend_for_noedit` configuration**: Separate backend configuration for non-editing operations
  - Purpose: Configure a different backend (or backend order) for message generation compared to code editing
  - Breaking Change: Renamed from `message_backend` to `backend_for_noedit`
  - Configuration Sections:
    - `[backend_for_noedit]`: Top-level section for non-editing backend configuration
    - Fields:
      - `default`: Default backend name for non-editing operations
      - `order`: List of backend names to try for non-editing operations (in order)
  - Example:
    ```toml
    [backend]
    default = "qwen"
    order = ["qwen", "antigravity", "claude"]

    [backend_for_noedit]
    default = "claude"
    order = ["claude", "qwen"]
    ```
  - Behavior:
    - If not specified, falls back to general backend configuration (`[backend]` section)
    - Allows optimization: use fast/cheap models for messages, premium models for code
    - Backward compatible: old `message_backend` key still works but emits deprecation warning
  - Migration:
    - Replace `[message_backend]` with `[backend_for_noedit]` in config files
    - Replace environment variable `AUTO_CODER_MESSAGE_DEFAULT_BACKEND` with `AUTO_CODER_NOEDIT_DEFAULT_BACKEND`
  - Configuration file: "~/.auto-coder/llm_config.toml"
  - Optional field: Defaults to general backend configuration if not specified

- **Issue discovery and implementation admission**: Ordinary open Issues are collected on every candidate scan, independently of the number, priority, mergeability, or review state of open PRs. Existing candidate priorities still determine scheduling order after discovery. Starting Issue work remains separately controlled by the durable `[process_issues].max_concurrent_implementations` slots. Issue retries, provider sessions, and linked PRs share one persisted Issue-owned slot; standalone PRs own separate slots. Slots survive restarts, remain held during asynchronous and CI/review waits, and are normally released after authoritative terminal GitHub state is observed. Confirmed pre-submission Cloud rejection also releases unbound idle ownership, as described above. The state and lock files inherit the containing runtime directory's group and retain group read/write access (without world access) across every atomic replacement, allowing intentionally shared execution identities to coordinate safely. Permission or ownership setup failures are distinct fail-closed errors and never reset durable state. The `urgent` label retains its single durable emergency slot beyond normal capacity, and explicit `--only` / `--force` overrides retain their existing admission semantics.
- **Event-driven implementation-capacity refill**: A running daemon observes its cross-process implementation-slot repository. When normal capacity changes from full to available, it obtains a new authoritative open-Issue enumeration, strictly refreshes and priority-ranks every candidate, and sends candidates through the ordinary dispatch checks until capacity is full or the snapshot is exhausted. Rejected candidates do not consume the refill opportunity. Failed enumeration remains a level-triggered obligation and is retried, while idle steady state never polls GitHub for candidates.

- **Adversarial-review-blocked PR priority (issue #1731)**: Deprioritizes PRs that cannot make progress at their unchanged HEAD
  - Purpose: A PR whose current HEAD already completed Auto-Coder's own adversarial validation with material specification violations (`NEEDS_FIX` or `NEEDS_TESTS`) has no further normal action to perform until the PR author/backend supplies a new commit. Without this, such a PR keeps competing at the ordinary auto-merge-candidate priority (2), crowding out genuinely actionable work.
  - Implementation: `is_current_head_adversarial_review_blocked()` in `pr_processor.py`, called from `AutomationEngine._get_candidates()`.
  - Behavior:
    - When `[ENABLE_ADVERSARIAL_VALIDATION]` is enabled, the PR is not a dependency-bot PR, and it is otherwise mergeable with passing checks, the dedicated reviewer App's authoritative same-SHA verdict (looked up the same way the merge gate does, via `_get_published_adversarial_validation_status`) is consulted for the PR's exact current `head.sha`.
    - `NEEDS_FIX` or `NEEDS_TESTS` for the current HEAD lowers the PR to `pr_priority = 1`, the same tier as a mergeable PR with failing GitHub Actions checks.
    - The classification is re-derived from the current HEAD on every candidate-collection pass; a verdict recorded against an older SHA never suppresses a newer HEAD, so a fresh commit immediately restores the ordinary priority even before a new adversarial result exists for it.
    - Validator errors, timeouts, or indeterminate verdicts (`ERROR`, `BLOCKED`, `INCONCLUSIVE`), a PR with no linked-Issue specification oracle, and any lookup failure all resolve to "not blocked" so they remain retried rather than deprioritized.
    - Only the dedicated `auto-coder-reviewer` GitHub App's own marker-scoped verdict counts; a generic human `CHANGES_REQUESTED` review or unrelated unresolved review thread never triggers this classification on its own.
    - An independently applicable higher-priority condition (unmergeable, `urgent`, breaking-change) is unaffected: this check only runs, and can only apply, in the branch that would otherwise assign the mergeable/passing-checks priority of 2.
