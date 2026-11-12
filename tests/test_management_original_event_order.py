"""Original admission must validate predecessor claims in chronological order.

Rehashed original records are corruption controls, never positive host evidence.
"""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management.fresh_host_observation import serve_one_fresh_grant
from auto_coder.management.fresh_initialization import claim_fresh_initialization
from auto_coder.management.original_attempt_evidence import derive, observe
from auto_coder.management.original_attempts import OriginalEvidenceUnavailable, canonical, read_frontier
from tests.test_management_original_attempts import _covered, _fingerprint, _history


@pytest.mark.parametrize("boundary", ["admission", "grant"])
@pytest.mark.parametrize("claim_order", ["after-descendant-anchor", "after-descendant-claim"])
def test_original_origin_refuses_descendant_anchored_before_parent_claim(tmp_path, boundary, claim_order):
    binding = _history(tmp_path)
    parent = _covered(tmp_path, binding, "parent")
    claim_fresh_initialization(Path(parent.host_state_root), parent)
    child = replace(_covered(tmp_path, binding, "child"), old_instance=parent.new_instance, old_container_id=parent.new_container_id)
    claim_fresh_initialization(Path(child.host_state_root), child)
    current = replace(_covered(tmp_path, binding, "current"), old_instance=child.new_instance, old_container_id=child.new_container_id)
    if boundary == "grant":
        claim_fresh_initialization(Path(current.host_state_root), current)

    root = Path(binding.root)
    _, events = read_frontier(root, binding)
    originals = [event.record() for event in events]
    # Preserve every valid intent and per-attempt sequence, but place the
    # parent's durable claim after the child already used it as a predecessor.
    order = [0, 2, 1, 3] if claim_order == "after-descendant-anchor" else [0, 2, 3, 1]
    reordered = [originals[index] for index in order] + originals[4:]
    previous = ""
    for index, event in enumerate(reordered):
        event["previous"] = previous
        event["sequence"] = index
        encoded = canonical(event)
        (root / f"{index:08d}.json").write_bytes(encoded)
        previous = hashlib.sha256(encoded).hexdigest()
    (root / "frontier.json").write_bytes(canonical({"count": len(reordered), "digest": previous}))
    before = _fingerprint(tmp_path)

    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert any("predecessor" in reason for reason in evidence.unavailable)
    with pytest.raises(OriginalEvidenceUnavailable, match="predecessor"):
        if boundary == "admission":
            claim_fresh_initialization(Path(current.host_state_root), current)
        else:
            # The actual host entry must refuse at the original-history gate,
            # before examining any downstream host authority or opening a socket.
            serve_one_fresh_grant(tmp_path / "fresh-guard.sock", current, None, None, -1, -1)
    assert _fingerprint(tmp_path) == before
    assert not (tmp_path / "fresh-guard.sock").exists()
    if boundary == "admission":
        assert not any(Path(current.host_state_root).iterdir())
    assert not any(Path(current.host_journal_root).iterdir())


def test_original_order_preserves_serial_claims_and_failed_siblings(tmp_path):
    binding = _history(tmp_path)
    parent = _covered(tmp_path, binding, "parent")
    claim_fresh_initialization(Path(parent.host_state_root), parent)
    failed = replace(_covered(tmp_path, binding, "failed-sibling"), old_instance=parent.new_instance, old_container_id=parent.new_container_id)
    failed = replace(failed, oauth_inode=failed.oauth_inode + 1)
    with pytest.raises(ValueError, match="retained OAuth root identity differs"):
        claim_fresh_initialization(Path(failed.host_state_root), failed)
    child = replace(_covered(tmp_path, binding, "child"), old_instance=parent.new_instance, old_container_id=parent.new_container_id)
    claim_fresh_initialization(Path(child.host_state_root), child)
    later_sibling = replace(_covered(tmp_path, binding, "later-sibling"), old_instance=parent.new_instance, old_container_id=parent.new_container_id)
    claim_fresh_initialization(Path(later_sibling.host_state_root), later_sibling)
    before = _fingerprint(tmp_path)

    _, events = read_frontier(Path(binding.root), binding)
    assert [(event.attempt, event.kind) for event in events] == [
        (parent.request_id, "anchor"),
        (parent.request_id, "claimed"),
        (failed.request_id, "anchor"),
        (failed.request_id, "never-created"),
        (child.request_id, "anchor"),
        (child.request_id, "claimed"),
        (later_sibling.request_id, "anchor"),
        (later_sibling.request_id, "claimed"),
    ]
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert [(attempt.target, attempt.state) for attempt in evidence.attempts] == [("parent", "claimed"), ("failed-sibling", "never-created"), ("child", "claimed"), ("later-sibling", "claimed")]
        assert evidence.unavailable == tuple(intent.request_id + ": original grant boundary not crossed" for intent in (parent, child, later_sibling))
    assert _fingerprint(tmp_path) == before
