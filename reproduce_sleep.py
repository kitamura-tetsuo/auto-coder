import sys
from src.auto_coder.cli_ui import sleep_with_countdown

print("Starting sleep test for 3 seconds...")
sleep_with_countdown(3, sys.stdout)
print("Done.")
