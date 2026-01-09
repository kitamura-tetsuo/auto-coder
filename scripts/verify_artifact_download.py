
import sys
import os
import logging
import subprocess

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from auto_coder.util.github_action import _get_github_actions_logs
from auto_coder.automation_config import AutomationConfig
from auto_coder.github_client import GitHubClient

# Mock config
class MockConfig:
    SEARCH_GITHUB_ACTIONS_HISTORY = False

# Setup logging
logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    # User provided run ID: 20593185228
    parser.add_argument("--run-id", default="20593185228")
    args = parser.parse_args()
    
    # Fetch token from gh CLI
    try:
        token = subprocess.check_output(["gh", "auth", "token"]).decode("utf-8").strip()
    except Exception as e:
        print(f"Failed to get token from gh CLI: {e}")
        sys.exit(1)

    # Initialize GitHubClient
    GitHubClient.get_instance(token=token)
    
    repo_name = "kitamura-tetsuo/outliner"
    config = MockConfig()
    
    failed_checks = [
        {
            "name": "e2e-test",
            "details_url": f"https://github.com/{repo_name}/actions/runs/{args.run_id}",
            "conclusion": "failure"
        }
    ]
    
    print(f"Verifying logs for run {args.run_id}...")
    try:
        logs = _get_github_actions_logs(repo_name, config, failed_checks)
        print("Logs retrieved:")
        print(logs[:2000] + ("\n..." if len(logs) > 2000 else ""))
    except Exception as e:
        print(f"Caught expected exception or error: {e}")
