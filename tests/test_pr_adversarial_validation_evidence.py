"""Production-origin regressions for Issue #2433 (attempt-bound validation evidence).

Every scenario starts at the real PR-processing entry (``_handle_pr_merge``) and
runs the real native attempt allocation, context resolution, manifest builder,
prompt assembly, ``BackendManager`` interaction recording, response
normalization/parsing, deterministic coverage check and ``ReviewAuditStore``
persistence. Only true I/O boundaries are controlled: the GitHub client, the
worktree checkout and the reviewer backend's raw response text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from auto_coder.execution_trace import get_trace_collector
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_processor import _handle_pr_merge
from auto_coder.review_audit import ReviewAuditStore, StorageHealth, ValidationEvidenceReadStatus
from auto_coder.review_capture import validation_evidence
from auto_coder.review_capture.pr_adversarial_audit import PrAdversarialReviewTarget, record_reused
from tests.test_pr_adversarial_review_audit import (  # noqa: F401  (audit_store is a fixture)
    NON_JSON_PAYLOAD,
    PASS_PAYLOAD,
    PR_NUMBER,
    REPO_NAME,
    MockReviewerClient,
    _apply_standard_merge_gates,
    _build_backend_manager,
    _build_config,
    _build_github_client,
    _build_pr_data,
    _build_pr_repo,
    _static_worktree,
    _wire_backend,
    audit_store,
)


@pytest.fixture(autouse=True)
def _real_commands(_use_real_commands):
    """These tests drive real git boundaries; never stub them."""


BASE_SHA = "b" * 40


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _issue_body(count: int, prefix: str = "Requirement") -> str:
    lines = "\n".join(f"REQ-{index:03d}: {prefix} number {index} holds." for index in range(1, count + 1))
    return f"## Objective\nExercise attempt-bound evidence.\n\n## Requirements\n{lines}\n"


def _verified_payload(ids: List[str]) -> str:
    coverage = [{"requirement_id": requirement_id, "status": "VERIFIED", "evidence": "sample.py: verified."} for requirement_id in ids]
    return json.dumps({"result": "PASS", "summary": "All requirements verified.", "findings": [], "requirement_coverage": coverage, "specification_gaps": [], "test_oracle_gaps": [], "thread_dispositions": [], "dynamic_check_requested": None})


class RecordingReviewer(MockReviewerClient):
    """Reviewer double that also keeps the exact prompts it received."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompts: List[str] = []

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        self.prompts.append(prompt)
        return super()._run_llm_cli(prompt, is_noedit=is_noedit)

    def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
        self.prompts.append(prompt)
        return super().continue_session(session_id, prompt, is_noedit=is_noedit)


class Scenario:
    """One real ``_handle_pr_merge`` run wired to scripted reviewer responses."""

    def __init__(self, tmp_path, monkeypatch, store: ReviewAuditStore, *, issue_body: str, responses: List[str], session_id: Optional[str] = None, reviewer: Optional[RecordingReviewer] = None):
        self.store = store
        self.repo, self.head_sha = _build_pr_repo(tmp_path)
        self.issue_body = issue_body
        self.client = _build_github_client(self.head_sha, issue_body=issue_body)
        self.pr_data = _build_pr_data(self.head_sha)
        self.pr_data["base"] = {"ref": "main", "sha": BASE_SHA}
        self.config = _build_config()
        self.reviewer = reviewer or RecordingReviewer("reviewer", responses=responses, session_id=session_id)
        self.manager = _build_backend_manager(monkeypatch, {"reviewer": self.reviewer}, "reviewer")
        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, self.manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(self.repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "COMMENT", ""))

    def run(self):
        return _handle_pr_merge(self.client, REPO_NAME, self.pr_data, self.config, {})

    def evaluation(self):
        records = self.store.get_recent_history(REPO_NAME, limit=50).records
        assert len(records) == 1
        return records[0]

    def evidence(self):
        record = self.evaluation()
        found = self.store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=record.native_report["attempt_id"])
        assert found.status == ValidationEvidenceReadStatus.AVAILABLE, found.status
        assert found.producer is not None
        return record, found.producer.payload


def _by_id(items: List[dict], key: str, value: str) -> dict:
    return next(item for item in items if item[key] == value)


# ---------------------------------------------------------------------------
# AS-001: returned VERIFIED entries rejected as unknown IDs
# ---------------------------------------------------------------------------


