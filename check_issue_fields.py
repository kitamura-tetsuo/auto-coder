import json
import os

from dotenv import load_dotenv
from ghapi.all import GhApi


def check_issue_fields():
    env_path = "/home/node/src/auto-coder/.env"
    print(f"Loading env from {env_path}")
    load_dotenv(env_path)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN not found in environment.")
        # Try reading file manually just to be sure
        try:
            with open(env_path, "r") as f:
                content = f.read()
                if "GITHUB_TOKEN" in content:
                    print("GITHUB_TOKEN is present in file, but load_dotenv failed?")
                else:
                    print("GITHUB_TOKEN is NOT present in file.")
        except Exception as e:
            print(f"Error reading .env: {e}")

        return

    print("GITHUB_TOKEN loaded.")

    api = GhApi(token=token)
    owner = "kitamura-tetsuo"
    repo = "auto-coder"

    print(f"Fetching issues for {owner}/{repo}...")
    try:
        # Request sub_issues feature via header just in case
        # But 'i' won't have it if we don't ask, or if list_for_repo doesn't support it?
        # The user says "sub_issues_summary" is there.

        issues = api.issues.list_for_repo(owner, repo, state="open", per_page=10)
    except Exception as e:
        print(f"Error fetching issues: {e}")
        return

    if not issues:
        print("No open issues found.")
        return

    print(f"Found {len(issues)} issues.")

    for i in issues:
        print(f"\n--- Issue #{i['number']} ---")

        # Check for specific fields
        print(f"closed_by exists: {'closed_by' in i}")
        if "closed_by" in i:
            print(f"closed_by: {i['closed_by']}")

        print(f"sub_issues_summary exists: {'sub_issues_summary' in i}")
        if "sub_issues_summary" in i:
            print(f"sub_issues_summary: {i['sub_issues_summary']}")

        print(f"parent_issue_url exists: {'parent_issue_url' in i}")
        if "parent_issue_url" in i:
            print(f"parent_issue_url exists: {i['parent_issue_url']}")

        print(f"parent exists: {'parent' in i}")
        if "parent" in i:
            print(f"parent: {i['parent']}")

        if "sub_issues_summary" in i and i["sub_issues_summary"]["total"] > 0:
            print("!!! Found issue with sub-issues !!!")


if __name__ == "__main__":
    check_issue_fields()
