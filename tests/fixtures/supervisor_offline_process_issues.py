"""Exec the real process-issues CLI without requiring live GitHub credentials.

The Supervisor invokes this fixture using the selected runtime's ``python``
from PATH, so a staged worker still runs in its installed virtual environment.
Only production CLI options are used; no application functions are replaced.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    if os.environ.get("AUTO_CODER_FIXTURE_HOLD_BOOTSTRAP") == "1" and "/worker-versions/" not in os.environ.get("VIRTUAL_ENV", ""):
        # The disposable bootstrap incumbent holds a genuine UDS/OS drain;
        # the subsequently selected locked-uv runtime still executes the
        # unmodified production process-issues CLI below.
        os.execv(sys.executable, [sys.executable, "/fixtures/supervisor_bridge_worker.py"])
    token = os.environ["GITHUB_TOKEN"]
    command = [
        sys.executable,
        "-m",
        "auto_coder.cli",
        "process-issues",
        "--repo",
        os.environ["AUTO_CODER_REPOSITORY"],
        "--github-token",
        token,
        "--skip-codex-auth-secret-sync",
        "--disable-webhook",
        "--log-file",
        os.path.join(os.environ["AUTO_CODER_RUNTIME_ROOT"], "logs", "auto-coder.log"),
    ]
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
