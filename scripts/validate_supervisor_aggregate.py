"""Validate the fixed Supervisor integration job result set."""

from __future__ import annotations

import json
import os

REQUIRED_GROUPS = {
    "worker-bridge",
    "management-self-update",
    "fresh-host",
    "isolated-containers",
}


def main() -> None:
    """Fail unless every required job is present and successful."""
    results = json.loads(os.environ["REQUIRED_RESULTS"])
    if set(results) != REQUIRED_GROUPS:
        raise SystemExit(f"missing or unexpected groups: {set(results) ^ REQUIRED_GROUPS}")
    failed = {name: data.get("result") for name, data in results.items() if data.get("result") != "success"}
    if failed:
        raise SystemExit(f"required integration groups did not succeed: {failed}")


if __name__ == "__main__":
    main()
