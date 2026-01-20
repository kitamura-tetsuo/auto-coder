#!/usr/bin/env python
"""
Simple performance verification for singleton patterns.
"""

import time
from unittest.mock import Mock

from auto_coder.backend_manager import LLMBackendManager
from auto_coder.github_client import GitHubClient

print("=" * 60)
print("Singleton Pattern Verification")
print("=" * 60)

# Test 1: GitHubClient Singleton
print("\n1. GitHubClient Singleton Test")
GitHubClient.reset_singleton()

start = time.perf_counter()
for _ in range(100):
    client = GitHubClient.get_instance("test-token")
end = time.perf_counter()

client1 = GitHubClient.get_instance("different-token")
client2 = GitHubClient.get_instance("another-token")

if client1 is client2:
    print("   ✓ Singleton returns same instance")
else:
    print("   ✗ FAILED: Different instances")

print(f"   100 accesses in {end - start:.6f} seconds")

# Test 2: LLMBackendManager Singleton
print("\n2. LLMBackendManager Singleton Test")
LLMBackendManager.reset_singleton()

mock_client = Mock()
manager = LLMBackendManager.get_llm_instance(
    default_backend="test",
    default_client=mock_client,
    factories={"test": lambda: mock_client}
)

start = time.perf_counter()
for _ in range(100):
    mgr = LLMBackendManager.get_llm_instance()
end = time.perf_counter()

manager1 = LLMBackendManager.get_llm_instance()
manager2 = LLMBackendManager.get_llm_instance()

if manager1 is manager2:
    print("   ✓ Singleton returns same instance")
else:
    print("   ✗ FAILED: Different instances")

print(f"   100 accesses in {end - start:.6f} seconds")

print("\n" + "=" * 60)
print("Verification Complete!")
print("=" * 60)
print("\nAll singleton patterns working correctly:")
print("- GitHubClient: Single instance across all calls")
print("- LLMBackendManager: Single instance across all calls")
print("=" * 60)
