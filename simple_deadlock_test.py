#!/usr/bin/env python3
"""
Simple test to check if the deadlock is fixed.
"""

import sys
import threading
import time
from pathlib import Path

# Add the src directory to the path
sys.path.insert(0, str(Path(__file__).parent / "src"))

print("Testing basic GitHubClient import and singleton...")

try:
    from auto_coder.github_client import GitHubClient

    print("✓ Successfully imported GitHubClient")
except ImportError as e:
    print(f"✗ Failed to import GitHubClient: {e}")
    sys.exit(1)

# Test basic singleton functionality
print("Testing basic singleton behavior...")

# Reset singleton to clean state
GitHubClient.reset_singleton()
print("✓ Reset singleton")

# Test single instance creation
try:
    client1 = GitHubClient.get_instance("test_token", False)
    print("✓ Created first instance")
except Exception as e:
    print(f"✗ Failed to create first instance: {e}")
    sys.exit(1)

# Test second call with different parameters
try:
    client2 = GitHubClient.get_instance("different_token", True)
    print("✓ Created second instance")
except Exception as e:
    print(f"✗ Failed to create second instance: {e}")
    sys.exit(1)

# Verify singleton behavior
if client1 is client2:
    print("✓ Singleton behavior confirmed - both calls returned same instance")
else:
    print("✗ Singleton behavior broken - different instances returned")
    sys.exit(1)

# Check if first parameters were used
if client1.token == "test_token" and client1.disable_labels == False:
    print("✓ First parameters were preserved correctly")
else:
    print(f"✗ First parameters not preserved: token={client1.token}, disable_labels={client1.disable_labels}")
    sys.exit(1)

print("\nBasic functionality test passed!")
print("Now testing concurrent access with 3 threads...")


def worker(worker_id):
    print(f"Worker {worker_id}: Starting...")
    try:
        client = GitHubClient.get_instance(f"worker_{worker_id}_token", worker_id % 2 == 0)
        print(f"Worker {worker_id}: Got instance successfully")
        return True
    except Exception as e:
        print(f"Worker {worker_id}: Failed with error: {e}")
        return False


# Test with just 3 threads to keep it simple
threads = []
results = []

for i in range(3):

    def make_worker(worker_id):
        return lambda: worker(worker_id)

    thread = threading.Thread(target=make_worker(i))
    threads.append(thread)
    thread.start()
    print(f"Started worker {i}")

print("Waiting for threads to complete...")

# Wait with timeout
for i, thread in enumerate(threads):
    thread.join(timeout=10)  # 10 second timeout per thread
    if thread.is_alive():
        print(f"Worker {i} timed out - possible deadlock!")
        break
else:
    print("All threads completed successfully!")
    print("✅ No deadlock detected!")

print("\nTest completed!")