def test_twelve_unqualified_verified_entries_are_diagnosable_and_not_accepted(tmp_path, monkeypatch, audit_store):
    body = _issue_body(12)
    unqualified = [f"REQ-{index:03d}" for index in range(1, 13)]
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=body, responses=[_verified_payload(unqualified)])

    scenario.run()

    record, payload = scenario.evidence()
    qualified = [f"#99/REQ-{index:03d}" for index in range(1, 13)]

    # The real checker still rejects the response; twelve VERIFIED entries are not twelve accepted requirements.
    assert record.native_verdict == "ERROR"
    assert record.native_report["diagnostic_category"] == "unknown_requirement_coverage_id"

    assert payload["completeness"] == "complete" and payload["unrecorded"] == []
    identity = payload["identity"]
    assert identity["attempt_id"] == record.native_report["attempt_id"]
    assert identity["attempt_sequence"] == record.native_report["attempt_sequence"] >= 1
    assert identity["review_id"] == record.review_id
    assert identity["repository"] == REPO_NAME and identity["pr_number"] == PR_NUMBER
    assert identity["head_sha"] == scenario.head_sha and identity["base_sha"] == BASE_SHA
    assert identity["process_run_id"] == get_trace_collector().process_run_id
    assert payload["build"]["process_run_id"] == identity["process_run_id"]
    assert payload["build"]["source_revision"]["available"] is False and payload["build"]["source_revision"]["reason"]
    assert "execution_id" in identity["unavailable"]  # no execution scope is bound in this production path

    consumed = payload["input"]
    assert consumed["pr_body"]["sha256"] == _sha(scenario.pr_data["body"]) and consumed["pr_body"]["byte_length"] == len(scenario.pr_data["body"].encode())
    assert [(issue["repository"], issue["number"]) for issue in consumed["resolved_issues"]] == [(REPO_NAME, 99)]
    assert consumed["resolved_issues"][0]["body"] == {"state": "present", "sha256": _sha(body), "byte_length": len(body.encode())}
    assert consumed["resolved_issues"][0]["source_updated_at"] is None  # not supplied by the retrieval: unknown, not guessed
    assert consumed["resolved_issues"][0]["retrieval_mode"] == "default"
    assert consumed["capture_time_is_not_freshness"] is True
    assert consumed["linked_issue_context"]["sha256"]

    supplied = _by_id(payload["manifests"], "role", "supplied")
    checked = _by_id(payload["manifests"], "role", "checked")
    assert [entry["id"] for entry in supplied["entries"]] == qualified == [entry["id"] for entry in checked["entries"]]
    assert supplied["count"] == 12 and supplied["mode"] == "explicit-contract"
    assert supplied["identity_sha256"] == checked["identity_sha256"]
    assert supplied["validation_snapshot"] == checked["validation_snapshot"] and len(supplied["validation_snapshot"]) == 64
    assert supplied["entries"][0]["text"]["sha256"] == _sha("REQ-001: Requirement number 1 holds.".split(": ", 1)[1])

    response = payload["responses"][0]
    assert response["stage"] == "initial" and response["response_id"] == "r1"
    assert response["prompt"]["sha256"] == _sha(scenario.reviewer.prompts[0]) and response["prompt"]["byte_length"] == len(scenario.reviewer.prompts[0].encode())
    assert response["manifest_transmitted"] is True and response["supplied_manifest_id"] == supplied["manifest_id"]
    assert response["response"]["sha256"] == _sha(_verified_payload(unqualified)) and response["response_state"] == "nonempty"
    assert response["semantic_payload"] is None  # direct JSON: normalization changed nothing
    assert response["parse"]["state"] == "parsed" and response["parse"]["parsed_verdict"] == "PASS"
    assert [(entry["id"], entry["status"]) for entry in response["parse"]["returned_entries"]] == [(rid, "VERIFIED") for rid in unqualified]
    interaction = response["interaction"]
    assert interaction["association"] == "verified_review_scoped_interaction_records"
    assert interaction["interaction_ids"] == [record.interactions[0].interaction_id]
    assert interaction["backend_alias"] == "reviewer" and interaction["requested_model"] == "reviewer-model" and interaction["reported_model"] is None

    first = payload["coverage_checks"][0]
    assert first["performed"] is True and first["response_id"] == "r1"
    assert first["expected_ids"] == qualified and first["returned_ids"] == unqualified
    assert first["unknown_ids"] == unqualified and first["missing_ids"] == qualified and first["duplicate_ids"] == []
    assert first["returned_count"] == 12 and first["counts_are_returned_evidence_not_acceptance"] is True
    assert first["verdict_before"] == "PASS" and first["verdict_after"] == "ERROR"
    assert first["diagnostic_category"] == "unknown_requirement_coverage_id"
    assert first["supplied_manifest_id"] == supplied["manifest_id"] and first["checked_manifest_id"] == checked["manifest_id"]
    # The later final interpretation does not recompute a hypothetical accepted result.
    assert payload["coverage_checks"][-1]["performed"] is False and payload["coverage_checks"][-1]["not_performed_reason"] == "result_already_error_with_diagnostic"

    final = payload["final"]
    assert final["kind"] == "semantic_response" and final["verdict"] == "ERROR" and final["source_response_id"] == "r1"
    assert final["diagnostic_category"] == "unknown_requirement_coverage_id"

    by_sequence = scenario.store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_sequence=identity["attempt_sequence"])
    assert by_sequence.producer is not None and by_sequence.producer.review_id == record.review_id


def test_qualified_pass_response_records_an_accepted_check(tmp_path, monkeypatch, audit_store):
    qualified = [f"#99/REQ-{index:03d}" for index in range(1, 4)]
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=_issue_body(3), responses=[_verified_payload(qualified)])

    scenario.run()

    record, payload = scenario.evidence()
    assert record.native_verdict == "PASS"
    check = payload["coverage_checks"][0]
    assert check["unknown_ids"] == [] and check["missing_ids"] == [] and check["verdict_after"] == "PASS"
    assert payload["final"]["verdict"] == "PASS" and payload["final"]["kind"] == "semantic_response"


# ---------------------------------------------------------------------------
# AS-002: overlapping attempts cannot mix evidence
# ---------------------------------------------------------------------------


class GatedReviewer(RecordingReviewer):
    """Reviewer double whose response is held until the test releases it."""

    def __init__(self, name: str, response: str):
        super().__init__(name, responses=[response])
        self.entered = threading.Event()
        self.release = threading.Event()

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        self.prompts.append(prompt)
        self.entered.set()
        assert self.release.wait(timeout=60), "test never released the reviewer"
        return self._next()


