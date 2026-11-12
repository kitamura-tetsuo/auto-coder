"""Read-only NEW status through the actual management operator command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from auto_coder.management.fresh_begin import begin_new_instance
from auto_coder.management.fresh_new_profile import inspect_new_instance_profile
from tests.test_management_new_instance_preflight import _bytes, _environment, request_file
from tests.test_management_new_instance_profile import configured_request

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


def _status(request_file: Path) -> subprocess.CompletedProcess[str]:
    request = json.loads(request_file.read_bytes())
    return subprocess.run(
        [sys.executable, "-B", "-m", "auto_coder.cli", "management", "fresh-status", "--config", request["management_config"]],
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(),
    )


def test_new_status_reports_original_only_without_launch_authority(configured_request: Path) -> None:
    begin_new_instance(configured_request)
    before = _bytes(configured_request.parent)
    result = _status(configured_request)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "request_id": json.loads(configured_request.read_bytes())["request_id"],
        "status": "origin-recorded-only",
        "launch_authorized": False,
    }
    assert _bytes(configured_request.parent) == before
    assert list((configured_request.parent / "state").iterdir()) == []
    assert list((configured_request.parent / "operations").iterdir()) == []


def test_new_status_and_profile_accept_retained_oauth_directory_timestamp_change(configured_request: Path) -> None:
    """SQLite journal sidecars may change directory times without replacing OAuth."""
    begin_new_instance(configured_request)
    oauth_root = configured_request.parent / "oauth"
    old = oauth_root.stat()
    os.utime(oauth_root, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
    original_oauth = (oauth_root / "oauth.sqlite3").read_bytes()
    before = _bytes(configured_request.parent)

    assert inspect_new_instance_profile(configured_request).status == "profile-verified-only"
    result = _status(configured_request)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "origin-recorded-only"
    assert (oauth_root / "oauth.sqlite3").read_bytes() == original_oauth
    assert _bytes(configured_request.parent) == before


def test_new_status_refuses_replaced_retained_oauth_root(configured_request: Path) -> None:
    """Relaxing timestamps cannot admit a different OAuth directory inode."""
    begin_new_instance(configured_request)
    oauth_root = configured_request.parent / "oauth"
    oauth_root.rename(configured_request.parent / "oauth-original")
    oauth_root.mkdir(mode=0o700)

    result = _status(configured_request)
    assert result.returncode != 0 and result.stdout == ""
    assert "fresh status unavailable" in result.stderr


@pytest.mark.parametrize("fault", ["partial-host-claim", "unclaimed-outcome", "source-changed", "origin-request-substitution"])
def test_new_status_never_reports_success_from_inconsistent_original(configured_request: Path, fault: str) -> None:
    begin_new_instance(configured_request)
    root = configured_request.parent
    if fault == "partial-host-claim":
        (root / "guard/new-docker-first-start.json").write_bytes(b"{}")
    elif fault == "unclaimed-outcome":
        (root / "state/new-first-outcome.json").write_bytes(b'{"status":"succeeded"}')
    elif fault == "source-changed":
        config = json.loads((root / "bootstrap.json").read_bytes())
        config["crash_budget"] = 1
        (root / "bootstrap.json").write_text(json.dumps(config))
    elif fault == "origin-request-substitution":
        record = root / "guard/new-instance-origin.json"
        value = json.loads(record.read_bytes())
        value["assessment"]["original_request"]["path"] = str(root / "oauth/oauth.sqlite3")
        record.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    before = _bytes(root)
    result = _status(configured_request)
    assert result.returncode != 0 and result.stdout == "", (result.returncode, result.stdout, result.stderr)
    assert "fresh status unavailable" in result.stderr
    assert _bytes(root) == before
    assert not (root / "state/physical-child-invocations").exists()
