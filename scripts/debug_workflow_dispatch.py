import logging
import os
import sys

from dotenv import load_dotenv

# Add src to path to ensure we can import auto_coder modules if not installed
sys.path.append(os.path.join(os.getcwd(), "src"))

from auto_coder.util.gh_cache import GitHubClient
from auto_coder.util.github_action import trigger_workflow_dispatch

# Setup logging
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv("/home/node/src/auto-coder/.env")  # Try loading from standard location if present

import subprocess

token = os.environ.get("GITHUB_TOKEN")
if not token:
    try:
        token = subprocess.check_output(["gh", "auth", "token"]).decode("utf-8").strip()
        print("Fetched GITHUB_TOKEN from gh CLI")
    except Exception as e:
        print(f"Failed to fetch token from gh CLI: {e}")

if not token:
    print("GITHUB_TOKEN not found in environment or gh CLI")
else:
    print("GITHUB_TOKEN found")

# Initialize GitHubClient
try:
    GitHubClient.get_instance(token=token)
except Exception as e:
    print(f"Failed to initialize GitHubClient: {e}")
    sys.exit(1)

repo_name = "kitamura-tetsuo/auto-coder"
workflow_id = "ci.yml"
ref = "main"

print(f"Attempting to trigger {workflow_id} on {ref} in {repo_name}")
try:
    result = trigger_workflow_dispatch(repo_name, workflow_id, ref)
    print(f"Result: {result}")
except Exception as e:
    print(f"Exception: {e}")
