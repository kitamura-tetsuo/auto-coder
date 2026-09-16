# Backend Configuration

The auto-coder system supports multiple LLM backends:

```toml
          [qwen.qwen-direct]
          command = "uvx"
          args = ["qwen-direct"]
          description = "Direct Qwen API access"
          QWEN_API_KEY = "your-api-key"
```
