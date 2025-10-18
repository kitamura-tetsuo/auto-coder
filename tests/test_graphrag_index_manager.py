"""Tests for GraphRAG Index Manager."""

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from src.auto_coder.graphrag_index_manager import GraphRAGIndexManager


@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary repository for testing."""
    # Create some test files
    (tmp_path / "file1.py").write_text("print('hello')")
    (tmp_path / "file2.py").write_text("print('world')")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "file3.py").write_text("print('test')")
    return tmp_path


@pytest.fixture
def temp_index_state_file(tmp_path):
    """Create a temporary index state file."""
    state_file = tmp_path / "index_state.json"
    return str(state_file)


@pytest.fixture
def index_manager(temp_repo, temp_index_state_file):
    """Create a GraphRAGIndexManager instance for testing."""
    return GraphRAGIndexManager(
        repo_path=str(temp_repo), index_state_file=temp_index_state_file
    )


def test_init_default_paths():
    """Test initialization with default paths."""
    manager = GraphRAGIndexManager()
    assert manager.repo_path == Path.cwd()
    assert str(manager.index_state_file).endswith("graphrag_index_state.json")


def test_init_custom_paths(temp_repo, temp_index_state_file):
    """Test initialization with custom paths."""
    manager = GraphRAGIndexManager(
        repo_path=str(temp_repo), index_state_file=temp_index_state_file
    )
    assert manager.repo_path == Path(temp_repo)
    assert manager.index_state_file == Path(temp_index_state_file)


def test_get_codebase_hash(index_manager, temp_repo):
    """Test codebase hash calculation."""
    # Mock git ls-files to fail, forcing fallback to all Python files
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=1)

        hash1 = index_manager._get_codebase_hash()
        assert isinstance(hash1, str)
        assert len(hash1) == 64  # SHA256 hash length

        # Hash should be consistent
        hash2 = index_manager._get_codebase_hash()
        assert hash1 == hash2

        # Hash should change when file content changes
        (temp_repo / "file1.py").write_text("print('modified')")
        hash3 = index_manager._get_codebase_hash()
        assert hash1 != hash3


def test_get_codebase_hash_with_git(index_manager, temp_repo):
    """Test codebase hash calculation with git repository."""
    # Mock git ls-files to return specific files
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(
            returncode=0, stdout="file1.py\nfile2.py\nsubdir/file3.py\n"
        )
        hash1 = index_manager._get_codebase_hash()

    assert isinstance(hash1, str)
    assert len(hash1) == 64


def test_get_codebase_hash_git_failure(index_manager, temp_repo):
    """Test codebase hash calculation when git fails."""
    # Mock git ls-files to fail
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=1)
        hash1 = index_manager._get_codebase_hash()

    # Should fall back to all Python files
    assert isinstance(hash1, str)
    assert len(hash1) == 64


def test_load_index_state_nonexistent(index_manager):
    """Test loading index state when file doesn't exist."""
    state = index_manager._load_index_state()
    assert state == {}


def test_load_index_state_existing(index_manager, temp_index_state_file):
    """Test loading existing index state."""
    # Create a state file
    test_state = {"codebase_hash": "test_hash", "indexed_at": "2025-01-01"}
    with open(temp_index_state_file, "w") as f:
        json.dump(test_state, f)

    state = index_manager._load_index_state()
    assert state == test_state


def test_load_index_state_invalid_json(index_manager, temp_index_state_file):
    """Test loading index state with invalid JSON."""
    # Create an invalid JSON file
    with open(temp_index_state_file, "w") as f:
        f.write("invalid json")

    state = index_manager._load_index_state()
    assert state == {}


def test_save_index_state(index_manager, temp_index_state_file):
    """Test saving index state."""
    test_state = {"codebase_hash": "test_hash", "indexed_at": "2025-01-01"}
    index_manager._save_index_state(test_state)

    # Verify file was created and contains correct data
    assert Path(temp_index_state_file).exists()
    with open(temp_index_state_file, "r") as f:
        saved_state = json.load(f)
    assert saved_state == test_state


def test_save_index_state_creates_directory(temp_repo):
    """Test that save_index_state creates directory if it doesn't exist."""
    state_file = temp_repo / "nested" / "dir" / "state.json"
    manager = GraphRAGIndexManager(repo_path=str(temp_repo), index_state_file=str(state_file))

    test_state = {"codebase_hash": "test_hash"}
    manager._save_index_state(test_state)

    assert state_file.exists()
    with open(state_file, "r") as f:
        saved_state = json.load(f)
    assert saved_state == test_state


def test_is_index_up_to_date_no_state(index_manager):
    """Test is_index_up_to_date when no state exists."""
    result = index_manager.is_index_up_to_date()
    assert result is False


def test_is_index_up_to_date_matching_hash(index_manager):
    """Test is_index_up_to_date when hash matches."""
    # Save current hash
    current_hash = index_manager._get_codebase_hash()
    index_manager._save_index_state({"codebase_hash": current_hash})

    result = index_manager.is_index_up_to_date()
    assert result is True


def test_is_index_up_to_date_different_hash(index_manager, temp_repo):
    """Test is_index_up_to_date when hash differs."""
    # Save old hash
    index_manager._save_index_state({"codebase_hash": "old_hash"})

    result = index_manager.is_index_up_to_date()
    assert result is False


def test_update_index_when_up_to_date(index_manager):
    """Test update_index when index is already up to date."""
    # Save current hash
    current_hash = index_manager._get_codebase_hash()
    index_manager._save_index_state({"codebase_hash": current_hash})

    result = index_manager.update_index(force=False)
    assert result is True


def test_update_index_when_outdated(index_manager):
    """Test update_index when index is outdated."""
    # Save old hash
    index_manager._save_index_state({"codebase_hash": "old_hash"})

    result = index_manager.update_index(force=False)
    assert result is True

    # Verify state was updated
    state = index_manager._load_index_state()
    assert state["codebase_hash"] == index_manager._get_codebase_hash()


def test_update_index_force(index_manager):
    """Test update_index with force=True."""
    # Save current hash
    current_hash = index_manager._get_codebase_hash()
    index_manager._save_index_state({"codebase_hash": current_hash})

    result = index_manager.update_index(force=True)
    assert result is True

    # Verify state was updated
    state = index_manager._load_index_state()
    assert state["codebase_hash"] == current_hash


def test_ensure_index_up_to_date_when_up_to_date(index_manager):
    """Test ensure_index_up_to_date when index is up to date."""
    # Save current hash
    current_hash = index_manager._get_codebase_hash()
    index_manager._save_index_state({"codebase_hash": current_hash})

    result = index_manager.ensure_index_up_to_date()
    assert result is True


def test_ensure_index_up_to_date_when_outdated(index_manager):
    """Test ensure_index_up_to_date when index is outdated."""
    # Save old hash
    index_manager._save_index_state({"codebase_hash": "old_hash"})

    result = index_manager.ensure_index_up_to_date()
    assert result is True

    # Verify state was updated
    state = index_manager._load_index_state()
    assert state["codebase_hash"] == index_manager._get_codebase_hash()


def test_update_index_saves_indexed_at(index_manager):
    """Test that update_index saves indexed_at field."""
    result = index_manager.update_index(force=True)
    assert result is True

    state = index_manager._load_index_state()
    assert "indexed_at" in state
    assert isinstance(state["indexed_at"], str)

