import json
import pytest
from unittest.mock import Mock
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle, ValidationIdentity, _contract_evidence
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult

BODY = "## Requirements\n- REQ-001: Return the current value."


def test_legacy_record_with_evidence_reuse(tmp_path):
    path = tmp_path / "decisions.json"
    history_path = tmp_path / "individual_review_history.json"
    manifest = build_normative_issue_manifest(1728, "Title", BODY)

    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    first = SpecificationValidationLifecycle("owner/repo", "provider/model", path, calls)

    decision1 = first.decide(manifest, "Title", BODY)
    assert calls.call_count == 1

    state = json.loads(path.read_text())

    del state[decision1.identity.key]["semantic_policy_contract"]

    import hashlib

    contract = _contract_evidence(manifest, "Title", BODY)
    import auto_coder.specification_validation_lifecycle as sv

    c = sv.validation_policy_contract()
    c["provider"] = "provider/model"
    old_policy_id = hashlib.sha256(json.dumps(c, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    old_identity_dict = {"repository": "owner/repo", "issue_number": 1728, "specification_digest": decision1.identity.specification_digest, "policy_identity": old_policy_id, "relationship_digest": decision1.identity.relationship_digest}
    old_key = hashlib.sha256(json.dumps(old_identity_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    legacy_record = state[decision1.identity.key]
    legacy_record["identity"] = old_identity_dict
    del state[decision1.identity.key]
    state[old_key] = legacy_record

    path.write_text(json.dumps(state))

    restarted_calls = Mock(side_effect=AssertionError("must reuse"))
    import os

    os.environ["AUTO_CODER_SPECIFICATION_VALIDATOR_IDENTITY"] = "provider/model"
    restarted = SpecificationValidationLifecycle("owner/repo", "provider/model", path, restarted_calls)
    decision2 = restarted.decide(manifest, "Title", BODY)

    assert decision2.verdict == "READY"
    assert decision2.evaluation_source == "stored-decision-reuse"


def test_legacy_record_without_evidence_forces_new_review(tmp_path):
    path = tmp_path / "decisions.json"
    history_path = tmp_path / "individual_review_history.json"
    manifest = build_normative_issue_manifest(1728, "Title", BODY)

    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    first = SpecificationValidationLifecycle("owner/repo", "provider/model", path, calls)

    decision1 = first.decide(manifest, "Title", BODY)

    state = json.loads(path.read_text())
    del state[decision1.identity.key]["semantic_policy_contract"]

    import hashlib

    contract = _contract_evidence(manifest, "Title", BODY)
    import auto_coder.specification_validation_lifecycle as sv

    c = sv.validation_policy_contract()
    c["provider"] = "provider/model"
    old_policy_id = hashlib.sha256(json.dumps(c, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    old_identity_dict = {"repository": "owner/repo", "issue_number": 1728, "specification_digest": decision1.identity.specification_digest, "policy_identity": old_policy_id, "relationship_digest": decision1.identity.relationship_digest}
    old_key = hashlib.sha256(json.dumps(old_identity_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    legacy_record = state[decision1.identity.key]
    legacy_record["identity"] = old_identity_dict
    del state[decision1.identity.key]
    state[old_key] = legacy_record

    path.write_text(json.dumps(state))

    history = json.loads(history_path.read_text())
    history["1728"]["baseline"] = '{"mismatched": true}'
    history_path.write_text(json.dumps(history))

    restarted_calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    import os

    os.environ["AUTO_CODER_SPECIFICATION_VALIDATOR_IDENTITY"] = "provider/model"
    restarted = SpecificationValidationLifecycle("owner/repo", "provider/model", path, restarted_calls)

    decision2 = restarted.decide(manifest, "Title", BODY)

    assert decision2.verdict == "READY"
    assert restarted_calls.call_count == 0
