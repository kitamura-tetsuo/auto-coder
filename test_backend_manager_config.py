#!/usr/bin/env python3
"""
Simple test to verify the backend manager can be initialized from configuration.
"""

import os
import sys
from pathlib import Path

# Add the src directory to the path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from auto_coder.cli_helpers import build_backend_manager_from_config
from auto_coder.llm_backend_config import LLMBackendConfiguration


def test_backend_manager_from_config() -> None:
    """Test that the backend manager can be built from configuration."""
    print("Testing backend manager initialization from configuration...")
    
    # Create a temporary config file for testing
    temp_config_path = Path("/tmp/test_llm_config_full.toml")
    
    # Create a sample configuration
    config = LLMBackendConfiguration()
    config.default_backend = "codex"
    config.backend_order = ["codex", "gemini", "qwen"]
    
    # Add backend configurations
    from auto_coder.llm_backend_config import BackendConfig
    config.backends["codex"] = BackendConfig(name="codex", enabled=True, model="codex")
    config.backends["gemini"] = BackendConfig(
        name="gemini",
        enabled=True,
        model="gemini-2.5-pro",
        api_key="test-gemini-key"
    )
    config.backends["qwen"] = BackendConfig(
        name="qwen",
        enabled=True,
        model="qwen3-coder-plus",
        openai_api_key="test-qwen-key",
        openai_base_url="https://test-qwen.com/v1"
    )
    
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
        
        # Now try to build a backend manager from the configuration
        # This should work without errors, though client creation might fail due to missing CLI tools
        try:
            backend_manager = build_backend_manager_from_config()
            print(f"Successfully built backend manager with default: {backend_manager._default_backend}")
            print(f"Backend order: {backend_manager._all_backends}")
        except RuntimeError as e:
            if "CLI not available" in str(e):
                print(f"Expected error due to missing CLI tools: {e}")
                print("This is expected in a test environment without the actual CLIs installed.")
            else:
                raise
        except Exception as e:
            print(f"Unexpected error: {e}")
            raise
            
    finally:
        # Restore original config
        if original_config is not None:
            config_module._llm_config = original_config
    
    print("Test completed successfully!")


if __name__ == "__main__":
    test_backend_manager_from_config()