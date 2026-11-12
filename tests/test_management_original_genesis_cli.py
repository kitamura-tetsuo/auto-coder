"""Actual CLI refusal boundaries, separate from Docker/genesis acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


def test_selected_genesis_missing_state_root_is_not_recreated_by_diagnostic_logging(tmp_path: Path) -> None:
    state = tmp_path / "missing-original-state"
    configuration = tmp_path / "config.json"
    configuration.write_text(
        json.dumps(
            {
                "instance_id": "original-component",
                "state_root": str(state),
                "oauth_root": str(tmp_path / "oauth"),
                "journal_root": str(tmp_path / "journal"),
                "listener_url": "http://127.0.0.1:8081/mcp",
                "initial_runtime": "must-not-launch",
                "runtimes": [{"reference": "must-not-launch", "receipt": "sha256:" + "a" * 64, "executable": "/opt/auto-coder/.venv/bin/auto-coder", "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"]}],
                "original_genesis": "/bootstrap/original-genesis.json",
            }
        )
    )
    configuration.chmod(0o600)
    result = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.bootstrap_supervisor", "--config", str(configuration)], capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert not state.exists(), "diagnostics recreated a missing selected original state root before refusal"
    assert not (tmp_path / "journal").exists() and not (tmp_path / "oauth").exists()
