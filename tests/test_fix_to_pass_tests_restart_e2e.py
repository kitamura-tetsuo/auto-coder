import json
import os
import subprocess
import sys
from pathlib import Path

from .test_cli_auto_update_e2e import _build_env


def _write_fake_upgrading_pipx(script_path: Path) -> Path:
    marker = script_path.parent / "pipx_invocation.json"
    script_contents = f"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
marker = Path({str(script_path.parent)!r}) / "pipx_invocation.json"
existing = []
if marker.exists():
    try:
        existing = json.loads(marker.read_text())
    except Exception:
        existing = []
existing.append({{"argv": sys.argv, "cwd": os.getcwd()}})
marker.write_text(json.dumps(existing))
print('upgraded package auto-coder from 0.0.1 to 0.0.2')
"""
    script_path.write_text(script_contents)
    script_path.chmod(0o755)
    return script_path


def test_fix_to_pass_tests_loop_triggers_restart_on_update(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_upgrading_pipx(bin_dir / "pipx")
    codex_stub = bin_dir / "codex"
    codex_stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then\n'
        "  echo 'codex stub'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "exec" ]; then\n'
        "  echo 'codex exec stub'\n"
        "  exit 0\n"
        "fi\n"
        "echo 'codex stub invoked' >&2\n"
        "exit 0\n"
    )
    codex_stub.chmod(0o755)

    env = _build_env(tmp_path)
    env["AUTOCODER_CODEX_CLI"] = "echo codex"
    env["AUTO_CODER_TEST_CAPTURE_RESTART"] = str(tmp_path / "restart.json")

    repo_root = Path(__file__).resolve().parents[1]
    existing_py_path = env.get("PYTHONPATH") or ""
    extra_paths = [str(repo_root), str(repo_root / "src")]
    if existing_py_path:
        extra_paths.append(existing_py_path)
    env["PYTHONPATH"] = os.pathsep.join(extra_paths)

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    test_script = scripts_dir / "test.sh"
    marker_file = tmp_path / "test-script-invoked"
    test_script.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'should not run' > /dev/null\n"
        f"touch '{marker_file}'\n"
        "exit 1\n"
    )
    test_script.chmod(0o755)

    cmd = [
        sys.executable,
        "-m",
        "src.auto_coder.cli",
        "fix-to-pass-tests",
        "--dry-run",
    ]

    completed = subprocess.run(
        cmd,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert (
        not marker_file.exists()
    ), "test script should not run when restart happens first"

    restart_marker = tmp_path / "restart.json"
    assert restart_marker.exists()
    restart_payload = json.loads(restart_marker.read_text())
    assert "fix-to-pass-tests" in restart_payload["argv"]

    pipx_marker = bin_dir / "pipx_invocation.json"
    assert pipx_marker.exists()
    invocations = json.loads(pipx_marker.read_text())
    assert len(invocations) >= 1
    assert invocations[0]["argv"][1:] == ["upgrade", "auto-coder"]
