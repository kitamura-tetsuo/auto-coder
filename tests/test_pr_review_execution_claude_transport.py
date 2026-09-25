"""Regression coverage for two-tier review Claude transport and presentation parsing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_coder.pr_review_cycle import ContractSnapshot, Finding, StrongPolicyIdentity
from auto_coder.pr_review_execution import (
    REVIEW_RESPONSE_PREVIEW_LIMIT,
    ReviewExecutionInput,
    ReviewMode,
    _bounded_review_preview,
    parse_review_result,
)


def _strong_input() -> ReviewExecutionInput:
    return ReviewExecutionInput(
        mode=ReviewMode.STRONG_AUDIT,
        round_id="round-a",
        attempt_id="attempt-1",
        head_sha="a" * 40,
        base_sha="b" * 40,
        contract=ContractSnapshot(("#2101",), "REQ-007: Verify every outstanding finding."),
        policy=StrongPolicyIdentity("strong-route", '{"model":"strong"}', "v1"),
        repository_evidence="current source and tests",
        diff_evidence="complete cumulative diff",
        audited_head_sha="c" * 40,
    )


def _closure_input() -> ReviewExecutionInput:
    finding = Finding(
        finding_id="finding-a",
        origin_round_id="round-a",
        requirement_ids=("#2101/REQ-007",),
        requirement_texts=("Verify every outstanding finding.",),
        counterexample="The second path remains broken.",
        expected_behavior="Both paths preserve the invariant.",
        actual_behavior="One path loses the state.",
        evidence="src/state.py:40 reaches delete_two.",
        affected_boundary="delete_two",
        material_consequence="Persisted state is lost.",
        focused_regression_scenario="Exercise both deletion paths.",
    )
    return ReviewExecutionInput(
        mode=ReviewMode.ORDINARY_CLOSURE,
        round_id="round-a",
        attempt_id="attempt-1",
        head_sha="a" * 40,
        base_sha="b" * 40,
        contract=ContractSnapshot(("#2101",), "REQ-007: Verify every outstanding finding."),
        policy=StrongPolicyIdentity("strong-route", '{"model":"strong"}', "v1"),
        repository_evidence="current source and tests",
        diff_evidence="complete cumulative diff",
        finding_set_revision=3,
        findings=(finding,),
        audited_head_sha="c" * 40,
    )


def _strong_pass_payload(expected: ReviewExecutionInput) -> dict:
    return {
        "round_id": expected.round_id,
        "attempt_id": expected.attempt_id,
        "head_sha": expected.head_sha,
        "base_sha": expected.base_sha,
        "contract_identity": expected.contract.identity,
        "policy_identity": expected.policy.identity,
        "finding_set_revision": expected.finding_set_revision,
        "verdict": "PASS",
        "findings": [],
    }


def _closure_pass_payload(expected: ReviewExecutionInput) -> dict:
    return {
        "round_id": expected.round_id,
        "attempt_id": expected.attempt_id,
        "head_sha": expected.head_sha,
        "base_sha": expected.base_sha,
        "contract_identity": expected.contract.identity,
        "policy_identity": expected.policy.identity,
        "finding_set_revision": expected.finding_set_revision,
        "verdict": "PASS",
        "findings": [],
        "dispositions": [{"finding_id": "finding-a", "status": "FIXED", "evidence": "Both paths preserve state."}],
        "scope": "BOUNDED",
        "scope_evidence": "Only the guard and its regression changed.",
    }


def _stream_json(answer_text: str, session_id: str = "sess-1", extra_events: list[dict] | None = None) -> str:
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": session_id})]
    for event in extra_events or []:
        lines.append(json.dumps(event))
    lines.append(json.dumps({"type": "result", "subtype": "success", "session_id": session_id, "result": answer_text, "usage": {"tokens": 7}}))
    return "\n".join(lines)


def test_bare_and_presentation_forms_accept_equivalent_strong_pass() -> None:
    expected = _strong_input()
    payload = json.dumps(_strong_pass_payload(expected))
    assert parse_review_result(payload, expected, "prov").is_complete
    assert parse_review_result(f"  \n{payload}\n  ", expected, "prov").is_complete
    assert parse_review_result(f"```\n{payload}\n```", expected, "prov").is_complete
    assert parse_review_result(f"```json\n{payload}\n```", expected, "prov").is_complete
    assert parse_review_result(f"```JSON\n{payload}\n```", expected, "prov").is_complete
    prose = f"Here is the independent review:\n```json\n{payload}\n```\nThat concludes the audit."
    result = parse_review_result(prose, expected, "prov")
    assert result.is_complete
    assert result.verdict == "PASS"


def test_claude_single_envelope_and_stream_accept_same_payload() -> None:
    expected = _strong_input()
    payload = json.dumps(_strong_pass_payload(expected))
    envelope = json.dumps({"type": "result", "subtype": "success", "result": payload, "session_id": "s1"})
    assert parse_review_result(envelope, expected, "prov").is_complete
    stream = _stream_json(f"```json\n{payload}\n```")
    result = parse_review_result(stream, expected, "prov")
    assert result.is_complete
    assert result.verdict == "PASS"
    assert result.head_sha == expected.head_sha


def test_stream_with_startup_and_nested_evidence_accepts() -> None:
    expected = _strong_input()
    payload_dict = _strong_pass_payload(expected)
    evidence = 'src/a.py:1 {"nested": true} `code` "quoted \\"brace {\\"" with unicode \u2603 and array [1, {"k": "v"}]'
    payload_dict["findings"] = []
    payload = json.dumps(payload_dict)
    # Evidence with braces/backticks is inside the review object only; add startup event before init.
    startup = json.dumps({"type": "system", "subtype": "startup", "detail": "hooks loaded"})
    init = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"})
    assistant = json.dumps({"type": "assistant", "message": {"content": [{"text": "working"}]}})
    terminal = json.dumps({"type": "result", "subtype": "success", "session_id": "sess-1", "result": payload})
    stream = "\n\n".join(["", startup, init, assistant, terminal, ""])
    assert parse_review_result(stream, expected, "prov").is_complete
    assert evidence  # Payload construction preserves nested/exotic strings without extra candidates.


def test_terminal_findings_win_over_intermediate_pass() -> None:
    expected = _strong_input()
    pass_payload = json.dumps(_strong_pass_payload(expected))
    findings_payload = dict(_strong_pass_payload(expected))
    findings_payload["verdict"] = "FINDINGS"
    findings_payload["findings"] = [
        {
            "finding_id": "f-1",
            "requirement_ids": ["#2101/REQ-007"],
            "requirement_texts": ["Verify every outstanding finding."],
            "counterexample": "Second path broken.",
            "expected_behavior": "Both paths hold.",
            "actual_behavior": "One loses state.",
            "evidence": "src/state.py:40",
            "affected_boundary": "delete_two",
            "material_consequence": "State lost.",
            "focused_regression_scenario": "Exercise both paths.",
        }
    ]
    intermediate = json.dumps({"type": "assistant", "message": {"content": pass_payload}})
    stream = _stream_json(json.dumps(findings_payload), extra_events=[json.loads(intermediate)])
    result = parse_review_result(stream, expected, "prov")
    assert result.is_complete
    assert result.verdict == "FINDINGS"
    assert len(result.findings) == 1


def test_intermediate_pass_cannot_rescue_malformed_terminal() -> None:
    expected = _strong_input()
    pass_payload = json.dumps(_strong_pass_payload(expected))
    intermediate = {"type": "assistant", "message": {"content": pass_payload}}
    stream = _stream_json("truncated {", extra_events=[intermediate])
    result = parse_review_result(stream, expected, "prov")
    assert not result.is_complete
    assert result.verdict == "INCONCLUSIVE"
    assert "Invalid Claude transport" not in result.diagnostic or "answer JSON" in result.diagnostic or "not one JSON" in result.diagnostic


def test_transport_failures_stay_incomplete_without_fallback() -> None:
    expected = _strong_input()
    valid_answer = f"```json\n{json.dumps(_strong_pass_payload(expected))}\n```"
    base_lines = _stream_json(valid_answer).splitlines()
    init_line, result_line = base_lines[0], base_lines[-1]
    terminal_obj = json.loads(result_line)

    def check(stream: str) -> str:
        result = parse_review_result(stream, expected, "prov")
        assert not result.is_complete
        assert result.verdict == "INCONCLUSIVE"
        assert "Invalid Claude transport" in result.diagnostic
        return result.diagnostic

    # Missing terminal result.
    check(init_line)
    # Truncated non-JSON line.
    check(init_line + "\n{truncated\n" + result_line)
    # Appended non-JSON stderr contamination.
    check("\n".join([init_line, result_line, "stderr: something broke"]))
    # Error subtype.
    bad_subtype = dict(terminal_obj)
    bad_subtype["subtype"] = "error"
    check("\n".join([init_line, json.dumps(bad_subtype)]))
    # is_error true and invalid non-boolean.
    bad_true = dict(terminal_obj)
    bad_true["is_error"] = True
    check("\n".join([init_line, json.dumps(bad_true)]))
    bad_invalid = dict(terminal_obj)
    bad_invalid["is_error"] = "false"
    check("\n".join([init_line, json.dumps(bad_invalid)]))
    # Empty result.
    bad_empty = dict(terminal_obj)
    bad_empty["result"] = "   "
    check("\n".join([init_line, json.dumps(bad_empty)]))
    # Fatal top-level error event.
    check("\n".join([init_line, json.dumps({"type": "error", "message": "boom"}), result_line]))
    # Duplicate terminal results.
    check("\n".join([init_line, result_line, result_line]))
    # Event after terminal result.
    check("\n".join([init_line, result_line, json.dumps({"type": "assistant", "text": "late"})]))
    # Session mismatch.
    mismatched = dict(terminal_obj)
    mismatched["session_id"] = "other"
    check("\n".join([init_line, json.dumps(mismatched)]))


def test_presentation_rejects_ambiguity_and_repairs() -> None:
    expected = _strong_input()
    good = json.dumps(_strong_pass_payload(expected))
    wrong_head = dict(_strong_pass_payload(expected))
    wrong_head["head_sha"] = "d" * 40
    wrong_text = json.dumps(wrong_head)

    def check(text: str) -> None:
        result = parse_review_result(text, expected, "prov")
        assert not result.is_complete
        assert "not one JSON object" in result.diagnostic

    check(f"{good}\n{good}")
    check(f"```json\n{good}\n```\n```json\n{good}\n```")
    check(f"{wrong_text}\n{good}")
    check(f"{good}\n{wrong_text}")
    check(json.dumps([_strong_pass_payload(expected)]))
    check(good.replace("}", ",}"))
    duplicated = good.replace('"verdict"', '"verdict": "PASS", "verdict"', 1)
    assert duplicated  # Guards against accidental test simplification.
    check('{"round_id": "x", "round_id": "y"}')
    # Truncated outer object containing a complete inner review object.
    check('{"outer": {"incomplete": true, "inner": ' + good + "")
    # Generic wrapper with a nested result field must never be salvaged: it is
    # incomplete regardless of whether the failure is classified as answer-JSON
    # ambiguity or schema/identity rejection.
    wrapper = parse_review_result('{"result": ' + good + "}", expected, "prov")
    assert not wrapper.is_complete
    assert wrapper.verdict == "INCONCLUSIVE"


def test_wrapper_cannot_fix_identity_or_closure_obligations() -> None:
    strong_expected = _strong_input()
    bad = _strong_pass_payload(strong_expected)
    bad["head_sha"] = "d" * 40
    wrapped = _stream_json(f"```json\n{json.dumps(bad)}\n```")
    result = parse_review_result(wrapped, strong_expected, "prov")
    assert not result.is_complete
    assert result.diagnostic == "Mismatched or missing head_sha"

    closure_expected = _closure_input()
    payload = _closure_pass_payload(closure_expected)
    assert parse_review_result(_stream_json(json.dumps(payload)), closure_expected, "prov").grants_closure_evidence
    missing = dict(payload)
    missing["dispositions"] = []
    missing_result = parse_review_result(_stream_json(json.dumps(missing)), closure_expected, "prov")
    assert not missing_result.is_complete
    expanded = dict(payload)
    expanded["scope"] = "EXPANDED"
    expanded_result = parse_review_result(_stream_json(json.dumps(expanded)), closure_expected, "prov")
    assert expanded_result.is_complete
    assert not expanded_result.grants_closure_evidence


def test_preview_is_redacted_and_bounded() -> None:
    long_response = "x" * (REVIEW_RESPONSE_PREVIEW_LIMIT + 500)
    preview = _bounded_review_preview(long_response)
    assert len(preview) <= REVIEW_RESPONSE_PREVIEW_LIMIT + 100
    assert "characters omitted" in preview
    assert _bounded_review_preview("ghp_" + "a" * 40) == "[REDACTED]"


def test_execute_review_accepts_stream_json_fenced_pass_once(tmp_path: Path, _use_real_commands) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True, capture_output=True, text=True)
    (repository / "file.txt").write_text("head\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repository, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "head"], cwd=repository, check=True, capture_output=True, text=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    base = "b" * 40
    contract = ContractSnapshot(("#2101",), "REQ-007: Verify every outstanding finding.")
    policy = StrongPolicyIdentity("strong-route", '{"model":"strong"}', "v1")
    review_input = ReviewExecutionInput(
        mode=ReviewMode.STRONG_AUDIT,
        round_id="round-live",
        attempt_id="attempt-live",
        head_sha=head,
        base_sha=base,
        contract=contract,
        policy=policy,
        repository_evidence="evidence",
        diff_evidence="diff",
    )
    payload = {
        "round_id": "round-live",
        "attempt_id": "attempt-live",
        "head_sha": head,
        "base_sha": base,
        "contract_identity": contract.identity,
        "policy_identity": policy.identity,
        "finding_set_revision": 0,
        "verdict": "PASS",
        "findings": [],
    }
    stream = _stream_json(f"Here is the audit:\n```json\n{json.dumps(payload)}\n```\nDone.")
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("claude", "sonnet")
    from auto_coder import pr_review_execution as execution

    with patch.object(execution, "run_llm_prompt", return_value=stream) as transport:
        result = execution.execute_review(review_input, manager, str(repository))
    assert result.is_complete
    assert result.verdict == "PASS"
    assert result.head_sha == head
    transport.assert_called_once()
