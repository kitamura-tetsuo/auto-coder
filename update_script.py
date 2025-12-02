#!/usr/bin/env python3
import re

# Read the file
with open("src/auto_coder/codex_client.py", "r") as f:
    content = f.read()

# 1. Add use_noedit_options parameter to __init__
old = """    def __init__(
        self,
        backend_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
    ) -> None:"""
new = """    def __init__(
        self,
        backend_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        use_noedit_options: bool = False,
    ) -> None:"""
content = content.replace(old, new)

# 2. Update docstring
old_doc = """        \"\"\"Initialize Codex CLI client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
                         If provided, will use config for this backend.
            api_key: API key for the backend (optional, for custom backends).
            base_url: Base URL for the backend (optional, for custom backends).
            openai_api_key: OpenAI API key (optional, for OpenAI-compatible backends).
            openai_base_url: OpenAI base URL (optional, for OpenAI-compatible backends).
        \"\"\""""
new_doc = """        \"\"\"Initialize Codex CLI client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
                         If provided, will use config for this backend.
            api_key: API key for the backend (optional, for custom backends).
            base_url: Base URL for the backend (optional, for custom backends).
            openai_api_key: OpenAI API key (optional, for OpenAI-compatible backends).
            openai_base_url: OpenAI base URL (optional, for OpenAI-compatible backends).
            use_noedit_options: If True, use options_for_noedit from config instead of options.
        \"\"\""""
content = content.replace(old_doc, new_doc)

# 3. Update options assignment for if branch
old_if = """        if backend_name:
            config_backend = config.get_backend_config(backend_name)
            # Use backend config model, fall back to default \"codex\"
            self.model_name = (config_backend and config_backend.model) or \"codex\"
            self.options = (config_backend and config_backend.options) or []"""
new_if = """        if backend_name:
            config_backend = config.get_backend_config(backend_name)
            # Use backend config model, fall back to default \"codex\"
            self.model_name = (config_backend and config_backend.model) or \"codex\"
            # Use options_for_noedit if requested, otherwise use options
            if use_noedit_options:
                self.options = (config_backend and config_backend.options_for_noedit) or []
            else:
                self.options = (config_backend and config_backend.options) or []"""
content = content.replace(old_if, new_if)

# 4. Update options assignment for else branch
old_else = """        else:
            # Fall back to default codex config
            config_backend = config.get_backend_config(\"codex\")
            self.model_name = (config_backend and config_backend.model) or \"codex\"
            self.options = (config_backend and config_backend.options) or []"""
new_else = """        else:
            # Fall back to default codex config
            config_backend = config.get_backend_config(\"codex\")
            self.model_name = (config_backend and config_backend.model) or \"codex\"
            # Use options_for_noedit if requested, otherwise use options
            if use_noedit_options:
                self.options = (config_backend and config_backend.options_for_noedit) or []
            else:
                self.options = (config_backend and config_backend.options) or []"""
content = content.replace(old_else, new_else)

# Write back the file
with open("src/auto_coder/codex_client.py", "w") as f:
    f.write(content)

print("Successfully updated codex_client.py")
