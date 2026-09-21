"""Regression coverage for accepted-launch-specific Codex PR attribution."""

from dataclasses import replace

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository, resolve_codex_pr_origin, task_ids_from_text


def accepted_run(issue: int, attempt: int, task: str, ref: str) -> CloudRun:
    return CloudRun("owner/repo", issue, attempt, "codex-cloud", task_id=task, backend_name="codex-alias", submission_outcome="accepted", launch_identity=f"request-{issue}-{attempt}", publication_head_repository="owner/repo", publication_head_ref=ref)


def pr(number: int, issue: int, ref: str, body_extra: str = "") -> dict[str, object]:
    return {"number": number, "body": f"Closes #{issue}\n{body_extra}", "head": {"ref": ref, "repo": {"full_name": "owner/repo"}}}


def test_supported_task_urls_are_exact_and_normalized():
    task = "task_e_Ab19"
    text = " ".join(
        [
            f"https://chatgpt.com/codex/tasks/{task}",
            f"https://chat.openai.com/codex/cloud/tasks/{task}/?x=1#ok",
            "https://evil.example/codex/tasks/task_e_Evil",
            "https://chatgpt.com.evil.example/codex/tasks/task_e_Suffix",
            "https://user@chatgpt.com/codex/tasks/task_e_User",
            "https://chatgpt.com:443/codex/tasks/task_e_Port",
            "https://chatgpt.com/codex/tasks/task_e_Extra/more",
            "https://chatgpt.com/codex/tasks/task_fake",
        ]
    )
    assert task_ids_from_text(text) == {task}


def test_url_free_exact_publication_intent_establishes_durable_origin(tmp_path):
    runs = CloudRunRepository("owner/repo", tmp_path / "runs.json")
    bindings = CodexPrAttributionRepository("owner/repo", tmp_path / "bindings.json")
    run = accepted_run(2229, 0, "task_e_One", "issue-2229-attempt-0-codex-cloud")
    assert runs.save(run)
    result = resolve_codex_pr_origin("owner/repo", pr(31, 2229, run.publication_head_ref), runs, bindings)
    assert result.disposition is AttributionDisposition.VERIFIED
    assert result.origin is not None
    assert (result.origin.task_id, result.origin.issue_number, result.origin.backend_name, result.origin.attempt) == ("task_e_One", 2229, "codex-alias", 0)
    assert result.origin.evidence == "retained-publication-intent+closing-reference"
    assert result.consistency_token == "1"
    assert CodexPrAttributionRepository("owner/repo", tmp_path / "bindings.json").get(31) == result


def test_historical_binding_is_immutable_when_current_attempt_advances(tmp_path):
    runs = CloudRunRepository("owner/repo", tmp_path / "runs.json")
    bindings = CodexPrAttributionRepository("owner/repo", tmp_path / "bindings.json")
    first = accepted_run(7, 1, "task_e_First", "issue-7-attempt-1-codex-cloud")
    second = accepted_run(7, 2, "task_e_Second", "issue-7-attempt-2-codex-cloud")
    runs.save(first)
    original = resolve_codex_pr_origin("owner/repo", pr(40, 7, first.publication_head_ref), runs, bindings)
    runs.save(second)
    changed_metadata = pr(40, 7, second.publication_head_ref, "https://chatgpt.com/codex/tasks/task_e_Second")
    replay = resolve_codex_pr_origin("owner/repo", changed_metadata, runs, bindings)
    assert replay == original
    assert replay.origin is not None and replay.origin.task_id == "task_e_First"


def test_weak_evidence_stays_unresolved_and_conflicting_proof_is_explicit(tmp_path):
    runs = CloudRunRepository("owner/repo", tmp_path / "runs.json")
    bindings = CodexPrAttributionRepository("owner/repo", tmp_path / "bindings.json")
    runs.save(accepted_run(9, 0, "task_e_First", "expected-one"))
    runs.save(accepted_run(9, 1, "task_e_Second", "expected-two"))
    weak = resolve_codex_pr_origin("owner/repo", pr(50, 9, "guessed-branch"), runs, bindings)
    assert weak.disposition is AttributionDisposition.UNRESOLVED
    assert weak.boundary == "no coherent accepted task and qualifying PR publication proof"
    conflicting = pr(51, 9, "guessed-branch", "https://chatgpt.com/codex/tasks/task_e_First https://chat.openai.com/codex/cloud/tasks/task_e_Second")
    assert resolve_codex_pr_origin("owner/repo", conflicting, runs, bindings).disposition is AttributionDisposition.CONFLICT


def test_unaccepted_run_and_foreign_head_cannot_become_verified(tmp_path):
    runs = CloudRunRepository("owner/repo", tmp_path / "runs.json")
    bindings = CodexPrAttributionRepository("owner/repo", tmp_path / "bindings.json")
    run = replace(accepted_run(10, 0, "task_e_Pending", "expected"), submission_outcome="indeterminate")
    runs.save(run)
    metadata = pr(52, 10, "expected")
    metadata["head"] = {"ref": "expected", "repo": {"full_name": "fork/repo"}}
    assert resolve_codex_pr_origin("owner/repo", metadata, runs, bindings).disposition is AttributionDisposition.UNRESOLVED
