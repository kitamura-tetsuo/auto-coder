import subprocess
import sys
from pathlib import Path


def test_cli_accessible_from_any_directory(tmp_path):
    """Ensure CLI works after installation from any directory."""
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-e",
            str(project_root),
        ],
        check=True,
    )
    result = subprocess.run(
        [
            "auto-coder",
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert "Usage: auto-coder" in result.stdout
