"""Actual normal-owner CLI lifetime with synthetic startup/readiness ports."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from tests.test_management_fresh_launch_process import alive, available_fences, wait_for


@pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
@pytest.mark.parametrize("boundary", ["start", "restore", "recovery"])
@pytest.mark.parametrize("during_dispatch", [False, True])
@pytest.mark.parametrize("restart", [True, False])
@pytest.mark.parametrize("interrupt", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("close_order", ["before-close", "after-close"])
def test_normal_owner_close_fault_preserves_all_fences_on_shutdown(tmp_path, boundary, restart, interrupt, close_order, during_dispatch):
    repository = Path(__file__).resolve().parents[1]
    log = (tmp_path / "driver.log").open("w")
    process = subprocess.Popen([sys.executable, "-m", "tests.fixtures.management_reader_close_process", str(tmp_path), boundary, str(restart).lower(), close_order, str(int(interrupt)) if during_dispatch else "none"], cwd=repository, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    child = None
    try:
        wait_for(lambda: (tmp_path / "start-returned").exists() or process.poll() is not None or ((tmp_path / "child.json").exists() and available_fences(tmp_path)[2]), "start boundary")
        assert (tmp_path / "start-returned").exists(), {"log": (tmp_path / "driver.log").read_text(), "child_created": (tmp_path / "child.json").exists(), "fences": available_fences(tmp_path)}
        wait_for(lambda: (tmp_path / "child-ready").exists(), "child ready")
        child = json.loads((tmp_path / "child.json").read_text())
        assert alive(child) and process.poll() is None
        assert available_fences(tmp_path) == [False, False, False]
        assert len((tmp_path / "spawned.jsonl").read_text().splitlines()) == 1
        outcome = json.loads((tmp_path / "start-result.json").read_text())
        assert (outcome["status"], outcome["fresh"], outcome["owned"]) == ("unknown", False, 1)

        def child_state():
            database = tmp_path / "state" / "management-child.sqlite3"
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                return connection.execute("SELECT record_json FROM child_state WHERE instance_id=?", (outcome["child"]["instance_id"],)).fetchone()[0]

        original_child = child_state()
        original_record = (tmp_path / "start-result.json").read_bytes()
        os.kill(process.pid, interrupt)
        wait_for(lambda: "preserving owner fences" in (tmp_path / "driver.log").read_text() or process.poll() is not None or available_fences(tmp_path)[2], "shutdown reached the owner preservation boundary")
        observed = {"owner_exit": process.poll(), "child_alive": alive(child), "fences_available": available_fences(tmp_path)}
        assert observed == {"owner_exit": None, "child_alive": True, "fences_available": [False, False, False]}, observed
        time.sleep(0.1)
        assert process.poll() is None and alive(child)
        assert available_fences(tmp_path) == [False, False, False]
        assert child_state() == original_child
        assert (tmp_path / "start-result.json").read_bytes() == original_record
        assert (tmp_path / "launches").read_text().splitlines() == ["launch"]
        (tmp_path / "child-release").touch()
        process.wait(timeout=10)
        assert process.returncode != 0
        assert not alive(child)
        assert available_fences(tmp_path) == [True, True, True]
    finally:
        cleanup_errors = []
        try:
            (tmp_path / "child-release").touch()
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
        except Exception as exc:
            cleanup_errors.append(exc)
        identities = []
        try:
            if (tmp_path / "spawned.jsonl").exists():
                for line in (tmp_path / "spawned.jsonl").read_text().splitlines():
                    try:
                        identities.append(json.loads(line))
                    except (ValueError, TypeError) as exc:
                        cleanup_errors.append(exc)
            if (tmp_path / "child.json").exists():
                identities.append(json.loads((tmp_path / "child.json").read_text()))
        except Exception as exc:
            cleanup_errors.append(exc)
        seen = set()
        for identity in identities:
            try:
                key = (identity["pid"], identity["started"])
                if key in seen:
                    continue
                seen.add(key)
                if alive(identity):
                    try:
                        os.kill(identity["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    wait_for(lambda: not alive(identity), "child cleanup")
            except Exception as exc:
                cleanup_errors.append(exc)
        log.close()
        if cleanup_errors:
            raise ExceptionGroup("disposable process cleanup failed", cleanup_errors)
