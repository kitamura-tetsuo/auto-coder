"""Regression coverage for accepted-launch-specific Codex PR attribution."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository, resolve_codex_pr_origin, task_ids_from_text
from auto_coder.issue_processor import _process_issue_codex_cloud_mode
from auto_coder.issue_stage_routing import ImplementationRetryRequest
from auto_coder.pr_processor import process_pull_request
from auto_coder.retry_dispatch import RetryDispatchRepository


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


def test_historical_binding_exposes_new_qualifying_conflict_without_rebinding(tmp_path):
    runs = CloudRunRepository("owner/repo", tmp_path / "runs.json")
    bindings = CodexPrAttributionRepository("owner/repo", tmp_path / "bindings.json")
    first = accepted_run(7, 1, "task_e_First", "issue-7-attempt-1-codex-cloud")
    second = accepted_run(7, 2, "task_e_Second", "issue-7-attempt-2-codex-cloud")
    runs.save(first)
    original = resolve_codex_pr_origin("owner/repo", pr(40, 7, first.publication_head_ref), runs, bindings)
    runs.save(second)
    changed_metadata = pr(40, 7, second.publication_head_ref, "https://chatgpt.com/codex/tasks/task_e_Second")
    replay = resolve_codex_pr_origin("owner/repo", changed_metadata, runs, bindings)
    assert replay.disposition is AttributionDisposition.CONFLICT
    assert replay.origin is not None and replay.origin.task_id == "task_e_First"
    assert bindings.get(40) == original
    removed_proof = resolve_codex_pr_origin("owner/repo", pr(40, 7, "unrelated-head"), runs, bindings)
    assert removed_proof == original


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


def test_initial_dispatch_persists_intent_before_submit_and_recovers_accepted_receipt(tmp_path, monkeypatch):
    """REQ-002: the real dispatch boundary never submits before durable intent."""
    monkeypatch.setenv("HOME", str(tmp_path))
    issue = {"number": 2229, "title": "Attribute PR", "body": "Implement it", "labels": []}
    retry = ImplementationRetryRequest(
        "request-2229",
        "owner/repo",
        2229,
        "generation-1",
        "attempt-1",
        "owned",
        "execution-1",
        predecessor_captured=True,
    )

    with (
        patch("auto_coder.codex_cloud_client.CodexCloudClient") as client_type,
        patch("auto_coder.issue_processor.get_current_attempt", return_value=0),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
        patch("auto_coder.cloud_run.CloudRunRepository.acquire_submission_claim", side_effect=OSError("intent unavailable")),
    ):
        blocked = _process_issue_codex_cloud_mode("owner/repo", issue, AutomationConfig(), MagicMock(), "codex-alias")

    client_type.return_value.submit_task.assert_not_called()
    assert blocked == ["Deferred Codex Cloud task for issue #2229: could not persist submission claim: intent unavailable"]
    assert CloudRunRepository("owner/repo").get(2229, 0) is None

    receipts = RetryDispatchRepository("owner/repo")
    receipts.claim(retry, "codex-cloud", "codex-alias", {"base_branch": "main"})
    receipt = receipts.allocate_numeric_attempt(retry.request_id, [2])
    assert receipt.numeric_attempt == 3
    receipts.record_outcome(
        retry.request_id,
        "accepted",
        external_id="task_e_Accepted",
        external_url="https://chatgpt.com/codex/tasks/task_e_Accepted",
        environment_id="environment-retained",
    )

    with (
        patch("auto_coder.codex_cloud_client.CodexCloudClient") as replay_client_type,
        patch("auto_coder.issue_processor.get_current_attempt", return_value=2),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
        patch("auto_coder.issue_processor._durable_retry_authority", return_value=retry),
    ):
        recovered = _process_issue_codex_cloud_mode("owner/repo", issue, AutomationConfig(), MagicMock(), "codex-alias", retry_authority=retry)

    replay_client_type.return_value.submit_task.assert_not_called()
    assert recovered == ["Codex Cloud task 'task_e_Accepted' already accepted for retry attempt-1; skipped duplicate dispatch"]
    run = CloudRunRepository("owner/repo").get(2229, 3)
    assert run is not None
    assert (run.task_id, run.launch_identity, run.publication_head_repository, run.publication_head_ref) == ("task_e_Accepted", retry.request_id, "", "")
    unresolved = resolve_codex_pr_origin(
        "owner/repo",
        pr(60, 2229, "issue-2229-attempt-3-codex-cloud"),
        CloudRunRepository("owner/repo"),
        CodexPrAttributionRepository("owner/repo"),
    )
    assert unresolved.disposition is AttributionDisposition.UNRESOLVED


def test_duplicate_accepted_task_id_with_conflicting_launches_is_conflict(tmp_path):
    for reverse in (False, True):
        suffix = "reverse" if reverse else "forward"
        runs = CloudRunRepository("owner/repo", tmp_path / f"runs-{suffix}.json")
        bindings = CodexPrAttributionRepository("owner/repo", tmp_path / f"bindings-{suffix}.json")
        candidates = [
            accepted_run(12, 1, "task_e_Duplicate", "head-one"),
            replace(accepted_run(12, 2, "task_e_Duplicate", "head-two"), backend_name="other-backend"),
        ]
        for run in reversed(candidates) if reverse else candidates:
            runs.save(run)
        metadata = pr(61, 12, "unrelated", "https://chatgpt.com/codex/tasks/task_e_Duplicate")
        result = resolve_codex_pr_origin("owner/repo", metadata, runs, bindings)
        assert result.disposition is AttributionDisposition.CONFLICT
        assert bindings.get(61).disposition is AttributionDisposition.UNRESOLVED


def test_real_pr_processing_verifies_url_free_intent_without_retargeting_jules(tmp_path, monkeypatch):
    """REQ-007: a production PR pass persists URL-free accepted attribution."""
    monkeypatch.setattr("auto_coder.cloud_run.Path.home", lambda: tmp_path)
    monkeypatch.setattr("auto_coder.codex_pr_attribution.Path.home", lambda: tmp_path)
    monkeypatch.setattr("auto_coder.cloud_manager.Path.home", lambda: tmp_path)
    repository = "owner/repo"
    run = accepted_run(2229, 4, "task_e_Published", "issue-2229-attempt-4-codex-cloud")
    CloudRunRepository(repository).save(run)
    manager = CloudManager(repository)
    legacy = CloudTaskBinding("jules", "legacy-jules-session", "jules")
    assert manager.ensure_binding(2229, legacy)
    metadata = {
        **pr(2237, 2229, run.publication_head_ref),
        "state": "open",
        "user": {"login": "codex"},
        "changed_files": 1,
        "head": {"ref": run.publication_head_ref, "sha": "head-sha", "repo": {"full_name": repository}},
        "base": {"ref": "main", "sha": "base-sha"},
    }

    class FreshMetadataClient:
        def __init__(self) -> None:
            self.strict_reads = 0
            self.live = metadata
            self.update_attempts = []
            self.write_error = None
            self.apply_before_error = False

        def get_pull_request_metadata_strict(self, repo_name: str, pr_number: int) -> dict[str, object]:
            assert (repo_name, pr_number) == (repository, 2237)
            self.strict_reads += 1
            return self.live

        def get_repository(self, repo_name: str):
            assert repo_name == repository
            return ProjectionRepository(self)

    github = FreshMetadataClient()
    closed = MagicMock(closed=True, actions=[], issue_numbers=())
    with patch("auto_coder.pr_processor._close_empty_pr", return_value=closed):
        process_pull_request(github, AutomationConfig(), repository, {"number": 2237})

    assert github.strict_reads == 2
    assert github.update_attempts == ["Closes #2229\n\nhttps://chatgpt.com/codex/tasks/task_e_Published"]
    established = CodexPrAttributionRepository(repository).get(2237)
    assert established.disposition is AttributionDisposition.VERIFIED
    assert established.origin is not None
    assert (established.origin.task_id, established.origin.provider, established.origin.backend_name, established.origin.attempt) == (
        "task_e_Published",
        "codex-cloud",
        "codex-alias",
        4,
    )
    assert established.origin.evidence == "retained-publication-intent+closing-reference"
    assert established.consistency_token == "1"
    assert manager.get_binding(2229) == legacy


class ProjectionPull:
    def __init__(self, client):
        self.client = client

    def edit(self, *, body):
        self.client.update_attempts.append(body)
        if self.client.write_error is not None:
            if self.client.apply_before_error:
                self.client.live["body"] = body
            raise self.client.write_error
        self.client.live["body"] = body
        return {"body": body}


class ProjectionRepository:
    def __init__(self, client):
        self.client = client

    def get_pull(self, number):
        assert number == self.client.live["number"]
        return ProjectionPull(self.client)


class ProjectionGitHub:
    def __init__(self, live):
        self.live = live
        self.reads = 0
        self.update_attempts = []
        self.write_error = None
        self.apply_before_error = False

    def get_pull_request_metadata_strict(self, repository, number):
        assert (repository, number) == ("owner/repo", self.live["number"])
        self.reads += 1
        return {**self.live, "head": dict(self.live["head"])}

    def get_repository(self, repository):
        assert repository == "owner/repo"
        return ProjectionRepository(self)


def _projection_metadata(body="Summary\n\nTesting\n\nCloses #2230"):
    return {
        "number": 77,
        "body": body,
        "head": {"ref": "issue-2230-attempt-0-codex-cloud", "repo": {"full_name": "owner/repo"}},
    }


def _prepare_projection(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_coder.cloud_run.Path.home", lambda: tmp_path)
    monkeypatch.setattr("auto_coder.codex_pr_attribution.Path.home", lambda: tmp_path)
    run = accepted_run(2230, 0, "task_e_Projected77", "issue-2230-attempt-0-codex-cloud")
    CloudRunRepository("owner/repo").save(run)


def test_verified_projection_uses_fresh_body_and_is_idempotent(tmp_path, monkeypatch):
    """REQ-001/002/003: only the exact durable origin reaches a fresh PR body."""
    from auto_coder.pr_processor import _link_codex_cloud_pr_to_issue

    _prepare_projection(tmp_path, monkeypatch)
    stale = _projection_metadata("Old summary\n\nCloses #2230")
    github = ProjectionGitHub(_projection_metadata("Author's new summary\n\nTesting retained\n\nCloses #2230"))

    first = _link_codex_cloud_pr_to_issue("owner/repo", stale, github)
    expected = "Author's new summary\n\nTesting retained\n\nCloses #2230\n\nhttps://chatgpt.com/codex/tasks/task_e_Projected77"
    assert (first.status, first.confirmed, first.confirmed_body) == ("updated", True, expected)
    assert github.live["body"] == expected
    assert stale["body"] == expected
    assert github.update_attempts == [expected]

    github.live["body"] = "Author edit\n\nCloses #2230\n\n[task](https://chat.openai.com/codex/cloud/tasks/task_e_Projected77/?view=1#turn)"
    second = _link_codex_cloud_pr_to_issue("owner/repo", stale, github)
    assert second.status == "present"
    assert stale["body"] == github.live["body"]
    assert github.update_attempts == [expected]


def test_projection_failure_and_ambiguous_write_recover_after_restart(tmp_path, monkeypatch):
    """REQ-004/005: failed publication is not success and durable ownership retries."""
    from auto_coder.pr_processor import _link_codex_cloud_pr_to_issue

    _prepare_projection(tmp_path, monkeypatch)
    original = _projection_metadata()
    github = ProjectionGitHub(original)
    github.write_error = RuntimeError("response lost")
    github.apply_before_error = True

    failed = _link_codex_cloud_pr_to_issue("owner/repo", dict(original), github)
    assert failed.status == "failed"
    assert not failed.confirmed
    assert len(github.update_attempts) == 1

    reconstructed = ProjectionGitHub(dict(github.live))
    recovered = _link_codex_cloud_pr_to_issue("owner/repo", {"number": 77, "body": "stale"}, reconstructed)
    assert recovered.status == "present"
    assert reconstructed.update_attempts == []
    origin = CodexPrAttributionRepository("owner/repo").get(77).origin
    assert origin is not None
    assert (origin.task_id, origin.attempt) == ("task_e_Projected77", 0)


def test_projection_without_verified_origin_never_guesses_from_issue_state(tmp_path, monkeypatch):
    """REQ-001/005: current pointers and manual URLs cannot create PR ownership."""
    from auto_coder.pr_processor import _link_codex_cloud_pr_to_issue

    monkeypatch.setattr("auto_coder.cloud_run.Path.home", lambda: tmp_path)
    monkeypatch.setattr("auto_coder.codex_pr_attribution.Path.home", lambda: tmp_path)
    live = _projection_metadata("Closes #2230\n\nhttps://example.com/codex/tasks/task_e_Manual")
    github = ProjectionGitHub(live)

    result = _link_codex_cloud_pr_to_issue("owner/repo", dict(live), github)
    assert result.status == "deferred"
    assert "UNRESOLVED" in result.diagnostic
    assert github.update_attempts == []
