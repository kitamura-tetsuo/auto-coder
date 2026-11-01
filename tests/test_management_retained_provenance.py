import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management.fresh_host_observation import HostObservationUnavailable, assert_retained_journal_source
from auto_coder.management.fresh_initialization import claim_fresh_initialization, load_fresh_initialization
from auto_coder.management.operation_store import ManagementOperationStore
from tests.test_management_fresh_initialization import _intent


def test_prior_journal_source_matches_captured_old_mount_and_inode(tmp_path: Path, monkeypatch) -> None:
    intent = _intent(tmp_path)
    root = tmp_path / "old-journal"
    store = ManagementOperationStore(root)
    store.get("missing")
    database = root / "management-operations.sqlite3"
    info = database.stat()
    intent = replace(intent, host_prior_journal_root=str(root), prior_journal_root="/management-prior-operations", prior_journal_device=info.st_dev, prior_journal_inode=info.st_ino)
    intent.validate()
    config = tmp_path / "old-bootstrap.json"
    config.write_text(json.dumps({"instance_id": "old-instance", "journal_root": "/management-operations"}), encoding="utf-8")
    mounts = [{"Type": "bind", "Source": str(root), "Destination": "/management-operations", "RW": True}]
    observed = ((str(root), "/management-operations", True),)
    digest = hashlib.sha256(json.dumps(observed, separators=(",", ":")).encode()).hexdigest()
    prior = SimpleNamespace(bootstrap_source=str(config), bootstrap_sha256=hashlib.sha256(config.read_bytes()).hexdigest(), instance_id="old-instance", container_id="a" * 64, mounts_sha256=digest)
    monkeypatch.setattr("auto_coder.management.fresh_host_observation._inspect", lambda _container: [None] * 11 + [mounts])
    assert_retained_journal_source(prior, intent)
    with pytest.raises(HostObservationUnavailable):
        assert_retained_journal_source(prior, replace(intent, prior_journal_inode=info.st_ino + 1))
    mounts[0]["Source"] = str(tmp_path / "other")
    with pytest.raises(HostObservationUnavailable):
        assert_retained_journal_source(prior, intent)


def test_old_fresh_intent_encoding_is_still_loadable(tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    claim_fresh_initialization(tmp_path / "state", intent)
    path = tmp_path / "state" / "fresh-initialization.json"
    raw = json.loads(path.read_text())
    for key in ("host_prior_journal_root", "prior_journal_root", "prior_journal_device", "prior_journal_inode"):
        del raw[key]
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    loaded, digest = load_fresh_initialization(tmp_path / "state", host=True)
    assert loaded == intent and digest == hashlib.sha256(path.read_bytes()).hexdigest()