def test_overlapping_attempts_keep_their_own_consumed_inputs(tmp_path, monkeypatch, audit_store):
    first_body, second_body = _issue_body(2, "First"), _issue_body(2, "Second")
    repo, head_sha = _build_pr_repo(tmp_path)
    client = _build_github_client(head_sha, issue_body=first_body)
    pr_data = _build_pr_data(head_sha)
    config = _build_config()
    first_reviewer = GatedReviewer("first", _verified_payload([f"#99/REQ-{i:03d}" for i in (1, 2)]))
    second_reviewer = GatedReviewer("second", _verified_payload(["REQ-001"]))
    managers = [_build_backend_manager(monkeypatch, {"first": first_reviewer}, "first"), _build_backend_manager(monkeypatch, {"second": second_reviewer}, "second")]
    handed_out: List[object] = []
    handout_lock = threading.Lock()

    def _next_manager(validation_kind=None):
        from auto_coder.cli_helpers import AdversarialValidationAvailability

        with handout_lock:
            manager = managers[len(handed_out)]
            handed_out.append(manager)
        return AdversarialValidationAvailability(backend_manager=manager)

    _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
    monkeypatch.setattr("auto_coder.cli_helpers.resolve_adversarial_validation_availability", _next_manager)
    monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
    monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "COMMENT", ""))

    errors: List[BaseException] = []

    def _run():
        try:
            _handle_pr_merge(client, REPO_NAME, pr_data, config, {})
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    first = threading.Thread(target=_run)
    first.start()
    assert first_reviewer.entered.wait(timeout=60)
    # The Issue changes after the first attempt consumed it, before the second starts.
    client.get_issue.return_value = {"number": 99, "title": "Add greet()", "body": second_body, "state": "open"}
    second = threading.Thread(target=_run)
    second.start()
    assert second_reviewer.entered.wait(timeout=60)
    # Both attempts are now at the reviewer boundary: release in reverse order.
    second_reviewer.release.set()
    second.join(timeout=60)
    first_reviewer.release.set()
    first.join(timeout=60)
    assert not errors and not first.is_alive() and not second.is_alive()

    records = audit_store.get_recent_history(REPO_NAME, limit=10).records
    assert len(records) == 2
    payloads: Dict[str, dict] = {}
    for record in records:
        found = audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=record.native_report["attempt_id"])
        assert found.producer is not None and found.producer.review_id == record.review_id
        payloads[found.producer.payload["manifests"][0]["entries"][0]["text"]["sha256"]] = found.producer.payload
    first_payload = payloads[_sha("First number 1 holds.")]
    second_payload = payloads[_sha("Second number 1 holds.")]

    assert first_payload["identity"]["attempt_id"] != second_payload["identity"]["attempt_id"]
    assert first_payload["identity"]["attempt_sequence"] != second_payload["identity"]["attempt_sequence"]
    assert first_payload["identity"]["review_id"] != second_payload["identity"]["review_id"]
    assert first_payload["input"]["resolved_issues"][0]["body"]["sha256"] == _sha(first_body)
    assert second_payload["input"]["resolved_issues"][0]["body"]["sha256"] == _sha(second_body)
    assert first_payload["responses"][0]["prompt"]["sha256"] == _sha(first_reviewer.prompts[0])
    assert second_payload["responses"][0]["prompt"]["sha256"] == _sha(second_reviewer.prompts[0])
    assert first_payload["responses"][0]["response"]["sha256"] != second_payload["responses"][0]["response"]["sha256"]
    assert first_payload["final"]["verdict"] == "PASS" and second_payload["final"]["verdict"] == "ERROR"
    assert first_payload["manifests"][0]["identity_sha256"] != second_payload["manifests"][0]["identity_sha256"]
    assert first_payload["responses"][0]["interaction"]["backend_alias"] == "first"
    assert second_payload["responses"][0]["interaction"]["backend_alias"] == "second"


# ---------------------------------------------------------------------------
# AS-003: follow-ups, normalization and refusal keep their own response identity
# ---------------------------------------------------------------------------


def _codex_envelope(message: str) -> str:
    return "\n".join([json.dumps({"type": "thread.started", "thread_id": "t"}), json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": message}})])


def test_cli_envelope_normalization_keeps_raw_and_semantic_fingerprints(tmp_path, monkeypatch, audit_store):
    envelope = _codex_envelope(PASS_PAYLOAD)
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=_issue_body(1), responses=[envelope])

    scenario.run()

    record, payload = scenario.evidence()
    response = payload["responses"][0]
    assert record.native_verdict == "PASS"
    assert response["response"]["sha256"] == _sha(envelope)
    assert response["semantic_payload"]["sha256"] == _sha(PASS_PAYLOAD) != response["response"]["sha256"]
    assert [entry["id"] for entry in response["parse"]["returned_entries"]] == ["#99/REQ-001"]


def test_malformed_response_is_not_an_empty_successful_coverage_set(tmp_path, monkeypatch, audit_store):
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=_issue_body(1), responses=[NON_JSON_PAYLOAD])

    scenario.run()

    record, payload = scenario.evidence()
    response = payload["responses"][0]
    assert response["parse"]["state"] == "failed" and response["parse"]["failure_category"]
    assert response["parse"]["returned_entries_observed"] is False and response["parse"]["returned_entries"] == []
    assert response["response"]["sha256"] == _sha(NON_JSON_PAYLOAD)
    assert payload["coverage_checks"][0]["performed"] is False
    assert payload["final"]["verdict"] == record.native_verdict != "PASS"


