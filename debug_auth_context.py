
import os
import sys
from auto_coder.auth_utils import get_github_token, verify_github_access

def debug_auth():
    print("--- Debug Auth Context ---")
    token = os.getenv("GITHUB_TOKEN")
    if token:
        print(f"GITHUB_TOKEN env var is SET. Length: {len(token)}")
        print(f"Token prefix: {token[:4]}...")
    else:
        print("GITHUB_TOKEN env var is NOT SET.")
        
    resolved_token = get_github_token()
    if resolved_token:
        print(f"Resolved token (get_github_token): Present. Length: {len(resolved_token)}")
        if token and resolved_token == token:
            print("Resolved token MATCHES env var.")
        else:
            print("Resolved token DOES NOT match env var (likely from gh CLI).")
    else:
        print("Resolved token: None")
        
    print("\nAttempting verification...")
    try:
        success = verify_github_access()
        print(f"Verification result: {success}")
    except Exception as e:
        print(f"Verification raised exception: {e}")

if __name__ == "__main__":
    debug_auth()
