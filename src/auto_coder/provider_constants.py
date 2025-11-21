"""
Shared provider configuration constants.
"""

from __future__ import annotations

# Default Codex CLI arguments leveraged when routing through codex CLI.
DEFAULT_CODEX_ARGS: tuple[str, ...] = ("exec", "-s", "workspace-write", "--dangerously-bypass-approvals-and-sandbox")

# Default Qwen CLI arguments leveraged when invoking the official Qwen CLI.
DEFAULT_QWEN_ARGS: tuple[str, ...] = ("-y",)