def test_duplicate_returned_ids_are_retained_as_parsed(tmp_path, monkeypatch, audit_store):
    ids = ["#99/REQ-001", "#99/REQ-001"]
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=_issue_body(1), responses=[_verified_payload(ids)])

    scenario.run()

    _record, payload = scenario.evidence()
    response = payload["responses"][0]
    assert response["parse"]["state"] == "failed"
    assert [entry["id"] for entry in response["parse"]["returned_entries"]] == ids
    assert payload["final"]["kind"] == "semantic_response"


def test_dynamic_followup_gets_its_own_response_and_check(tmp_path, monkeypatch, audit_store):
    from tests.test_pr_adversarial_review_audit import _add_dynamic_check_script

    repo, head_sha = _build_pr_repo(tmp_path)
    head_sha = _add_dynamic_check_script(repo, head_sha)
    ids = ["#99/REQ-001"]
    initial = json.loads(_verified_payload(ids))
    initial["dynamic_check_requested"] = "tests/test_sample.py::test_greet"
    reviewer = RecordingReviewer("reviewer", responses=[json.dumps(initial), _verified_payload(["REQ-001"])], session_id="session-S")
    scenario = Scenario(tmp_path / "unused", monkeypatch, audit_store, issue_body=_issue_body(1), responses=[], reviewer=reviewer)
    scenario.repo, scenario.head_sha = repo, head_sha
    scenario.client = _build_github_client(head_sha, issue_body=_issue_body(1))
    scenario.pr_data = _build_pr_data(head_sha)
    scenario.config.TEST_SCRIPT_PATH = str(repo / "scripts" / "test.sh")
    monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))

    scenario.run()

    record, payload = scenario.evidence()
    first, second = payload["responses"]
    assert (first["stage"], second["stage"]) == ("initial", "dynamic_check_followup")
    assert first["response"]["sha256"] != second["response"]["sha256"] and first["prompt"]["sha256"] != second["prompt"]["sha256"]
    assert second["manifest_transmitted"] is True and second["supplied_manifest_id"] == first["supplied_manifest_id"]
    assert first["interaction"]["interaction_ids"] == [record.interactions[0].interaction_id]
    assert second["interaction"]["interaction_ids"] == [record.interactions[1].interaction_id]
    # The earlier favorable coverage is never attached to the later (unknown-ID) response.
    assert first["parse"]["parsed_verdict"] == "PASS" and second["parse"]["parsed_verdict"] == "PASS"
    performed = [check for check in payload["coverage_checks"] if check["performed"]]
    assert [check["response_id"] for check in performed][:2] == ["r1", "r2"]
    assert performed[0]["unknown_ids"] == [] and performed[1]["unknown_ids"] == ["REQ-001"]
    assert payload["final"]["source_response_id"] == "r2" and payload["final"]["verdict"] == "ERROR"


def test_target_correction_continuation_names_the_manifest_it_continues(tmp_path, monkeypatch, audit_store):
    ids = ["#99/REQ-001"]
    initial = json.loads(_verified_payload(ids))
    initial["dynamic_check_requested"] = "tests/test_missing_target.py::test_missing"
    corrected = json.loads(_verified_payload(ids))
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=_issue_body(1), responses=[json.dumps(initial), json.dumps(corrected)], session_id="session-S")

    scenario.run()

    _record, payload = scenario.evidence()
    initial_response, correction = payload["responses"][0], payload["responses"][1]
    assert correction["stage"] == "target_correction"
    assert correction["manifest_transmitted"] is False and correction["supplied_manifest_id"] is None
    assert correction["continues_manifest_id"] == initial_response["supplied_manifest_id"]
    assert correction["prompt"]["sha256"] == _sha(scenario.reviewer.prompts[1])


def test_local_refusal_before_invocation_has_no_response(tmp_path, monkeypatch, audit_store):
    duplicate = "## Objective\nExercise.\n\n## Requirements\nREQ-001: greet() returns hello.\nREQ-001: greet() must not raise.\n"
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=duplicate, responses=[])
    scenario.manager = MagicMock()
    scenario.manager.get_current_backend_identity.side_effect = AssertionError("no backend before the manifest gate")
    _wire_backend(monkeypatch, scenario.manager)

    scenario.run()

    record, payload = scenario.evidence()
    assert record.interactions == []
    assert payload["responses"] == [] and payload["coverage_checks"] == []
    assert payload["input"]["resolved_issues"][0]["body"]["sha256"] == _sha(duplicate)
    assert payload["final"]["kind"] == "local_without_semantic_response" and payload["final"]["verdict"] == "BLOCKED"
    assert payload["final"]["source_response_id"] is None


