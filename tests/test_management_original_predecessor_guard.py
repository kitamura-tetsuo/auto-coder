"""Isolated Linux regression for refused original predecessors and branch capture."""

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management.fresh_initialization import claim_fresh_initialization
from auto_coder.management.original_attempt_evidence import derive, observe, verify
from auto_coder.management.original_attempts import OriginalEvidenceUnavailable
from tests.test_management_original_attempts import _covered, _fingerprint, _history, _terminal


def test_refused_original_cannot_be_reused_as_a_successful_predecessor(tmp_path):
    binding = _history(tmp_path)
    refused = _terminal(tmp_path, binding, "refused-parent")
    successor = replace(
        _covered(tmp_path, binding, "invalid-successor"),
        old_instance=refused.new_instance,
        old_container_id=refused.new_container_id,
    )
    before = _fingerprint(tmp_path)

    with pytest.raises(OriginalEvidenceUnavailable, match="uncovered, refused or cyclic original predecessor"):
        claim_fresh_initialization(Path(successor.host_state_root), successor)

    assert _fingerprint(tmp_path) == before
    assert list(Path(successor.host_state_root).iterdir()) == []
    assert list(Path(successor.host_journal_root).iterdir()) == []
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert evidence.unavailable == ()
        assert [attempt.target for attempt in evidence.attempts] == ["refused-parent"]
        assert verify(evidence, binding, fence) == evidence


_ORIGINAL_CLAIM_PROCESS = """
import json
import sys
from pathlib import Path
from auto_coder.management.fresh_initialization import claim_fresh_initialization, parse_fresh_intent
intent = parse_fresh_intent(json.loads(Path(sys.argv[1]).read_bytes()))
claim_fresh_initialization(Path(intent.host_state_root), intent)
"""


def test_separate_os_producers_capture_claimed_and_refused_successor_branches(tmp_path):
    """Native claim producers are real; grants/child readiness remain unproven."""
    binding = _history(tmp_path)
    first = _covered(tmp_path, binding, "first")
    claimed_child = replace(
        _covered(tmp_path, binding, "claimed-child"),
        old_instance=first.new_instance,
        old_container_id=first.new_container_id,
    )
    refused_sibling = replace(
        _covered(tmp_path, binding, "refused-sibling"),
        old_instance=first.new_instance,
        old_container_id=first.new_container_id,
        oauth_inode=first.oauth_inode + 1,
    )
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    for intent, success in ((first, True), (claimed_child, True), (refused_sibling, False)):
        request = tmp_path / (intent.new_instance + "-request.json")
        request.write_text(json.dumps(asdict(intent), sort_keys=True))
        result = subprocess.run(
            [sys.executable, "-B", "-c", _ORIGINAL_CLAIM_PROCESS, str(request)],
            text=True,
            capture_output=True,
            env=environment,
            timeout=30,
            check=False,
        )
        assert (result.returncode == 0) is success, result.stderr
        if not success:
            assert "retained OAuth root identity differs" in result.stderr

    binding_file = tmp_path / "binding.json"
    binding_file.write_text(json.dumps(asdict(binding), sort_keys=True))
    command = [sys.executable, "-B", "-m", "auto_coder.management.original_attempt_evidence", "--binding", str(binding_file)]
    before = _fingerprint(tmp_path)
    result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30, check=False)
    # Claimed is NOT launched: honest unavailability is the correct consumer result.
    assert result.returncode == 1, result.stderr
    produced = json.loads(result.stdout)
    assert [(item["target"], item["predecessor"], item["state"]) for item in produced["attempts"]] == [
        ("first", "old-instance", "claimed"),
        ("claimed-child", "first", "claimed"),
        ("refused-sibling", "first", "never-created"),
    ]
    assert len(produced["unavailable"]) == 2
    assert all("original grant boundary not crossed" in item for item in produced["unavailable"])
    assert _fingerprint(tmp_path) == before

    # A separate verifier must reject a plausible but truncated original frontier.
    submitted = tmp_path / "submitted.json"
    submitted.write_text(json.dumps({**produced, "attempts": produced["attempts"][:-1]}))
    before = _fingerprint(tmp_path)
    recheck = subprocess.run(
        [*command, "--submitted", str(submitted)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )
    assert recheck.returncode == 1
    assert "forged, stale, truncated or cross-deployment" in recheck.stderr
    assert _fingerprint(tmp_path) == before


def test_independent_consumer_rejects_failed_descendant_in_valid_hash_chain(tmp_path):
    """A test-only forged anchor is not a positive original admission oracle."""
    from auto_coder.management import original_attempts

    binding = _history(tmp_path)
    refused = _terminal(tmp_path, binding, "refused-parent")
    successor = replace(
        _covered(tmp_path, binding, "forged-successor"),
        old_instance=refused.new_instance,
        old_container_id=refused.new_container_id,
    )
    state = Path(successor.host_state_root)
    journal = Path(successor.host_journal_root)
    # Negative control bypasses admission and uses the genuine durable writer
    # to construct a well-formed digest chain with an impossible predecessor.
    with original_attempts.history_lock(Path(binding.root), exclusive=True):
        original_attempts._append(
            Path(binding.root),
            "anchor",
            successor.request_id,
            {
                "intent": asdict(successor),
                "lifetime": original_attempts._lifetime(),
                "stores": original_attempts.expected_stores(successor),
                "state": (state.stat().st_dev, state.stat().st_ino),
                "journal": (journal.stat().st_dev, journal.stat().st_ino),
                "pristine": True,
            },
        )
    before = _fingerprint(tmp_path)
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert any("original lineage has uncovered or refused predecessor" in reason for reason in evidence.unavailable)
        with pytest.raises(OriginalEvidenceUnavailable, match="original lineage has uncovered or refused predecessor"):
            verify(evidence, binding, fence)
    assert _fingerprint(tmp_path) == before
