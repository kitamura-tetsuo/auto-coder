"""Distribution-level checks for the management OAuth browser assets."""

import subprocess
import sys
import time
import zipfile


def test_built_wheel_contains_readable_management_browser_assets(tmp_path, _use_real_commands):
    distribution = tmp_path / "dist"
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(distribution)], check=True, capture_output=True, text=True)
    deadline = time.monotonic() + 10
    wheels = list(distribution.glob("*.whl"))
    while not wheels and time.monotonic() < deadline:
        time.sleep(0.05)
        wheels = list(distribution.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        assert "auto_coder/management/static/authorize.html" in names
        assert "auto_coder/management/static/authorize.js" in names
        install_root = tmp_path / "installed"
        wheel.extractall(install_root)

    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; from importlib.resources import files; "
                f"sys.path.insert(0, {str(install_root)!r}); "
                "root=files('auto_coder.management.static'); "
                "assert 'Authorize Auto-Coder' in root.joinpath('authorize.html').read_text(); "
                "assert 'signInWithPopup' in root.joinpath('authorize.js').read_text()"
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
