import json
import os
import subprocess
import sys
from pathlib import Path


def _write_fake_pipx(script_path: Path, exit_code: int, stderr: str = "") -> Path:
    marker_dir = repr(str(script_path.parent))
    script_contents = f"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
marker = Path({marker_dir}) / "pipx_invocation.json"
data = {{
    "argv": sys.argv,
    "cwd": os.getcwd(),
}}
marker.write_text(json.dumps(data))
if {exit_code}:
    sys.stderr.write({stderr!r})
sys.exit({exit_code})
"""
    script_path.write_text(script_contents)
    script_path.chmod(0o755)
    return script_path


def _build_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PATH", "")
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}" + env["PATH"]
    env["PIPX_HOME"] = str(tmp_path / "pipx-home")
    env["AUTO_CODER_UPDATE_STATE_DIR"] = str(tmp_path / "state")
    env["AUTO_CODER_UPDATE_INTERVAL_SECONDS"] = "0"
    env.pop("AUTO_CODER_DISABLE_AUTO_UPDATE", None)
    return env


def test_cli_auto_update_invokes_pipx_upgrade(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script_path = bin_dir / "pipx"
    _write_fake_pipx(script_path, exit_code=0)

    env = _build_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "src.auto_coder.cli", "auth-status"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    marker = bin_dir / "pipx_invocation.json"
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["argv"][1:] == ["upgrade", "auto-coder"]
    assert "Checking authentication status" in result.stdout

    state_file = tmp_path / "state" / "update_state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["last_result"] == "success"


def test_cli_auto_update_notifies_on_failure(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script_path = bin_dir / "pipx"
    _write_fake_pipx(script_path, exit_code=1, stderr="simulated error\n")

    env = _build_env(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "src.auto_coder.cli", "auth-status"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    marker = bin_dir / "pipx_invocation.json"
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["argv"][1:] == ["upgrade", "auto-coder"]
    assert "Auto-Coder auto-update could not be completed" in completed.stderr
    assert "pipx upgrade auto-coder" in completed.stderr

    state_file = tmp_path / "state" / "update_state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["last_result"] == "failure"