def test_backend_fallback_attributes_the_response_to_the_producing_backend(tmp_path, monkeypatch, audit_store):
    from auto_coder.exceptions import AutoCoderUsageLimitError

    repo, head_sha = _build_pr_repo(tmp_path)
    client = _build_github_client(head_sha, issue_body=_issue_body(1))
    pr_data = _build_pr_data(head_sha)
    reviewer_a = RecordingReviewer("reviewer-a", raise_once=AutoCoderUsageLimitError("quota exceeded"))
    reviewer_b = RecordingReviewer("reviewer-b", responses=[PASS_PAYLOAD])
    manager = _build_backend_manager(monkeypatch, {"reviewer-a": reviewer_a, "reviewer-b": reviewer_b}, "reviewer-a")
    _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
    _wire_backend(monkeypatch, manager)
    monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
    monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "COMMENT", ""))

    _handle_pr_merge(client, REPO_NAME, pr_data, _build_config(), {})

    record = audit_store.get_recent_history(REPO_NAME, limit=10).records[0]
    found = audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=record.native_report["attempt_id"])
    interaction = found.producer.payload["responses"][0]["interaction"]
    assert interaction["interaction_ids"] == [i.interaction_id for i in record.interactions] and len(interaction["interaction_ids"]) == 2
    assert interaction["backend_alias"] == "reviewer-b" and interaction["requested_model"] == "reviewer-b-model"
    assert interaction["reported_model"] is None


# ---------------------------------------------------------------------------
# AS-004: partial, historical and unavailable evidence stay distinguishable
# ---------------------------------------------------------------------------


def test_input_capture_is_partial_until_the_response_completes(tmp_path, monkeypatch, audit_store):
    repo, head_sha = _build_pr_repo(tmp_path)
    client = _build_github_client(head_sha, issue_body=_issue_body(1))
    pr_data = _build_pr_data(head_sha)
    reviewer = GatedReviewer("held", PASS_PAYLOAD)
    manager = _build_backend_manager(monkeypatch, {"held": reviewer}, "held")
    _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
    _wire_backend(monkeypatch, manager)
    monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
    monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "COMMENT", ""))
    worker = threading.Thread(target=lambda: _handle_pr_merge(client, REPO_NAME, pr_data, _build_config(), {}))
    worker.start()
    try:
        assert reviewer.entered.wait(timeout=60)
        # A fresh reader on the same audit root, as after a controller restart.
        fresh = ReviewAuditStore(audit_root=audit_store._audit_root)
        running = fresh.get_recent_history(REPO_NAME, limit=10).records[0]
        found = fresh.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_sequence=1)
        assert found.status == ValidationEvidenceReadStatus.AVAILABLE and found.producer is not None
        partial = found.producer.payload
        assert found.producer.review_id == running.review_id and found.producer.completeness == "partial"
        assert partial["completeness"] == "partial" and {"response", "final_result"} <= set(partial["unrecorded"])
        assert partial["input"]["resolved_issues"][0]["body"]["sha256"] == _sha(_issue_body(1))
        assert partial["responses"][0]["prompt"]["sha256"] == _sha(reviewer.prompts[0])
        assert partial["responses"][0]["response_state"] == "pending" and partial["responses"][0]["response"]["state"] == "unavailable"
        assert "final" not in partial
    finally:
        reviewer.release.set()
        worker.join(timeout=60)
    completed = ReviewAuditStore(audit_root=audit_store._audit_root).get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_sequence=1)
    assert completed.producer.completeness == "complete" and completed.producer.payload["final"]["verdict"] == "PASS"


def test_reuse_observation_references_but_never_replaces_the_producer(tmp_path, monkeypatch, audit_store):
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=_issue_body(1), responses=[PASS_PAYLOAD])
    scenario.run()
    record, producer_payload = scenario.evidence()
    attempt_id, sequence = record.native_report["attempt_id"], record.native_report["attempt_sequence"]
    target = PrAdversarialReviewTarget(REPO_NAME, PR_NUMBER, scenario.head_sha)

    reuse_review_id = record_reused(target, policy_identity="p", source_review_id=None, native_verdict="PASS", attempt_id=attempt_id, attempt_sequence=sequence)
    unrelated_review_id = record_reused(target, policy_identity="p", source_review_id=None, native_verdict="PASS", attempt_id="not-a-recorded-attempt", attempt_sequence=99)

    from auto_coder.review_audit import ReviewEffectRecord

    audit_store.record_effect(REPO_NAME, ReviewEffectRecord(review_id=record.review_id, effect_id="e1", observation_time="t", disposition="confirmed"))
    found = audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=attempt_id)
    assert found.producer.review_id == record.review_id and found.producer.payload == producer_payload
    assert [row.review_id for row in found.reuse_observations] == [reuse_review_id]
    reuse = found.reuse_observations[0].payload["reuse"]
    assert reuse == {"source_review_id": record.review_id, "source_known": True, "fresh_invocation": False}
    # The production publication effect and the added one stay attributed to the producing review row.
    assert {effect.review_id for effect in found.effects} == {record.review_id}
    assert "e1" in {effect.effect_id for effect in found.effects}
    unknown = audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id="not-a-recorded-attempt")
    assert unknown.producer is None and unknown.reuse_observations[0].review_id == unrelated_review_id
    assert unknown.reuse_observations[0].payload["reuse"]["source_known"] is False


