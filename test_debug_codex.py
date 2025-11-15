#!/usr/bin/env python3
"""
Simple test to debug the CodexClient model name issue.
"""

import os
import sys
from pathlib import Path

# Add the src directory to the path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from auto_coder.codex_client import CodexClient
from auto_coder.llm_backend_config import LLMBackendConfiguration, BackendConfig


def debug_codex_client() -> None:
    """Debug the CodexClient model name issue."""
    print("Debugging CodexClient model name...")
    
    # Create a temporary config file for testing
    temp_config_path = Path("/tmp/test_debug_config.toml")
    
    # Create a sample configuration
    config = LLMBackendConfiguration()
    config.default_backend = "codex"
    config.backend_order = ["codex"]
    
    # Add backend configuration
    config.backends["codex"] = BackendConfig(name="codex", enabled=True, model="codex-test")
    
    # Save temporarily
    config.save_to_file(str(temp_config_path))
    
    # Temporarily override the global config to use our test config
    original_config = None
    try:
        from auto_coder.llm_backend_config import get_llm_config, reset_llm_config
        reset_llm_config()  # Reset to force reload
        import auto_coder.llm_backend_config as config_module
        original_config = config_module._llm_config
        config_module._llm_config = config  # Inject our test config
        
        # Test Codex client initialization
        print("Testing CodexClient...")
        codex_client = CodexClient()
        print(f"  CodexClient model: '{codex_client.model_name}'")
        print(f"  Expected 'codex-test'")
        
        # Check the config directly
        backend_config = config.get_backend_config("codex")
        print(f"  Config model: '{backend_config.model if backend_config else 'None'}'")
        
    finally:
        # Restore original config
        if original_config is not None:
            config_module._llm_config = original_config
    
    print("Debug completed!")


if __name__ == "__main__":
    debug_codex_client()