# Backend Management

    nested_managers:
      description: "Multiple backend manager instances can exist with different configurations"
      llm_backend: "Singleton instance for general LLM operations (PR processing, test fixes, code generation)"
      noedit_backend: "Separate singleton instance for non-editing operations (commit messages, PR messages)"
      usage: "Access via get_llm_backend_manager() and get_noedit_backend_manager() for different use cases"
      provider_manager: "BackendProviderManager is shared across backend manager instances and tracks provider rotation state"
      deprecated_names: "get_message_backend_manager(), run_llm_message_prompt(), get_message_backend_and_model() are deprecated - use get_noedit_backend_manager(), run_llm_noedit_prompt(), get_noedit_backend_and_model() instead"
  session_resume:
      description: "Persist and reuse backend session identifiers so supported backends can resume previous conversations."
      prerequisites:
        - "Backend configuration includes options_for_resume with a [sessionId] placeholder that matches the backend CLI flag"
      behavior:
        - "Captures session IDs from backend clients after each execution and persists them to ~/.auto-coder/backend_session_state.json"
        - "Automatically injects configured resume options when the same backend runs consecutively and a session ID is available"
        - "Clears persisted session data when rotating to a different backend to avoid cross-backend leakage"
      configuration_file: "~/.auto-coder/llm_config.toml"
      persistence_file: "~/.auto-coder/backend_session_state.json"

  retry_configuration:
      description: "Configurable retry mechanism for LLM backends when usage limits are hit"
      fields:
        - name: "usage_limit_retry_count"
          type: "int"
          default: 0
          purpose: "Number of retry attempts before switching to next backend"
        - name: "usage_limit_retry_wait_seconds"
          type: "int"
          default: 0
          purpose: "Seconds to wait between retry attempts"
      behavior:
        - "When AutoCoderUsageLimitError is caught, backend manager checks retry configuration"
        - "Retries the same backend up to configured count with specified wait time between attempts"
        - "After retries are exhausted, rotates to next available backend"
        - "Default behavior (0 retries, 0 wait) maintains backward compatibility with immediate rotation"
      example:
        toml_example: |
          [backends.gemini]
          model = "gemini-2.5-pro"
          usage_limit_retry_count = 3
          usage_limit_retry_wait_seconds = 30
      configuration_file: "~/.auto-coder/llm_config.toml"
      scope: "Per-backend configuration allows different backends to have different retry policies"
  post_execution_rotation:
      description: "Optional round-robin style rotation after every successful backend call"
      fields:
        - name: "always_switch_after_execution"
          type: "bool"
          default: false
          purpose: "If true, immediately switch to the next backend in backend.order after a successful execution"
      use_cases:
        - "Distribute traffic evenly across providers"
        - "Avoid soft per-backend limits by rotating proactively"
        - "Comply with backends that only permit a single execution per window"
      example:
        toml_example: |
          [backend]
          order = ["antigravity", "qwen", "claude"]

          [backends.antigravity]
          model = "gemini-2.5-pro"
          always_switch_after_execution = true

          [backends.qwen]
          model = "qwen3-coder-plus"
          always_switch_after_execution = true
      configuration_file: "~/.auto-coder/llm_config.toml"
      scope: "Per-backend flag; defaults to false when omitted"
