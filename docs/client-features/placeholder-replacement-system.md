# Placeholder Replacement System

- **Purpose**: Enable dynamic configuration values in option lists using placeholders
- **Documentation**: See [Configuration Guide](configuration_guide.md) for complete documentation
- **Supported Placeholders**:
  - `[model_name]` - Replaced with the backend's model value
  - `[sessionId]` - Replaced with session ID for resume functionality
  - `[settings]` - Replaced with backend's settings file path
  - `[model_provider]` - Replaced with backend's model_provider value
- **Implementation**: The `BackendConfig.replace_placeholders()` method processes all three option lists (`options`, `options_for_noedit`, `options_for_resume`)
- **Behavior**:
  - Placeholders are replaced with provided values at runtime
  - If a placeholder value is not provided, the placeholder remains unchanged
  - The method returns a new dictionary with processed lists, leaving the original configuration unmodified
  - Multiple occurrences of the same placeholder in a list are all replaced
- **Example Configuration**:
  ```toml
  [backends.codex]
  model = "gpt-5.1-codex-max"
  options = ["--model", "[model_name]", "--json", "--dangerously-bypass-approvals-and-sandbox"]
  options_for_noedit = ["--model", "[model_name]", "--json"]
  options_for_resume = ["--model", "[model_name]"]

  [backends.custom-claude]
  model = "opus"
  backend_type = "claude"
  settings = "/path/to/settings.json"
  options = ["--print", "--model", "[model_name]", "--settings", "[settings]"]

  [backends.openrouter-backend]
  model = "some/model:free"
  backend_type = "codex"
  model_provider = "openrouter"
  options = ["--model", "[model_name]", "-c", "model_provider=[model_provider]"]
  ```
- **Usage in Code**:
  ```python
  config = BackendConfig(name="codex", ...)
  processed = config.replace_placeholders(
      model_name="gpt-5.1-codex-max",
      session_id="sess_20241204",
      settings="/path/to/settings.json"
  )
  # processed['options'] = ["--model", "gpt-5.1-codex-max", "--json", "--dangerously-bypass-approvals-and-sandbox"]
  ```
- **Benefits**:
  - Single configuration template can be used with different models
  - Session-based backends can dynamically inject session IDs
  - Settings files can be configured without hardcoding paths in options
  - Model providers can be specified dynamically for OpenAI-compatible backends
  - Reduces configuration duplication
  - Supports dynamic runtime values without modifying configuration files
- **Backward Compatibility**: Fully backward compatible - configurations without placeholders work unchanged