def test_legacy_missing_unsupported_and_corrupt_reads_are_distinct(tmp_path, monkeypatch, audit_store):
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=_issue_body(1), responses=[PASS_PAYLOAD])
    scenario.run()
    record, _payload = scenario.evidence()
    attempt_id = record.native_report["attempt_id"]
    db_path = audit_store._get_db_path(REPO_NAME)

    assert audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id="other").status == ValidationEvidenceReadStatus.NOT_FOUND
    assert audit_store.get_validation_evidence("other/repo", "1", attempt_id="x").status == ValidationEvidenceReadStatus.UNINITIALIZED

    connection = sqlite3.connect(db_path)
    with connection:
        connection.execute("UPDATE validation_evidence SET payload = 'not json'")
    assert audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=attempt_id).status == ValidationEvidenceReadStatus.CORRUPT
    with connection:
        connection.execute("UPDATE validation_evidence SET payload = '{}', schema_version = 99")
    assert audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=attempt_id).status == ValidationEvidenceReadStatus.UNSUPPORTED_SCHEMA
    with connection:
        connection.execute("DELETE FROM validation_evidence")
    # A pre-feature evaluation retained this native attempt, but not its extension.
    assert audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=attempt_id).status == ValidationEvidenceReadStatus.PRE_FEATURE
    with connection:
        connection.execute("DROP TABLE validation_evidence")
    connection.close()
    assert audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=attempt_id).status == ValidationEvidenceReadStatus.PRE_FEATURE
    monkeypatch.setattr(audit_store, "_connect_readonly", lambda repository: (None, StorageHealth.UNAVAILABLE))
    assert audit_store.get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=attempt_id).status == ValidationEvidenceReadStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# AS-005: limits and redaction
# ---------------------------------------------------------------------------


def test_oversized_and_sensitive_inputs_are_bounded_redacted_and_not_archived(tmp_path, monkeypatch, audit_store):
    secret = "configured-secret-value-12345"
    monkeypatch.setenv("REVIEW_SERVICE_API_TOKEN", secret)
    token = "ghp_" + "a" * 36
    marker = "UNIQUE-ISSUE-BODY-MARKER"
    body = _issue_body(600, marker)
    unknown = ["/etc/passwd", "https://example.com/private/path", "C:\\Users\\victim\\file.txt", f"id-{token}", f"id-{secret}", "x" * 3000] + [f"UNKNOWN-{index:04d}" for index in range(700)]
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=body, responses=[_verified_payload(unknown)])

    scenario.run()

    record, payload = scenario.evidence()
    found_row = sqlite3.connect(audit_store._get_db_path(REPO_NAME)).execute("SELECT payload FROM validation_evidence").fetchone()[0]
    retained = found_row.encode("utf-8")

    assert len(retained) <= validation_evidence.MAX_RECORD_BYTES
    for forbidden in (secret, token, marker, "/etc/passwd", "example.com/private", "victim", scenario.reviewer.prompts[0][:200], PASS_PAYLOAD):
        assert forbidden.encode() not in retained
    manifests = {m["role"]: m for m in payload["manifests"]}
    assert manifests["supplied"]["count"] == 600 and len(manifests["supplied"]["entries"]) <= validation_evidence.MAX_ENTRIES
    limits = payload["limits"]
    sections = {omission["section"]: omission for omission in limits["omissions"]}
    supplied_entries = sections["manifests.supplied-1.entries"]
    assert supplied_entries["source_count"] == 600 and supplied_entries["omitted"] == 600 - supplied_entries["retained"]
    # The digest still describes the complete (pre-clipping) manifest.
    expected_identity = validation_evidence.manifest_identity([(f"#99/REQ-{i:03d}", f"{marker} number {i} holds.") for i in range(1, 601)])
    assert manifests["supplied"]["identity_sha256"] == expected_identity
    returned = payload["responses"][0]["parse"]
    assert returned["returned_entry_count"] == len(unknown)
    omitted_identities = [entry["id"] for entry in returned["returned_entries"] if isinstance(entry["id"], dict)]
    assert omitted_identities and all(item["omitted"] and item["sha256"] and item["byte_length"] >= 0 for item in omitted_identities)
    reasons = {item["reason"] for item in omitted_identities}
    assert {"path_or_url_like", "redacted", "too_long"} <= reasons
    assert returned["returned_entry_count"] > len(returned["returned_entries"])  # the retained list is capped, and says so
    assert sections["responses.r1.returned_entries"]["source_count"] == len(unknown)
    assert all("value" not in item for item in omitted_identities)  # an omitted identity never carries a value
    final = payload["final"]
    assert len(final["diagnostic_reason"]) <= validation_evidence.MAX_TEXT_CHARS
    assert record.native_verdict == "ERROR" and final["verdict"] == "ERROR"


def test_text_is_redacted_before_it_is_clipped(tmp_path):
    token = "ghp_" + "b" * 36
    recorder = validation_evidence.AttemptEvidenceRecorder(validation_evidence.AttemptIdentity(repository=REPO_NAME, pr_number=1, review_id="r", attempt_id="a", attempt_sequence=1), validation_evidence.observe_controller_artifact())
    # The token straddles the 2000-char clip boundary: clipping first would leak a prefix.
    reason = "x" * (validation_evidence.MAX_TEXT_CHARS - 10) + token + " tail"
    recorder.observe_final(validation_evidence.FinalObservation(kind="local_without_semantic_response", verdict="ERROR", diagnostic_reason=reason))
    rendered = json.dumps(recorder.render_bounded())
    assert "ghp_" not in rendered
    assert "final.diagnostic_reason" in recorder.render_bounded()["limits"]["clipped_fields"] or "[REDACTED]" in rendered


def test_identity_changed_by_redaction_is_omitted_not_rewritten():
    limits = validation_evidence._Limits(10, 2000, ["super-secret-credential"])
    assert limits.identity("REQ-001") == "REQ-001"
    assert limits.identity("#99/REQ-001") == "#99/REQ-001"
    omitted = limits.identity("REQ-super-secret-credential")
    assert omitted["omitted"] is True and omitted["reason"] == "redacted" and "value" not in omitted
    assert limits.identity("src/auto_coder/secret.py")["reason"] == "path_or_url_like"


