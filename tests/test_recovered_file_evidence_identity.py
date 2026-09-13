"""Regression coverage for cross-head recovered changed-file evidence."""

from auto_coder.adversarial_validator import (
    AdversarialValidationContext,
    AdversarialValidationResult,
    _reconcile_reusable_recovered_evidence,
)
from auto_coder.reviewer_session_registry import RecoveredFileEvidence, ReviewerSession


def _entry(path: str, identity: str, *, status: str = "RECOVERED") -> RecoveredFileEvidence:
    return RecoveredFileEvidence(
        path=path,
        source="GitHub REST recovery",
        status=status,
        evidence="verified via authoritative record",
        requirement_ids=["REQ-001"],
        identity_version=1,
        change_identity=identity,
        requirement_manifest_identity="manifest-1",
        origin_head_sha="head-1",
        origin_validation_snapshot="snapshot-1",
        scope_basis_identity="basis-1" if status == "IRRELEVANT" else "",
    )


def _session(*entries: RecoveredFileEvidence) -> ReviewerSession:
    return ReviewerSession(
        repository="owner/repo",
        pr_number=2049,
        backend_name="reviewer",
        backend_type="codex",
        model_name="strong",
        session_id="session",
        evidence_head_sha="head-1",
        recovered_file_evidence=list(entries),
    )


def test_cross_head_reuse_keeps_only_equivalent_recovered_file() -> None:
    stored = _session(_entry("src/a.py", "old-a"), _entry("src/b.py", "same-b"))
    context = AdversarialValidationContext(
        unverified_files=["src/a.py", "src/b.py"],
        file_change_identities={"src/a.py": "new-a", "src/b.py": "same-b"},
        requirement_manifest_identity="manifest-1",
    )

    result = _reconcile_reusable_recovered_evidence(AdversarialValidationResult(result="PASS"), stored, context, "head-2")

    assert [(entry.path, entry.provenance, entry.origin_head_sha) for entry in result.evidence_recovery] == [("src/b.py", "REUSED_EQUIVALENT", "head-1")]


def test_manifest_change_irrelevance_and_legacy_entries_fail_closed() -> None:
    stored = _session(
        _entry("src/recovered.py", "same"),
        _entry("src/irrelevant.py", "same", status="IRRELEVANT"),
        RecoveredFileEvidence(path="src/legacy.py", status="RECOVERED"),
    )
    context = AdversarialValidationContext(
        unverified_files=["src/recovered.py", "src/irrelevant.py", "src/legacy.py"],
        file_change_identities={path: "same" for path in ("src/recovered.py", "src/irrelevant.py", "src/legacy.py")},
        requirement_manifest_identity="manifest-2",
    )

    result = _reconcile_reusable_recovered_evidence(AdversarialValidationResult(result="PASS"), stored, context, "head-2")

    assert result.evidence_recovery == []
