import shutil
from pathlib import Path

import pytest

from auto_coder.workspace_ownership import MARKER, create_owned_workspace, create_ownership, verify_ownership


def test_producer_anchor_rejects_copied_marker_and_legacy_root(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    target = root / "job" / "incarnation"
    target.mkdir(parents=True)
    anchor = create_ownership(root, target, "job", "incarnation")
    assert verify_ownership(target, anchor, "job", "incarnation")
    assert not verify_ownership(target, anchor, "other", "incarnation")
    old = tmp_path / "old"
    target.rename(old)
    target.mkdir()
    shutil.copy2(old / MARKER, target / MARKER)
    assert not verify_ownership(target, anchor, "job", "incarnation")
    (target / MARKER).unlink()
    assert not verify_ownership(target, anchor, "job", "incarnation")


def test_existing_directory_is_never_stamped_or_replaced(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    target = root / "job" / "incarnation"
    target.mkdir(parents=True)
    (target / "foreign").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_owned_workspace(root, target, "job", "incarnation")
    assert (target / "foreign").read_text(encoding="utf-8") == "keep"
    assert not (target / MARKER).exists()
    assert not list((root / ".ownership-v1").iterdir())
