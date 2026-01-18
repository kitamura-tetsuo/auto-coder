#!/usr/bin/env python3
"""
Test script to verify that GitHubClient deadlock issue is fixed.
Tests concurrent access to GitHubClient.get_instance() from multiple threads.
"""

import threading
import time
import sys
from pathlib import Path

# Add the src directory to the path so we can import auto_coder modules
sys.path.insert(0, str(Path(__file__).parent / "src"))

from auto_coder.github_client import GitHubClient

class DeadlockTestResult:
    def __init__(self):
        self.success_count = 0
        self.failure_count = 0
        self.errors = []
        self.lock = threading.Lock()
        
    def record_success(self):
        with self.lock:
            self.success_count += 1
            
    def record_failure(self, error):
        with self.lock:
            self.failure_count += 1
            self.errors.append(str(error))

def test_worker(worker_id, result_collector, token="test_token", disable_labels=False):
    """Worker function to test concurrent GitHubClient access."""
    try:
        # Simulate some work time
        time.sleep(0.01 * (worker_id % 3))
        
        # Get instance - this should not deadlock
        client = GitHubClient.get_instance(token, disable_labels)
        
        # Verify the client is properly initialized
        if not hasattr(client, 'github') or not hasattr(client, 'token'):
            raise Exception(f"Worker {worker_id}: Client not properly initialized")
            
        result_collector.record_success()
        print(f"Worker {worker_id}: Successfully got GitHubClient instance")
        
    except Exception as e:
        result_collector.record_failure(e)
        print(f"Worker {worker_id}: Failed with error: {e}")

def test_get_instance_calls(worker_id, result_collector):
    """Test multiple get_instance calls from the same worker."""
    try:
        # Test multiple calls to get_instance
        for i in range(5):
            # Test both with and without parameters
            if i % 2 == 0:
                client = GitHubClient.get_instance("test_token", False)
            else:
                client = GitHubClient.get_instance()
                
            # Verify all calls return the same instance
            if i == 0:
                first_instance = client
            elif client is not first_instance:
                raise Exception(f"Worker {worker_id}: get_instance returned different instances on call {i}")
                
        result_collector.record_success()
        print(f"Worker {worker_id}: Multiple get_instance calls successful")
        
    except Exception as e:
        result_collector.record_failure(e)
        print(f"Worker {worker_id}: Multiple get_instance calls failed: {e}")

def test_singleton_behavior():
    """Test that GitHubClient behaves as a singleton."""
    print("Testing singleton behavior...")
    
    # Reset the singleton first
    GitHubClient.reset_singleton()
    
    # Get instance with parameters
    client1 = GitHubClient.get_instance("token1", True)
    
    # Get instance with different parameters (should return same instance)
    client2 = GitHubClient.get_instance("token2", False)
    
    # Test that they are the same instance
    if client1 is not client2:
        print("FAIL: get_instance returned different instances")
        return False
        
    # Test that the first parameters were used
    if client1.token != "token1" or client1.disable_labels != True:
        print("FAIL: Singleton instance not using first parameters")
        return False
        
    print("PASS: Singleton behavior works correctly")
    return True

def test_concurrent_access():
    """Test concurrent access from multiple threads."""
    print("\nTesting concurrent access from multiple threads...")
    
    # Reset the singleton for clean test
    GitHubClient.reset_singleton()
    
    # Number of concurrent workers
    num_workers = 10
    result_collector = DeadlockTestResult()
    
    # Create and start threads
    threads = []
    
    # Test 1: Multiple workers getting instances
    print(f"Starting {num_workers} workers to test concurrent get_instance calls...")
    for i in range(num_workers):
        thread = threading.Thread(target=test_worker, args=(i, result_collector))
        threads.append(thread)
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    print(f"Test 1 Results: {result_collector.success_count} successes, {result_collector.failure_count} failures")
    
    if result_collector.failure_count > 0:
        print("Errors encountered:")
        for error in result_collector.errors:
            print(f"  - {error}")
        return False
    
    # Test 2: Multiple calls from same thread
    print("\nTesting multiple get_instance calls from same thread...")
    result_collector2 = DeadlockTestResult()
    threads2 = []
    
    for i in range(5):
        thread = threading.Thread(target=test_get_instance_calls, args=(i, result_collector2))
        threads2.append(thread)
        thread.start()
    
    for thread in threads2:
        thread.join()
    
    print(f"Test 2 Results: {result_collector2.success_count} successes, {result_collector2.failure_count} failures")
    
    if result_collector2.failure_count > 0:
        print("Errors encountered:")
        for error in result_collector2.errors:
            print(f"  - {error}")
        return False
    
    return True

def test_edge_cases():
    """Test edge cases that might cause deadlocks."""
    print("\nTesting edge cases...")
    
    # Reset singleton
    GitHubClient.reset_singleton()
    
    # Test rapid successive calls
    print("Testing rapid successive calls...")
    for i in range(100):
        client = GitHubClient.get_instance(f"token_{i}", i % 2 == 0)
        if i == 0:
            first_client = client
        elif client is not first_client:
            print(f"FAIL: Rapid calls returned different instance at iteration {i}")
            return False
    
    print("PASS: Rapid successive calls work correctly")
    return True

def main():
    """Main test function."""
    print("Testing GitHubClient deadlock fix...")
    print("=" * 50)
    
    try:
        # Test 1: Singleton behavior
        singleton_ok = test_singleton_behavior()
        
        # Test 2: Concurrent access
        concurrent_ok = test_concurrent_access()
        
        # Test 3: Edge cases
        edge_cases_ok = test_edge_cases()
        
        print("\n" + "=" * 50)
        print("Test Summary:")
        print(f"Singleton behavior: {'PASS' if singleton_ok else 'FAIL'}")
        print(f"Concurrent access: {'PASS' if concurrent_ok else 'FAIL'}")
        print(f"Edge cases: {'PASS' if edge_cases_ok else 'FAIL'}")
        
        if singleton_ok and concurrent_ok and edge_cases_ok:
            print("\n✅ All tests passed! Deadlock issue appears to be fixed.")
            return True
        else:
            print("\n❌ Some tests failed. Deadlock issue may not be fully resolved.")
            return False
            
    except Exception as e:
        print(f"\n❌ Test execution failed with exception: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)