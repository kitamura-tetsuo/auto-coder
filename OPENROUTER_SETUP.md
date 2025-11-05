# OpenRouter Setup Guide

## Overview
Configuration has been completed to use Qwen3 Coder via OpenRouter with the Codex CLI.

## Configuration Details

### 1. Environment Variables
The following environment variables have been added to `~/.bashrc`:

```bash
export OPENAI_API_KEY="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"
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export OPENAI_MODEL="qwen/qwen3-coder:free"
```

### 2. Codex Configuration File (~/.codex/config.toml)
The following configuration has been added:

```toml
model = "qwen/qwen3-coder:free"
model_provider = "openrouter"

# OpenRouter configuration
[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENAI_API_KEY"
```

## Usage

### Enable Environment Variables in Current Session
```bash
source ~/.bashrc
```

Or

```bash
export OPENAI_API_KEY="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"
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export OPENAI_MODEL="qwen/qwen3-coder:free"
```

### Running Codex CLI
With the configuration complete, you can now use Codex normally:

```bash
codex "Hello, how are you?"
```

Or

```bash
codex exec "Write a Python function to calculate fibonacci numbers"
```

## Verifying Configuration

### Verify Environment Variables
```bash
env | grep OPENAI
```

### Verify Codex Configuration
```bash
cat ~/.codex/config.toml
```

### Verify Codex Version
```bash
codex --version
```

## Backup
The original configuration file has been backed up at:
- `~/.codex/config.toml.backup`

## Troubleshooting

### If Configuration Is Not Applied
1. Open a new terminal session
2. Or run `source ~/.bashrc`

### To Change Model Provider
Edit the following line in `~/.codex/config.toml`:
```toml
model_provider = "openrouter"  # Can be changed to other providers
```

### To Use Different Model
Edit the following line in `~/.codex/config.toml`:
```toml
model = "qwen/qwen3-coder:free"  # Can be changed to other OpenRouter models
```

## Reference Links
- [Codex CLI Configuration Documentation](https://developers.openai.com/codex/local-config/)
- [OpenRouter Models](https://openrouter.ai/models)
- [Codex GitHub Repository](https://github.com/openai/codex)

