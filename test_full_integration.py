#!/usr/bin/env python3
"""
Full integration test to verify the configuration system works with all client types.
"""

import os
import sys
from pathlib import Path

# Add the src directory to the path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from auto_coder.auggie_client import AuggieClient
from auto_coder.claude_client import ClaudeClient
from auto_coder.codex_client import CodexClient
from auto_coder.codex_mcp_client import CodexMCPClient
from auto_coder.gemini_client import GeminiClient
from auto_coder.llm_backend_config import LLMBackendConfiguration, BackendConfig
from auto_coder.qwen_client import QwenClient


def test_full_integration() -> None:
    """Test that all clients can be initialized using configuration."""
    print("Testing full integration of configuration system with all clients...")
    
    # Create a temporary config file for testing
    temp_config_path = Path("/tmp/test_integration_config.toml")
    
    # Create a sample configuration
    config = LLMBackendConfiguration()
    config.default_backend = "gemini"
    config.backend_order = ["gemini", "qwen", "auggie", "claude", "codex", "codex-mcp"]
    
    # Add backend configurations
    config.backends["codex"] = BackendConfig(name="codex", enabled=True, model="codex-test")
    config.backends["codex-mcp"] = BackendConfig(name="codex-mcp", enabled=True, model="codex-mcp-test")
    config.backends["gemini"] = BackendConfig(
        name="gemini",
        enabled=True,
        model="gemini-test-model",
        api_key="test-gemini-key"
    )
    config.backends["qwen"] = BackendConfig(
        name="qwen",
        enabled=True,
        model="qwen-test-model",
        openai_api_key="test-qwen-key",
        openai_base_url="https://test-qwen.com/v1"
    )
    config.backends["auggie"] = BackendConfig(
        name="auggie",
        enabled=True,
        model="auggie-test-model"
    )
    config.backends["claude"] = BackendConfig(
        name="claude",
        enabled=True,
        model="claude-test-model"
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
        
        # Test all clients initialization
        print("Testing CodexClient...")
        codex_client = CodexClient()
        assert codex_client.model_name == "codex-test", f"Expected 'codex-test', got '{codex_client.model_name}'"
        print(f"  CodexClient model: {codex_client.model_name}")
        
        print("Testing CodexMCPClient...")
        codex_mcp_client = CodexMCPClient()
        assert codex_mcp_client.model_name == "codex-mcp-test", f"Expected 'codex-mcp-test', got '{codex_mcp_client.model_name}'"
        print(f"  CodexMCPClient model: {codex_mcp_client.model_name}")
        
        print("Testing GeminiClient...")
        gemini_client = GeminiClient()
        assert gemini_client.model_name == "gemini-test-model", f"Expected 'gemini-test-model', got '{gemini_client.model_name}'"
        print(f"  GeminiClient model: {gemini_client.model_name}")
        
        print("Testing QwenClient...")
        qwen_client = QwenClient()
        assert qwen_client.model_name == "qwen-test-model", f"Expected 'qwen-test-model', got '{qwen_client.model_name}'"
        print(f"  QwenClient model: {qwen_client.model_name}")
        
        print("Testing AuggieClient...")
        auggie_client = AuggieClient()
        assert auggie_client.model_name == "auggie-test-model", f"Expected 'auggie-test-model', got '{auggie_client.model_name}'"
        print(f"  AuggieClient model: {auggie_client.model_name}")
        
        print("Testing ClaudeClient...")
        claude_client = ClaudeClient()
        assert claude_client.model_name == "claude-test-model", f"Expected 'claude-test-model', got '{claude_client.model_name}'"
        print(f"  ClaudeClient model: {claude_client.model_name}")
        
        print("All clients initialized successfully with configuration values!")
        
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
    
    print("Full integration test completed!")


if __name__ == "__main__":
    test_full_integration()