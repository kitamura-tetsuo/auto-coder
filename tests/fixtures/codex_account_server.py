"""Strict stdio peer for account API transport regression tests."""

import json
import sys
import time
from pathlib import Path

mode, request_path = sys.argv[1:]


def read():
    line = sys.stdin.readline()
    with Path(request_path).open("a") as output:
        output.write(line)
    return json.loads(line)


assert read()["method"] == "initialize"
print(json.dumps({"id": 1, "result": {}}), flush=True)
assert read() == {"method": "initialized"}
request = read()
assert request["method"] == "account/rateLimits/read"
if mode == "timeout":
    time.sleep(30)
elif mode == "invalid":
    print("invalid JSON", flush=True)
elif mode == "oversized":
    print("x" * 1_048_577, flush=True)
elif mode == "error":
    print(json.dumps({"id": 2, "error": {"message": "private server error"}}), flush=True)
elif mode == "success":
    print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
    print(json.dumps({"id": 99, "result": {"wrong": True}}), flush=True)
    print(json.dumps({"id": 2, "result": {"rateLimits": {"limitId": "codex"}}}), flush=True)
