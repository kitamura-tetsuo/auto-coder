"""Native Supervisor CLI + physical disposable child; host authorization substituted.

This is a positive real-OS provenance/store production check, NOT a true
host-guard or authenticated management-readiness acceptance oracle.
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_management_fresh_launch_process import alive, available_fences, wait_for

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


@pytest.fixture
def _use_real_commands() -> None:
    pass


@pytest.fixture
def _use_real_sleep() -> None:
    pass


def test_original_profile_cli_produces_private_store_receipt_before_physical_early_exit(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(repository / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    log = (tmp_path / "driver.log").open("w")
    driver = subprocess.Popen(
        [sys.executable, "-B", "-m", "tests.fixtures.fresh_launch_process", str(tmp_path), "original-early-exit"],
        cwd=repository,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    try:
        wait_for(lambda: (tmp_path / "host-exited").exists() or driver.poll() is not None, "substituted host grant did not complete", timeout=20)
        assert (tmp_path / "host-exited").exists(), (tmp_path / "driver.log").read_text()
        driver.wait(timeout=20)
        assert driver.returncode != 0
        # The actual Supervisor entrypoint created an original 0600 owner and
        # the actual native DB/receipt producers ran prior to physical Popen.
        state = tmp_path / "state"
        journals = tmp_path / "journal"
        assert (state / "fresh-launch-claimed.json").is_file()
        assert (state / "fresh-original-stores.json").is_file()
        assert (tmp_path / "launches").read_text().splitlines() == ["launch"]
        assert not (state / "fresh-launched.json").exists()
        for path in [
            state / ".supervisor-owner.lock",
            state / "management-child.sqlite3",
            state / "management-self-update.sqlite3",
            state / "management-recovery.sqlite3",
            state / "fresh-original-stores.json",
            journals / "management-operations.sqlite3",
        ]:
            assert path.is_file()
            assert path.stat().st_mode & 0o777 == 0o600
        record = json.loads((state / "fresh-original-stores.json").read_bytes())
        assert record["lifetime"]["pid"] == driver.pid
        assert set(record["stores"]) == {"child", "handoff", "recovery", "operations"}
        assert record["request_id"] == json.loads((state / "fresh-initialization.json").read_bytes())["request_id"]
        assert available_fences(tmp_path) == [True, True, True]

        # The independent reader sees the complete covered attempt, but must
        # refuse to turn substituted host transport into original grant proof.
        authority = json.loads((tmp_path / "original-history/authority.json").read_bytes())
        binding_file = tmp_path / "original-binding.json"
        binding_file.write_text(json.dumps(authority["binding"], sort_keys=True))
        verifier = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "auto_coder.management.original_attempt_evidence",
                "--binding",
                str(binding_file),
            ],
            capture_output=True,
            text=True,
            cwd=repository,
            env=environment,
            timeout=20,
            check=False,
        )
        assert verifier.returncode == 1, verifier.stderr
        evidence = json.loads(verifier.stdout)
        assert len(evidence["attempts"]) == 1
        assert evidence["attempts"][0]["state"] == "claimed"
        assert evidence["unavailable"] == [record["request_id"] + ": original grant boundary not crossed"]
    finally:
        (tmp_path / "holder-release").touch()
        (tmp_path / "child-release").touch()
        if driver.poll() is None:
            os.killpg(driver.pid, signal.SIGKILL)
        driver.wait(timeout=10)
        for name in ("child.json", "holder.json"):
            path = tmp_path / name
            if path.exists():
                lifetime = json.loads(path.read_text())
                if alive(lifetime):
                    if name == "holder.json":
                        os.killpg(lifetime["pid"], signal.SIGKILL)
                    else:
                        os.kill(lifetime["pid"], signal.SIGKILL)
                    wait_for(lambda: not alive(lifetime), "fixture subprocess leaked", timeout=5)
        log.close()
