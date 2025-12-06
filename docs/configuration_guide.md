# Configuration File Format Guide

This guide explains the configuration file format for Auto-Coder, including the placeholder replacement system and configuration-based options.

## Table of Contents

- [Overview](#overview)
- [Configuration File Location](#configuration-file-location)
- [Placeholder System](#placeholder-system)
- [Configuration Structure](#configuration-structure)
- [Backend-Specific Documentation](#backend-specific-documentation)
- [Migration Guide](#migration-guide)
- [Troubleshooting](#troubleshooting)

## Overview

Auto-Coder uses a TOML configuration file (`llm_config.toml`) to manage LLM backend settings. The configuration system supports:

- **Dynamic placeholders** for runtime value replacement
- **Configuration-based CLI options** instead of hardcoded defaults
- **Multiple backend configurations** with inheritance support
- **Separate configurations** for editing vs. message generation operations

## Configuration File Location

Auto-Coder searches for configuration files in the following priority order:

1. **Local configuration** (highest priority): `.auto-coder/llm_config.toml` in current directory
2. **Home configuration**: `~/.auto-coder/llm_config.toml` in home directory
3. **Default configuration**: Auto-generated if no configuration file exists

### When to Use Local Configuration

Use local configuration (`.auto-coder/llm_config.toml`) when:
- You need project-specific LLM backend settings
- You want to version-control your configuration with your project
- You're working in a team and want to share configuration
- You're testing different configurations without affecting global settings
- You're working in container/CI environments where configuration should be part of the project

### When to Use Home Configuration

Use home configuration (`~/.auto-coder/llm_config.toml`) when:
- You want the same configuration across all projects
- You're working on personal projects with consistent settings
- You want to keep API keys and credentials out of project repositories

## Placeholder System

The placeholder system enables dynamic value replacement in configuration options at runtime.

### Supported Placeholders

| Placeholder | Replaced With | Use Case |
|------------|--------------|----------|
| `[model_name]` | Backend's model value | Dynamic model selection in CLI commands |
| `[sessionId]` | Session ID from previous execution | Session resumption for stateful backends |
| `[settings]` | Backend's settings file path | Claude settings file configuration |
| `[model_provider]` | Backend's model_provider value | Provider selection for OpenAI-compatible APIs |

### How Placeholders Work

1. **Configuration Time**: Placeholders are defined in `llm_config.toml` as part of option lists
2. **Runtime**: The `BackendConfig.replace_placeholders()` method processes options before execution
3. **Replacement**: Placeholders are replaced with actual values provided at runtime
4. **Preservation**: If a placeholder value is not provided, the placeholder remains unchanged

### Example: Basic Placeholder Usage

```toml
[backends.codex]
model = "gpt-5.1-codex-max"
options = ["--model", "[model_name]", "--json"]
```

At runtime, `[model_name]` is replaced with `"gpt-5.1-codex-max"`, resulting in:
```
codex --model gpt-5.1-codex-max --json
```

### Example: Multiple Placeholders

```toml
[backends.custom-backend]
model = "some/model:free"
backend_type = "codex"
model_provider = "openrouter"
openai_api_key = "sk-..."
options = ["--model", "[model_name]", "-c", "model_provider=[model_provider]"]
```

Results in:
```
codex --model some/model:free -c model_provider=openrouter
```

### Example: Session Resume

```toml
[backends.claude]
model = "sonnet"
backend_type = "claude"
options_for_resume = ["--resume", "[sessionId]"]
```

When resuming a session with ID `"abc123"`:
```
claude --resume abc123
```

## Configuration Structure

### Main Configuration Sections

#### 1. Backend Order and Defaults

```toml
[backend]
default = "codex"
order = ["codex", "gemini", "qwen"]
```

- **`default`**: Primary backend to use
- **`order`**: Fallback order when primary backend fails

#### 2. Backend for Non-Edit Operations

```toml
[backend_for_noedit]
default = "claude"
order = ["claude", "gemini"]
```

Separate configuration for message generation (commit messages, PR descriptions, etc.)

#### 3. Individual Backend Configuration

```toml
[backends.codex]
enabled = true
model = "gpt-5.1-codex-max"
backend_type = "codex"
options = ["--model", "[model_name]", "--json"]
options_for_noedit = ["--model", "[model_name]", "--json"]
options_for_resume = []
```

### Option Fields Explained

#### `options`

Used for **code editing operations** (default LLM operations):
- Issue analysis and implementation
- Bug fixes
- Code generation
- Refactoring

```toml
options = ["--model", "[model_name]", "--json", "--dangerously-bypass-approvals-and-sandbox"]
```

#### `options_for_noedit`

Used for **message generation operations**:
- Commit messages
- PR descriptions
- Review comments
- Non-code text generation

```toml
options_for_noedit = ["--model", "[model_name]", "--json"]
```

**Fallback behavior**: If `options_for_noedit` is not specified, it falls back to using `options`.

#### `options_for_resume`

Used for **session resumption**:
- Resuming previous conversations
- Maintaining context across operations

```toml
options_for_resume = ["--resume", "[sessionId]"]
```

### Complete Configuration Example

```toml
[backend]
default = "codex"
order = ["codex", "gemini", "claude"]

[backend_for_noedit]
default = "claude"
order = ["claude", "gemini"]

[backends.codex]
model = "gpt-5.1-codex-max"
backend_type = "codex"
options = ["--model", "[model_name]", "--json", "--dangerously-bypass-approvals-and-sandbox"]
options_for_noedit = ["--model", "[model_name]", "--json"]

[backends.gemini]
model = "gemini-2.5-pro"
backend_type = "gemini"
options = ["--model", "[model_name]", "--yolo", "--prompt"]
options_for_noedit = ["--model", "[model_name]", "--yolo", "--prompt"]

[backends.claude]
model = "sonnet"
backend_type = "claude"
settings = "/path/to/settings.json"
options = ["--print", "--model", "[model_name]", "--settings", "[settings]"]
options_for_noedit = ["--print", "--model", "[model_name]", "--settings", "[settings]"]
options_for_resume = ["--resume", "[sessionId]"]
```

## Backend-Specific Documentation

### Codex Backend

**Backend Type**: `codex`

**Use Case**: OpenAI Codex and OpenAI-compatible APIs (OpenRouter, Azure OpenAI, custom endpoints)

**Required Placeholders**: None (but `[model_name]` recommended)

**Example Configuration**:

```toml
[backends.codex]
model = "gpt-5.1-codex-max"
backend_type = "codex"
options = ["--model", "[model_name]", "--json", "--dangerously-bypass-approvals-and-sandbox"]
options_for_noedit = ["--model", "[model_name]", "--json"]
```

**Notes**:
- Removed `exec` subcommand (was: `codex exec [options]`, now: `codex [options]`)
- All CLI options must be specified in configuration
- Supports `[model_name]` and `[model_provider]` placeholders

### Codex with OpenRouter

**Example Configuration**:

```toml
[backends.openrouter-backend]
model = "some/model:free"
backend_type = "codex"
model_provider = "openrouter"
openai_api_key = "sk-..."
options = ["--model", "[model_name]", "-c", "model_provider=[model_provider]"]
```

### Gemini Backend

**Backend Type**: `gemini`

**Use Case**: Google Gemini API

**Required Placeholders**: None (but `[model_name]` recommended)

**Example Configuration**:

```toml
[backends.gemini]
model = "gemini-2.5-pro"
backend_type = "gemini"
options = ["--model", "[model_name]", "--yolo", "--prompt"]
options_for_noedit = ["--model", "[model_name]", "--yolo", "--prompt"]
```

**Important Notes**:
- **`--prompt` flag must be the last option** before the prompt text
- Supports automatic `@` escaping to `\@` in prompts for Gemini CLI compatibility

### Claude Backend

**Backend Type**: `claude`

**Use Case**: Anthropic Claude API

**Required Placeholders**: None (but `[model_name]` and `[settings]` recommended)

**Example Configuration**:

```toml
[backends.claude]
model = "sonnet"
backend_type = "claude"
settings = "/path/to/settings.json"
options = ["--print", "--model", "[model_name]", "--settings", "[settings]"]
options_for_noedit = ["--print", "--model", "[model_name]", "--settings", "[settings]"]
options_for_resume = ["--resume", "[sessionId]"]
```

**Session Resume Support**:
Claude supports session resumption using the `[sessionId]` placeholder:

```toml
options_for_resume = ["--resume", "[sessionId]"]
```

### Qwen Backend

**Backend Type**: `qwen`

**Use Case**: Native Qwen CLI with OAuth authentication

**Required Placeholders**: None (but `[model_name]` recommended)

**Example Configuration**:

```toml
[backends.qwen]
model = "qwen3-coder-plus"
backend_type = "qwen"
options = ["--model", "[model_name]", "-y"]
options_for_noedit = ["--model", "[model_name]", "-y"]
```

**Notes**:
- Uses OAuth authentication (no API keys required)
- Requires Qwen CLI to be installed and authenticated

### Auggie Backend

**Backend Type**: `auggie`

**Use Case**: AugmentCode Auggie CLI

**Required Placeholders**: None (but `[model_name]` recommended)

**Example Configuration**:

```toml
[backends.auggie]
model = "GPT-5"
backend_type = "auggie"
options = ["--print", "--model", "[model_name]"]
options_for_noedit = ["--print", "--model", "[model_name]"]
```

## Required Options by Backend

Each backend has specific required options that **must** be included in the `options` list:

| Backend | Required Options | Auto-Added by System |
|---------|-----------------|---------------------|
| `codex` | `--dangerously-bypass-approvals-and-sandbox` | Yes |
| `claude` | `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions` | Yes |
| `gemini` | `--yolo` | Yes |
| `auggie` | `--print` | Yes |
| `qwen` | `-y` | Yes |
| `jules` | None | N/A |
| `codex-mcp` | None | N/A |

**Note**: The system automatically adds these required options during execution. You only need to specify additional custom options.

## Migration Guide

### Migrating from Hardcoded Options

**Before** (hardcoded in code):
```python
# CodexClient hardcoded: codex exec --model codex --json
# GeminiClient hardcoded: gemini --model gemini-2.5-pro --yolo --prompt
```

**After** (configuration-based):
```toml
[backends.codex]
model = "codex"
options = ["--model", "[model_name]", "--json", "--dangerously-bypass-approvals-and-sandbox"]

[backends.gemini]
model = "gemini-2.5-pro"
options = ["--model", "[model_name]", "--yolo", "--prompt"]
```

### Migration Steps

1. **Identify Current Hardcoded Options**
   - Review how your backend is currently being called
   - Note any CLI flags that were hardcoded

2. **Create Configuration Entries**
   - Add backend configuration to `llm_config.toml`
   - Include all previously hardcoded options
   - Add placeholders where appropriate

3. **Test Configuration**
   ```bash
   auto-coder config validate
   auto-coder process-issues --repo owner/repo --only 123
   ```

4. **Update for Required Options**
   - Ensure required options are present (system auto-adds them, but explicit is better)
   - Validate with `auto-coder config validate`

### Common Migration Patterns

#### Pattern 1: CodexClient `exec` Removal

**Old**:
```
codex exec --model codex --json <prompt>
```

**New**:
```toml
[backends.codex]
model = "codex"
options = ["--model", "[model_name]", "--json", "--dangerously-bypass-approvals-and-sandbox"]
```

#### Pattern 2: Gemini `--prompt` Flag

**Old** (hardcoded):
```
gemini --model gemini-2.5-pro --yolo --prompt <prompt>
```

**New** (configuration):
```toml
[backends.gemini]
model = "gemini-2.5-pro"
options = ["--model", "[model_name]", "--yolo", "--prompt"]
```

**Important**: `--prompt` must be the last option!

#### Pattern 3: Claude Settings File

**Old** (hardcoded path):
```
claude --print --model sonnet --settings /path/to/settings.json
```

**New** (configuration with placeholder):
```toml
[backends.claude]
model = "sonnet"
settings = "/path/to/settings.json"
options = ["--print", "--model", "[model_name]", "--settings", "[settings]"]
```

## Troubleshooting

### Missing Required Options

**Error**:
```
Backend 'codex' missing required option: --dangerously-bypass-approvals-and-sandbox
```

**Solution**:
Add the required option to your configuration:
```toml
[backends.codex]
options = ["--model", "[model_name]", "--json", "--dangerously-bypass-approvals-and-sandbox"]
```

### Placeholder Not Replaced

**Problem**: Placeholder like `[model_name]` appears literally in CLI command

**Possible Causes**:
1. Model name not set in configuration
2. Placeholder syntax incorrect (must be exact: `[model_name]`, not `[modelName]` or `{model_name}`)

**Solution**:
```toml
[backends.codex]
model = "gpt-5.1-codex-max"  # Ensure model is set
options = ["--model", "[model_name]"]  # Correct placeholder syntax
```

### Gemini `--prompt` Flag Position

**Problem**: Gemini CLI fails with option ordering errors

**Cause**: `--prompt` flag must be the **last option** before the prompt text

**Solution**:
```toml
[backends.gemini]
options = ["--model", "[model_name]", "--yolo", "--prompt"]  # --prompt is last
```

### Options Not Being Used

**Problem**: Configuration options are ignored

**Possible Causes**:
1. `options_for_noedit` is set but operation is code editing (should use `options`)
2. Backend type mismatch
3. Configuration file not found

**Solution**:
1. Check operation type and use correct option field
2. Verify `backend_type` matches backend name or is explicitly set
3. Use `auto-coder config show` to verify loaded configuration

### Configuration File Not Found

**Problem**: Auto-Coder uses default configuration instead of your file

**Solution**:
1. Check file location priority:
   - `.auto-coder/llm_config.toml` (current directory)
   - `~/.auto-coder/llm_config.toml` (home directory)
2. Verify file name is exactly `llm_config.toml`
3. Use `auto-coder config show` to see loaded configuration path

### Debugging Configuration Issues

```bash
# Show current configuration
auto-coder config show

# Validate configuration
auto-coder config validate

# Test with specific backend
auto-coder process-issues --repo owner/repo --only 123 --backend codex

# Enable debug logging
export LOG_LEVEL=DEBUG
auto-coder process-issues --repo owner/repo --only 123
```

## Advanced Topics

### Configuration Inheritance

Backends can inherit options from parent configurations:

```toml
[backends.openrouter-base]
backend_type = "codex"
model = "qwen/qwen-2.5-plus"
openai_api_key = "sk-or-v1-..."
openai_base_url = "https://openrouter.ai/api/v1"
options = ["-o", "HTTPReferer", "https://yourapp.com"]

[backends.qwen-fast]
model = "qwen/qwen-2.5-plus"
backend_type = "openrouter-base"  # Inherits options from openrouter-base
```

### Environment Variable Overrides

Configuration values can be overridden with environment variables:

```bash
export AUTO_CODER_CODEX_MODEL="custom-model"
export AUTO_CODER_CODEX_OPENAI_API_KEY="sk-..."
```

### Custom Placeholders

To add custom placeholders, modify the `BackendConfig.replace_placeholders()` method to support additional placeholder types.

## See Also

- [Main README](../README.md) - General Auto-Coder documentation
- [Client Features Documentation](client-features.yaml) - Complete feature reference
- [Example Configuration File](llm_backend_config.example.toml) - Complete example configuration
- [Migration Guide for CLI Options](MIGRATION_CLI_OPTIONS.md) - Detailed migration instructions