# ---------------------------------------------------------------------------
# AS-006: instrumentation stays outside validation authority
# ---------------------------------------------------------------------------


def _scenario_outcome(tmp_path, monkeypatch, store, *, kind: str):
    issue = _issue_body(2)
    ids = [f"#99/REQ-{i:03d}" for i in (1, 2)]
    if kind == "pass":
        scenario = Scenario(tmp_path, monkeypatch, store, issue_body=issue, responses=[_verified_payload(ids)])
    elif kind == "unknown":
        scenario = Scenario(tmp_path, monkeypatch, store, issue_body=issue, responses=[_verified_payload(["REQ-001", "REQ-002"])])
    elif kind == "exception":
        scenario = Scenario(tmp_path, monkeypatch, store, issue_body=issue, responses=[], reviewer=RecordingReviewer("reviewer", raise_once=RuntimeError("boom")))
    else:
        scenario = Scenario(tmp_path, monkeypatch, store, issue_body="## Objective\nx\n\n## Requirements\nREQ-001: a.\nREQ-001: b.\n", responses=[])
        scenario.manager = MagicMock()
        scenario.manager.get_current_backend_identity.side_effect = AssertionError("no backend")
        _wire_backend(monkeypatch, scenario.manager)
    actions = scenario.run()
    evaluation = scenario.evaluation()
    github_calls = [call[0] for call in scenario.client.method_calls]
    # Each run builds its own checkout, so only the head SHA legitimately differs.
    normalized_actions = [action.replace(scenario.head_sha[:8], "<head>") for action in actions]
    prompts = [prompt.replace(scenario.head_sha, "<head>") for prompt in scenario.reviewer.prompts]
    return scenario, (normalized_actions, prompts, list(scenario.reviewer.calls), evaluation.native_verdict, evaluation.native_report["diagnostic_category"], evaluation.execution_mode, github_calls)


@pytest.mark.parametrize("kind", ["pass", "unknown", "exception", "refusal"])
def test_recorder_health_never_changes_validation_behavior(tmp_path, monkeypatch, kind):
    outcomes = {}
    for mode in ("healthy", "write_failure", "unavailable", "size_limited"):
        with pytest.MonkeyPatch.context() as mp:
            store = ReviewAuditStore(audit_root=tmp_path / mode / "audit")
            mp.setattr("auto_coder.review_capture.recorder._global_audit_store", store)
            if mode == "write_failure":
                mp.setattr(store, "record_validation_evidence", MagicMock(side_effect=RuntimeError("disk full")))
            elif mode == "unavailable":
                mp.setattr(store, "record_validation_evidence", lambda *a, **k: False)
                mp.setattr(store, "get_validation_evidence", MagicMock(side_effect=AssertionError("diagnostic reads must not run during validation")))
            elif mode == "size_limited":
                mp.setattr(validation_evidence, "MAX_RECORD_BYTES", 3000)
            scenario, outcome = _scenario_outcome(tmp_path / mode, mp, store, kind=kind)
            outcomes[mode] = outcome
            if mode in ("healthy", "size_limited"):
                record = scenario.evaluation()
                found = ReviewAuditStore(audit_root=store._audit_root).get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_id=record.native_report["attempt_id"])
                payload = found.producer.payload
                assert len(json.dumps(payload).encode()) <= (validation_evidence.MAX_RECORD_BYTES if mode == "healthy" else 8000)
                if mode == "size_limited" and kind in ("pass", "unknown"):
                    assert payload["limits"]["omissions"] or payload["limits"].get("incomplete_reason")
                assert payload["final"]["verdict"] == record.native_verdict
                if kind == "exception":
                    assert payload["final"]["kind"] == "exception"
    assert outcomes["write_failure"] == outcomes["healthy"] == outcomes["unavailable"] == outcomes["size_limited"]
    if kind == "pass":
        assert outcomes["healthy"][3] == "PASS" and outcomes["healthy"][2] == ["fresh"]
    if kind == "refusal":
        assert outcomes["healthy"][2] == []


# ---------------------------------------------------------------------------
# Review follow-ups: failed follow-up attribution, model verdict, exact interaction
# association and PR source metadata
# ---------------------------------------------------------------------------


def _dynamic_followup_scenario(tmp_path, monkeypatch, store, reviewer: RecordingReviewer) -> Scenario:
    from tests.test_pr_adversarial_review_audit import _add_dynamic_check_script

    repo, head_sha = _build_pr_repo(tmp_path)
    head_sha = _add_dynamic_check_script(repo, head_sha)
    scenario = Scenario(tmp_path / "unused", monkeypatch, store, issue_body=_issue_body(1), responses=[], reviewer=reviewer)
    scenario.repo, scenario.head_sha = repo, head_sha
    scenario.client = _build_github_client(head_sha, issue_body=_issue_body(1))
    scenario.pr_data = _build_pr_data(head_sha)
    scenario.config.TEST_SCRIPT_PATH = str(repo / "scripts" / "test.sh")
    monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
    return scenario


def _initial_with_dynamic_check() -> str:
    initial = json.loads(_verified_payload(["#99/REQ-001"]))
    initial["dynamic_check_requested"] = "tests/test_sample.py::test_greet"
    return json.dumps(initial)


