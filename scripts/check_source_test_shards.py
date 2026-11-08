"""Fail unless current source plan, native Actions jobs and receipts all match."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from auto_coder.tested_source_refs import TestedSourceError, verify_source_shard_attempt
from auto_coder.util.gh_cache import GitHubGitDataClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise TestedSourceError("GITHUB_TOKEN is unavailable")
    verify_source_shard_attempt(
        GitHubGitDataClient(token, args.repository),
        args.run_id,
        args.run_attempt,
        args.source_sha,
        plan_path=args.plan,
        artifacts_root=args.artifacts,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
