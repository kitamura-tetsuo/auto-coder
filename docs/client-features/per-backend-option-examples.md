# Per-Backend Option Examples

The following examples demonstrate typical `options` and `options_for_noedit` configurations for each supported backend:

- **Codex Backend** (`backend_type = "codex"`):
  ```toml
  [codex]
  model = "codex"
  options = ["--dangerously-bypass-approvals-and-sandbox"]
  options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox"]
  ```
  - Typical use: OpenAI-compatible providers (OpenRouter, Azure OpenAI, custom endpoints)
  - Automatic flags: `--dangerously-bypass-approvals-and-sandbox` during code editing operations
  - Custom options: Add tracking headers, timeouts, or provider-specific flags

- **Claude Backend** (`backend_type = "claude"`):
  ```toml
  [claude]
  model = "sonnet"
  backend_type = "claude"
  options = []
  options_for_noedit = []
  ```
  - Typical use: Anthropic Claude API
  - Local print-mode tasks deliver the complete prepared prompt as finite UTF-8 stdin
    (including retries and explicit session continuations), never as an argv or
    environment payload. Conflicting task-input selectors and non-text input formats
    are rejected before the task is launched.
  - Automatic flags: `--print`, `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`
  - Custom options: Path to settings file, debug modes
  - Cloud sessions: when `options` contain `--cloud` (or `--cloud=<description>`), the run is
    interactive-only. `--print` is never added and any `--print` / `--output-format <format>`
    entry is removed from the options, because the claude CLI rejects
    `--cloud` combined with `--print` and `--output-format` only works with `--print`.
    The prompt is passed as the trailing argument and becomes the cloud task description.
    Such runs are executed through a pseudo terminal (`CommandExecutor.run_command(..., use_pty=True)`),
    because the claude CLI aborts with "--cloud requires an interactive terminal" when stdout is a pipe.
    In pty mode stdout and stderr are merged into a single stream and ANSI escape sequences are stripped
    from the captured output.
  - Configuration recovery: before invoking the CLI, a missing or unparsable `~/.claude.json`
    (or `$CLAUDE_CONFIG_DIR/.claude.json`) is restored from the newest usable backup in
    `~/.claude/backups/`; an unparsable file is preserved as `.claude.json.corrupt.<timestamp>`.

- **Gemini Backend** (`backend_type = "antigravity"`):
  ```toml
  [gemini]
  model = "gemini-2.5-pro"
  backend_type = "antigravity"
  options = []
  options_for_noedit = []
  ```
  - Typical use: Google Gemini API
  - Automatic flags: `--dangerously-skip-permissions`, `--force-model`
  - Custom options: Debug modes, output formatting

- **Qwen Backend** (`backend_type = "qwen"`):
  ```toml
  [qwen]
  model = "qwen3-coder-plus"
  backend_type = "qwen"
  options = []
  options_for_noedit = []
  ```
  - Typical use: Native Qwen CLI with OAuth authentication
  - Automatic flags: `-y` (auto-confirm)
  - Custom options: Stream settings, debug modes, timeouts

- **Qwen via OpenRouter** (`backend_type = "codex"`):
  ```toml
  [qwen-openrouter]
  model = "qwen/qwen3-coder:free"
  backend_type = "codex"
  openai_api_key = "sk-or-v1-your-key"
  openai_base_url = "https://openrouter.ai/api/v1"
  options = ["-o", "HTTPReferer", "https://yourapp.com", "-o", "XTitle", "Auto-Coder"]
  options_for_noedit = ["-o", "timeout", "30"]
  ```
  - Typical use: Qwen models through OpenAI-compatible API
  - Automatic flags: `--dangerously-bypass-approvals-and-sandbox`
  - Custom options: Tracking headers, timeouts, stream settings

- **Auggie Backend** (`backend_type = "auggie"`):
  ```toml
  [auggie]
  model = "GPT-5"
  backend_type = "auggie"
  options = []
  options_for_noedit = []
  ```
  - Typical use: AugmentCode Auggie CLI
  - Automatic flags: `--print`
  - Task instructions are passed as exact UTF-8 through a private, external `--instruction-file`; the file is removed after the direct CLI child exits.
  - Custom options: Debug modes, output formatting (configured instruction-source options are rejected)

- **Jules Backend** (`backend_type = "jules"`):
  ```toml
  [jules]
  backend_type = "jules"
  options = []
  options_for_noedit = []
  ```
  - Typical use: Session-based AI assistant (Jules)
  - Automatic flags: Minimal - Jules manages sessions internally
  - Custom options: Usually minimal due to session-based nature