def test_failed_followup_does_not_borrow_the_initial_response(tmp_path, monkeypatch, audit_store):
    class FailingFollowup(RecordingReviewer):
        def continue_session(self, session_id, prompt, is_noedit=False):
            self.prompts.append(prompt)
            self.calls.append("continue")
            raise RuntimeError("follow-up transport failed")

    reviewer = FailingFollowup("reviewer", responses=[_initial_with_dynamic_check()], session_id="session-S")
    scenario = _dynamic_followup_scenario(tmp_path, monkeypatch, audit_store, reviewer)

    scenario.run()

    record, payload = scenario.evidence()
    first, second = payload["responses"]
    assert record.native_verdict == "BLOCKED"
    assert first["parse"]["state"] == "parsed" and first["response_state"] == "nonempty"  # r1 stays as historical evidence
    assert second["stage"] == "dynamic_check_followup" and second["response_state"] == "unavailable"
    assert second["response"]["state"] == "unavailable"
    final = payload["final"]
    assert final["verdict"] == "BLOCKED"
    assert final["kind"] == "local_without_semantic_response" and final["source_response_id"] is None
    assert payload["coverage_checks"][-1]["response_id"] is None  # the final interpretation is not attached to r1's coverage


def test_model_verdict_is_kept_apart_from_parser_finding_precedence(tmp_path, monkeypatch, audit_store):
    payload_json = json.loads(_verified_payload(["#99/REQ-001"]))
    payload_json["findings"] = [
        {
            "finding_identity": "greet-return-value",
            "correction_identity": "greet-return-value-fix",
            "violated_requirement": "greet() returns the string hello",
            "requirement_id": "#99/REQ-001",
            "evidence_classification": "DEMONSTRATED",
            "reachability": "greet() is called directly",
            "required_behavior": "greet() returns hello",
            "actual_behavior": "greet() returns another string",
            "evidence": "sample.py",
            "counterexample": "greet() returns hell0",
            "test_gap": "No assertion exists",
            "suggested_regression_scenario": "Assert greet() == 'hello'",
            "anchor_path": "sample.py",
        }
    ]
    scenario = Scenario(tmp_path, monkeypatch, audit_store, issue_body=_issue_body(1), responses=[json.dumps(payload_json)])

    scenario.run()

    record, payload = scenario.evidence()
    parse = payload["responses"][0]["parse"]
    assert parse["parsed_verdict"] == "PASS"  # what the model said
    assert parse["post_parse_verdict"] == "NEEDS_FIX"  # after the controller's finding precedence
    assert payload["coverage_checks"][0]["verdict_after"] == "NEEDS_FIX" == payload["final"]["verdict"] == record.native_verdict


def test_interaction_association_is_never_guessed_from_unassigned_records(tmp_path, monkeypatch, audit_store):
    reviewer = RecordingReviewer("reviewer", responses=[_initial_with_dynamic_check(), _verified_payload(["#99/REQ-001"])], session_id="session-S")
    scenario = _dynamic_followup_scenario(tmp_path, monkeypatch, audit_store, reviewer)
    real_get = audit_store.get_evaluation
    real_record = audit_store.record_interaction
    reads = {"count": 0}
    seen_interactions: List[str] = []

    def flaky_get(repository, review_id):
        reads["count"] += 1
        if reads["count"] == 2:  # the initial response's post-invocation read
            raise RuntimeError("transient read failure")
        return real_get(repository, review_id)

    def flaky_record(repository, interaction, credentials=None):
        if interaction.interaction_id not in seen_interactions:
            seen_interactions.append(interaction.interaction_id)
        if interaction.interaction_id == (seen_interactions[1] if len(seen_interactions) > 1 else None):
            return False  # the follow-up interaction write is lost
        return real_record(repository, interaction, credentials)

    monkeypatch.setattr(audit_store, "get_evaluation", flaky_get)
    monkeypatch.setattr(audit_store, "record_interaction", flaky_record)

    scenario.run()

    monkeypatch.undo()
    found = ReviewAuditStore(audit_root=audit_store._audit_root).get_validation_evidence(REPO_NAME, str(PR_NUMBER), attempt_sequence=1)
    first, second = found.producer.payload["responses"]
    assert first["interaction"]["association"] == "unavailable" and first["interaction"]["backend_alias"] is None
    assert second["interaction"]["association"] == "unavailable" and second["interaction"]["interaction_ids"] == []
    assert second["interaction"]["backend_alias"] is None and second["interaction"]["requested_model"] is None


def test_pr_source_updated_at_is_retained_or_explicitly_unknown(tmp_path, monkeypatch, audit_store):
    with_metadata = Scenario(tmp_path / "with", monkeypatch, audit_store, issue_body=_issue_body(1), responses=[PASS_PAYLOAD])
    with_metadata.pr_data["updated_at"] = "2026-01-02T03:04:05Z"
    with_metadata.run()
    _record, payload = with_metadata.evidence()
    assert payload["input"]["pr_source_updated_at"] == "2026-01-02T03:04:05Z"
    assert payload["input"]["pr_source_updated_at"] != payload["input"]["captured_at"]

    other_store = ReviewAuditStore(audit_root=tmp_path / "other-audit")
    monkeypatch.setattr("auto_coder.review_capture.recorder._global_audit_store", other_store)
    without = Scenario(tmp_path / "without", monkeypatch, other_store, issue_body=_issue_body(1), responses=[PASS_PAYLOAD])
    without.run()
    _record, payload = without.evidence()
    assert payload["input"]["pr_source_updated_at"] is None
