# scripts/test.sh enforces the package-wide mypy gate

`scripts/test.sh` runs `mypy --config-file pyproject.toml -p auto_coder` --
the same command, package scope, and root `pyproject.toml` `[tool.mypy]`
configuration as the `PR Tests` "Lint & Type Check" job and the `mypy`
pre-commit hook -- in place of the previous import-only probe that was
wrapped in `|| true` and could never fail the script. A nonzero result (or
an unavailable checker, missing/unreadable explicit configuration, or failed
tool provisioning) now stops the script under `set -Eeuo pipefail` before
the "all checks passed" message and before `local_test_log_collector.py` or
pytest ever runs, on both the `uv` and system-Python runner paths and in
both CI and local execution. The Black/isort/Flake8/mypy quality-stage order,
CI check-only vs. local auto-fix formatting behavior, and the collector's
exactly-once invocation with the original argument vector and exit status
are unchanged.

Container forwarding (`AM_I_AUTOCODER_CONTAINER=true` with an explicit,
nonempty `REPO_NAME` and no true `INSIDE_TARGET_EXECUTION`) still redirects
to `auto-coder-<repo>` and now also propagates a target checkout's mypy
failure to the outer caller, since the forwarded script is this same,
now-enforcing `scripts/test.sh`. `scripts/run_pr_test_shard.py`'s existing
timeout-only retry policy is unchanged: a completed mypy failure is an
ordinary failure and is never retried. This is test/CI infrastructure only:
production processing, structured trace events, and dashboard projections
are unchanged.
