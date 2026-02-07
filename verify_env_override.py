
import os
import sys

# overwrite .env
with open(".env", "w") as f:
    f.write("GITHUB_TOKEN=BAD_TOKEN_FROM_ENV_FILE\n")

# Set env var
os.environ["GITHUB_TOKEN"] = "GOOD_TOKEN_FROM_SHELL"

# Import cli (should trigger load_dotenv)
# We need to make sure we import it freshly
import auto_coder.cli

print(f"GITHUB_TOKEN in env: {os.environ.get('GITHUB_TOKEN')}")

if os.environ.get('GITHUB_TOKEN') == "GOOD_TOKEN_FROM_SHELL":
    print("SUCCESS: Shell env var preserved.")
else:
    print("FAILURE: Shell env var overwritten.")
