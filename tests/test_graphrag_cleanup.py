"""Tests for GraphRAG snapshot cleanup and CLI wiring."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest
from click.testing import CliRunner

from src.auto_coder.cli_commands_graphrag import graphrag_group
from src.auto_coder.graphrag_index_manager import GraphRAGIndexManager


def _make_index_manager(tmp_path: Path) -> GraphRAGIndexManager:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_file = tmp_path / "index_state.json"
    return GraphRAGIndexManager(repo_path=str(repo), index_state_file=str(state_file))


def test_build_repo_key_uses_remote_and_strips_credentials(tmp_path, monkeypatch):
    manager = _make_index_manager(tmp_path)
    repo_str = str(manager.repo_path.resolve())

    def fake_run(args, capture_output=False, text=False, timeout=None):
        class Result:
            returncode = 0
            stdout = "https://token123@github.com/example/repo.git\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("src.auto_coder.graphrag_index_manager.subprocess.run", fake_run)

    key = manager._build_repo_key()
    assert repo_str in key
    assert "github.com/example/repo.git" in key
    assert "token123@" not in key


def test_cleanup_snapshots_applies_time_based_policy(tmp_path, monkeypatch):
    manager = _make_index_manager(tmp_path)
    repo = manager.repo_path
    repo_key = manager._build_repo_key()
    now = datetime.now(timezone.utc)

    snapshots = [
        {
            "repo_key": repo_key,
            "snapshot_id": "old",
            "indexed_at": (now - timedelta(days=10)).isoformat(),
            "repo_path": str(repo),
            "codebase_hash": "h1",
        },
        {
            "repo_key": repo_key,
            "snapshot_id": "mid",
            "indexed_at": (now - timedelta(days=5)).isoformat(),
            "repo_path": str(repo),
            "codebase_hash": "h2",
        },
        {
            "repo_key": repo_key,
            "snapshot_id": "new",
            "indexed_at": now.isoformat(),
            "repo_path": str(repo),
            "codebase_hash": "h3",
        },
    ]
    manager._save_index_state({"codebase_hash": "current", "indexed_at": str(repo.resolve()), "snapshots": snapshots})

    deleted: List[str] = []

    def fake_delete(snap):
        deleted.append(snap.snapshot_id)

    monkeypatch.setattr(manager, "_delete_snapshot_from_stores", fake_delete)

    result = manager.cleanup_snapshots(dry_run=False, retention_days=7, max_snapshots_per_repo=9)

    assert deleted == ["old"]
    assert {a.snapshot_id for a in result.deleted} == {"old"}
    state_after = manager._load_index_state()
    remaining_ids = [s["snapshot_id"] for s in state_after["snapshots"]]
    assert remaining_ids == ["mid", "new"]


def test_graphrag_cleanup_cli_dry_run_works(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state_file = repo / ".auto-coder" / "graphrag_index_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    state = {
        "codebase_hash": "h1",
        "indexed_at": str(repo.resolve()),
        "snapshots": [
            {
                "repo_key": str(repo),
                "snapshot_id": "s1",
                "indexed_at": (now - timedelta(days=10)).isoformat(),
                "repo_path": str(repo),
                "codebase_hash": "h1",
            },
            {
                "repo_key": str(repo),
                "snapshot_id": "s2",
                "indexed_at": now.isoformat(),
                "repo_path": str(repo),
                "codebase_hash": "h2",
            },
        ],
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        graphrag_group,
        ["cleanup", "--dry-run", "--repo-path", str(repo)],
    )

    assert result.exit_code == 0
    assert "Dry-run complete" in result.output
