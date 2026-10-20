"""Deterministic contract checks for the opt-in live smoke harness.

These tests intentionally do not claim to be the credentialed live E2E.
"""

import importlib.util
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "live_local_pr_repair_smoke.py"
SPEC = importlib.util.spec_from_file_location("live_local_pr_repair_smoke", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


@pytest.mark.parametrize("repository", sorted(smoke.FORBIDDEN_REPOSITORIES))
def test_production_repository_is_refused_before_live_work(repository, tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(smoke.Blocked, match="must not"):
        smoke.validate_target(repository, tmp_path)


def test_ci_requires_terminal_failure_on_captured_head():
    snapshot = smoke.Snapshot("h1", "b1", True, (7,), "success")
    with pytest.raises(smoke.Blocked, match="terminal-failing"):
        smoke.require_initial_state("ci", snapshot)


def test_effective_rewritten_or_multiple_push_destinations_are_rejected(tmp_path, _use_real_commands):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/fixture-owner/disposable.git"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "set-url", "--add", "--push", "origin", "https://github.com/unrelated/production.git"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "set-url", "--add", "--push", "origin", "https://github.com/fixture-owner/disposable.git"], check=True)

    with pytest.raises(smoke.Blocked, match="outside the designated repository"):
        smoke.validate_target("fixture-owner/disposable", tmp_path)

    subprocess.run(["git", "-C", str(tmp_path), "remote", "set-url", "--delete", "--push", "origin", "https://github.com/unrelated/production.git"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "url.https://github.com/unrelated/production.git.insteadOf", "https://github.com/fixture-owner/disposable.git"],
        check=True,
    )
    with pytest.raises(smoke.Blocked, match="outside the designated repository"):
        smoke.validate_target("fixture-owner/disposable", tmp_path)


def test_missing_workspace_is_precisely_blocked(tmp_path):
    with pytest.raises(smoke.Blocked, match="workspace directory is unavailable"):
        smoke.validate_target("fixture-owner/disposable", tmp_path / "missing")


def test_known_empty_is_only_admitted_for_affirmative_conflict():
    snapshot = smoke.Snapshot("h1", "b1", False, (), "KNOWN_EMPTY")
    smoke.require_initial_state("conflict", snapshot)
    with pytest.raises(smoke.Blocked):
        smoke.require_initial_state("ci", snapshot)


def test_wait_requires_new_head_and_terminal_current_head_run():
    snapshots = iter(
        [
            smoke.Snapshot("h1", "b1", False, (1,), "failure"),
            smoke.Snapshot("h2", "b1", True, (2,), "pending"),
            smoke.Snapshot("h2", "b1", True, (2,), "success"),
        ]
    )
    observer = SimpleNamespace(snapshot=lambda _pr: next(snapshots))
    result = smoke.wait_for_result(observer, 42, "h1", 30, sleep=lambda _seconds: None)
    assert result.head == "h2"
    assert result.run_ids == (2,)
    assert result.conclusion == "success"


def make_args(tmp_path, scenario="ci"):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "test.sh").write_text("#!/bin/sh\n")
    return Namespace(
        scenario=scenario,
        repository="fixture-owner/disposable",
        pr=42,
        workspace=str(tmp_path),
        backend="codex",
        evidence=tmp_path.parent / f"{tmp_path.name}-evidence.json",
        timeout=30,
        config_path=str(tmp_path / "llm_config.toml"),
        assertion_workflow=".github/workflows/pr.yml",
        assertion_job="fixture",
        assertion_step="Run fixture assertion",
    )


def test_harness_command_is_accepted_by_real_process_issues_parser(tmp_path):
    from auto_coder.cli_commands_main import _synchronize_codex_auth_secret_unless_skipped, process_issues

    args = make_args(tmp_path)
    command = smoke.production_command(args, tmp_path / "result.json")

    assert "--backend" not in command
    assert "--skip-codex-auth-secret-sync" in command
    with process_issues.make_context("process-issues", command[2:]) as context:
        assert context.params["repo"] == args.repository
        assert context.params["only_target"] == "42"
        assert context.params["result_file"] == tmp_path / "result.json"
        assert context.params["skip_codex_auth_secret_sync"] is True

    with patch("auto_coder.github_secrets.synchronize_codex_auth_secret") as publisher:
        _synchronize_codex_auth_secret_unless_skipped(context.params["skip_codex_auth_secret_sync"])
    publisher.assert_not_called()


class BackendConfig:
    def __init__(self, groups, backend_types):
        self.groups = groups
        self.backend_types = backend_types

    def get_ordinary_priority_groups(self):
        return self.groups

    def get_backend_config(self, name):
        backend_type = self.backend_types.get(name)
        return SimpleNamespace(backend_type=backend_type) if backend_type else None

    def resolve_backend_type(self, name):
        current = name
        visited = set()
        while current not in {"codex", "antigravity", "codex-cloud", "jules"}:
            if current in visited or current not in self.backend_types:
                raise ValueError("unresolvable alias")
            visited.add(current)
            current = self.backend_types[current]
        return current


@pytest.mark.parametrize(
    ("requested", "config", "message"),
    [
        ("codex", BackendConfig([["gemini"], ["codex"]], {"gemini": "antigravity", "codex": "codex"}), "first ordinary backend"),
        ("codex-cloud", BackendConfig([["codex-cloud"]], {"codex-cloud": "codex-cloud"}), "not a synchronous local editing backend"),
    ],
)
def test_run_smoke_blocks_backend_mismatch_or_task_only_before_github_observation(tmp_path, monkeypatch, requested, config, message):
    args = make_args(tmp_path)
    args.backend = requested
    monkeypatch.setenv("GITHUB_TOKEN", "redacted")
    monkeypatch.setattr(smoke, "validate_target", lambda *_args: None)
    monkeypatch.setattr(smoke, "get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr(smoke, "GitHubObserver", lambda *_args: pytest.fail("fixture was observed before backend admission"))

    with pytest.raises(smoke.Blocked, match=message):
        smoke.run_smoke(args)


def test_configured_first_synchronous_backend_is_admitted(monkeypatch):
    config = BackendConfig([["gemini"], ["codex"]], {"gemini": "antigravity", "codex": "codex"})
    monkeypatch.setattr(smoke, "get_llm_config", lambda **_kwargs: config)

    smoke.verify_configured_local_backend("fixture-owner/disposable", "gemini")


def make_run(head, event, status, conclusion, run_id):
    return {
        "head_sha": head,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "id": run_id,
        "workflow_id": 10,
        "run_attempt": 1,
        "name": "PR Tests",
        "path": ".github/workflows/pr.yml",
    }


def snapshot_for_runs(runs):
    observer = smoke.GitHubObserver.__new__(smoke.GitHubObserver)
    observer.owner = "fixture-owner"
    observer.repo = "disposable"
    observer.token = "token"
    observer.api = SimpleNamespace(
        pulls=SimpleNamespace(
            get=lambda *_args: SimpleNamespace(
                head=SimpleNamespace(sha="current", ref="issue-42"),
                base=SimpleNamespace(sha="base"),
                user=SimpleNamespace(login="operator"),
                body="<!-- auto-coder:local-llm -->",
                mergeable=True,
            )
        ),
        checks=SimpleNamespace(list_for_ref=lambda *_args, **_kwargs: {"check_runs": []}),
        actions=SimpleNamespace(list_workflow_runs_for_repo=lambda *_args, **kwargs: {"workflow_runs": [run for run in runs if run["head_sha"] == kwargs.get("head_sha")] if kwargs.get("page", 1) == 1 else []}),
    )
    return observer.snapshot(42)


def test_snapshot_ignores_old_head_failure_and_keeps_current_pending():
    snapshot = snapshot_for_runs(
        [
            make_run("old", "pull_request", "completed", "failure", 1),
            make_run("current", "pull_request", "in_progress", None, 2),
        ]
    )

    assert snapshot.run_ids == (2,)
    assert snapshot.conclusion == "pending"


def test_snapshot_ignores_successful_push_when_current_pull_request_fails():
    snapshot = snapshot_for_runs(
        [
            make_run("current", "push", "completed", "success", 1),
            make_run("current", "pull_request", "completed", "failure", 2),
        ]
    )

    assert snapshot.run_ids == (2,)
    assert snapshot.conclusion == "failure"


def test_snapshot_succeeds_only_when_all_current_pr_runs_pass():
    snapshot = snapshot_for_runs(
        [
            make_run("current", "pull_request", "completed", "success", 1),
            make_run("current", "pull_request_target", "completed", "neutral", 2),
            make_run("current", "pull_request", "completed", "skipped", 3),
        ]
    )

    assert snapshot.run_ids == (1, 2, 3)
    assert snapshot.conclusion == "success"


def test_fixture_assertion_requires_matching_h2_step_success():
    observer = smoke.GitHubObserver.__new__(smoke.GitHubObserver)
    observer.owner = "fixture-owner"
    observer.repo = "disposable"
    observer.api = SimpleNamespace(
        actions=SimpleNamespace(
            list_jobs_for_workflow_run_attempt=lambda *_args, **_kwargs: {
                "jobs": [
                    {
                        "name": "fixture",
                        "steps": [{"name": "Run fixture assertion", "status": "completed", "conclusion": "skipped"}],
                    }
                ]
            }
        )
    )
    snapshot = smoke.Snapshot(
        "h2",
        "base",
        True,
        (22,),
        "success",
        run_records=(
            {
                "workflow_id": "10",
                "run_id": "22",
                "attempt": 2,
                "workflow_path": ".github/workflows/pr.yml",
            },
        ),
    )

    assertion = observer.assertion(snapshot, ".github/workflows/pr.yml", "fixture", "Run fixture assertion")

    assert assertion.run_id == "22"
    assert assertion.attempt == 2
    assert assertion.conclusion == "skipped"


def processing_artifact(outcome, actions=None, *, repair=None, final_gate=None):
    return {
        "target_number": 42,
        "target_type": "pr",
        "target_outcome": outcome,
        "target_reason": f"{outcome} disposition",
        "target_actions": actions or [],
        "schema_version": 1,
        "invocation_id": f"invocation-{outcome}",
        "repository": "fixture-owner/disposable",
        "configured_primary_backend": "codex",
        "execution_origin": "ordinary-explicit-process-issues",
        "repair": repair,
        "final_gate": final_gate,
        "editing_invocations": ([{"backend_alias": "codex", "backend_type": "codex", "model": "test-model", "purpose": "local-ci-repair", "status": "returned"}] if repair else []),
    }


def applied_repair():
    return {
        "repository": "fixture-owner/disposable",
        "pr_number": 42,
        "input_head": "h1",
        "disposition": "applied",
        "attempt_started": True,
        "resulting_head_state": "available",
        "resulting_head": "h2",
        "synchronization_state": "available",
        "synchronized": True,
        "actions": ["published repaired head"],
        "reason": "",
    }


def merged_gate():
    return {"disposition": "MERGE_COMPLETED", "repository": "fixture-owner/disposable", "pr_number": 42, "evaluated_head": "h2", "evaluated_base": "base"}


def test_conflict_without_local_attribution_is_blocked_before_processing(tmp_path, monkeypatch):
    args = make_args(tmp_path, scenario="conflict")
    observer = SimpleNamespace(snapshot=lambda _pr: smoke.Snapshot("h1", "base", False, (), "KNOWN_EMPTY", False))
    monkeypatch.setenv("GITHUB_TOKEN", "redacted")
    monkeypatch.setattr(smoke, "validate_target", lambda *_args: None)
    monkeypatch.setattr(smoke, "verify_configured_local_backend", lambda *_args: None)
    monkeypatch.setattr(smoke, "GitHubObserver", lambda *_args: observer)
    monkeypatch.setattr(smoke, "run_production", lambda *_args: pytest.fail("processing started without local attribution"))

    with pytest.raises(smoke.Blocked, match="local Auto-Coder attribution"):
        smoke.run_smoke(args)


def test_green_h2_with_deferred_authoritative_gate_is_not_passed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    args = make_args(tmp_path)
    before = smoke.Snapshot("h1", "base", True, (1,), "failure")
    after = smoke.Snapshot("h2", "base", True, (2,), "success")
    snapshots = iter((before, after, after))
    observer = SimpleNamespace(
        snapshot=lambda _pr: next(snapshots),
        final_pr=lambda _pr: smoke.FinalPRObservation("open", False, "h2", "base", ""),
        assertion=lambda *_args: smoke.AssertionObservation(status="completed", conclusion="success"),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "redacted")
    monkeypatch.setattr(smoke, "validate_target", lambda *_args: None)
    monkeypatch.setattr(smoke, "verify_configured_local_backend", lambda *_args: None)
    monkeypatch.setattr(smoke, "GitHubObserver", lambda *_args: observer)

    calls = 0

    def run(command, _workspace, _config_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            artifact = processing_artifact("deferred", ["review deferred"])
        else:
            artifact = processing_artifact("deferred", ["published repaired head"], repair=applied_repair())
        Path(command[command.index("--result-file") + 1]).write_text(json.dumps(artifact))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(smoke, "run_production", run)
    evidence = smoke.run_smoke(args)

    assert evidence.status == "FAILED"
    assert evidence.gate_outcome == "deferred"
    assert evidence.failure_phase == "independent_review_or_merge"
    assert evidence.repair_backend == "codex"
    assert evidence.repair_actions == ("published repaired head",)
    assert evidence.gate_actions == ("review deferred",)


def test_deferred_repair_processing_passes_only_with_applied_publication_and_matching_final_merge(tmp_path, monkeypatch):
    args = make_args(tmp_path)
    before = smoke.Snapshot("h1", "base", True, (1,), "failure")
    after = smoke.Snapshot("h2", "base", True, (2,), "success")
    snapshots = iter((before, after, after))
    observer = SimpleNamespace(
        snapshot=lambda _pr: next(snapshots),
        final_pr=lambda _pr: smoke.FinalPRObservation("closed", True, "h2", "advanced-base", "merge-sha"),
        assertion=lambda *_args: smoke.AssertionObservation(status="completed", conclusion="success"),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "redacted")
    monkeypatch.setattr(smoke, "validate_target", lambda *_args: None)
    monkeypatch.setattr(smoke, "verify_configured_local_backend", lambda *_args: None)
    monkeypatch.setattr(smoke, "GitHubObserver", lambda *_args: observer)

    def run(command, _workspace, _config_path):
        is_repair = any(part.endswith(".repair-result.json") for part in command)
        artifact = processing_artifact("deferred", ["published"], repair=applied_repair()) if is_repair else processing_artifact("success", ["merged"], final_gate=merged_gate())
        Path(command[command.index("--result-file") + 1]).write_text(json.dumps(artifact))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(smoke, "run_production", run)
    evidence = smoke.run_smoke(args)

    assert evidence.status == "PASSED"
    assert evidence.resulting_head == "h2"
    assert '"merged": true' in evidence.review_merge_result


def test_refused_repair_cannot_claim_an_independently_advanced_green_head(tmp_path, monkeypatch):
    record = smoke.ProcessingRecord(
        "deferred",
        "codex",
        "ordinary-explicit-process-issues",
        (),
        "refused",
        "invocation",
        "fixture-owner/disposable",
        {**applied_repair(), "disposition": "refused", "attempt_started": False},
        None,
        (),
    )

    assert smoke.repair_matches_run(record, "fixture-owner/disposable", 42, "h1", "h2", "codex") is False


def test_missing_gate_artifact_preserves_observed_repair_evidence(tmp_path, monkeypatch):
    args = make_args(tmp_path)
    before = smoke.Snapshot("h1", "base", True, (1,), "failure")
    after = smoke.Snapshot("h2", "base", True, (2,), "success")
    snapshots = iter((before, after, after))
    observer = SimpleNamespace(
        snapshot=lambda _pr: next(snapshots),
        final_pr=lambda _pr: smoke.FinalPRObservation("open", False, "h2", "base", ""),
        assertion=lambda *_args: smoke.AssertionObservation(status="completed", conclusion="success"),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "redacted")
    monkeypatch.setattr(smoke, "validate_target", lambda *_args: None)
    monkeypatch.setattr(smoke, "verify_configured_local_backend", lambda *_args: None)
    monkeypatch.setattr(smoke, "GitHubObserver", lambda *_args: observer)

    calls = 0

    def run(command, _workspace, _config_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(command[command.index("--result-file") + 1]).write_text(json.dumps(processing_artifact("deferred", ["published repaired head"], repair=applied_repair())))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(smoke, "run_production", run)
    evidence = smoke.run_smoke(args)

    assert evidence.status == "BLOCKED"
    assert evidence.failure_phase == "independent_review_or_merge"
    assert evidence.captured_head == "h1"
    assert evidence.resulting_head == "h2"
    assert evidence.repair_backend == "codex"
    assert evidence.repair_actions == ("published repaired head",)


def test_polling_failure_preserves_latest_pending_h2_and_repair(tmp_path, monkeypatch):
    from auto_coder.util.github_request_outcome import GitHubRequestError

    args = make_args(tmp_path)
    before = smoke.Snapshot("h1", "base", True, (1,), "failure")
    pending = smoke.Snapshot("h2", "base", True, (2,), "pending")
    outcome = SimpleNamespace(status=503, classification=SimpleNamespace(value="unavailable"))
    snapshots = iter((before, pending))

    def snapshot(_pr):
        try:
            return next(snapshots)
        except StopIteration:
            raise GitHubRequestError(outcome)

    monkeypatch.setenv("GITHUB_TOKEN", "redacted")
    monkeypatch.setattr(smoke, "validate_target", lambda *_args: None)
    monkeypatch.setattr(smoke, "verify_configured_local_backend", lambda *_args: None)
    monkeypatch.setattr(smoke, "GitHubObserver", lambda *_args: SimpleNamespace(snapshot=snapshot))

    def run(command, _workspace, _config_path):
        Path(command[command.index("--result-file") + 1]).write_text(json.dumps(processing_artifact("deferred", ["published repaired head"], repair=applied_repair())))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(smoke, "run_production", run)

    evidence = smoke.run_smoke(args)

    assert evidence.status == "BLOCKED"
    assert evidence.failure_phase == "publication_or_current_head_ci"
    assert (evidence.captured_head, evidence.resulting_head) == ("h1", "h2")
    assert evidence.post_ci_conclusion == "pending"
    assert evidence.repair_actions == ("published repaired head",)
    assert evidence.repair_exit_code == 0


def test_final_merge_permission_failure_preserves_h1_h2_and_processing_records(tmp_path, monkeypatch):
    from auto_coder.util.github_request_outcome import GitHubRequestError

    args = make_args(tmp_path)
    before = smoke.Snapshot("h1", "base", True, (1,), "failure")
    after = smoke.Snapshot("h2", "base", True, (2,), "success")
    snapshots = iter((before, after, after))
    outcome = SimpleNamespace(status=403, classification=SimpleNamespace(value="forbidden"))
    observer = SimpleNamespace(
        snapshot=lambda _pr: next(snapshots),
        final_pr=lambda _pr: (_ for _ in ()).throw(GitHubRequestError(outcome)),
        assertion=lambda *_args: smoke.AssertionObservation(status="completed", conclusion="success"),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "redacted")
    monkeypatch.setattr(smoke, "validate_target", lambda *_args: None)
    monkeypatch.setattr(smoke, "verify_configured_local_backend", lambda *_args: None)
    monkeypatch.setattr(smoke, "GitHubObserver", lambda *_args: observer)

    def run(command, _workspace, _config_path):
        result = "published repaired head" if any(part.endswith(".repair-result.json") for part in command) else "independent gate passed"
        artifact = processing_artifact("deferred", [result], repair=applied_repair()) if "published" in result else processing_artifact("success", [result], final_gate=merged_gate())
        Path(command[command.index("--result-file") + 1]).write_text(json.dumps(artifact))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(smoke, "run_production", run)
    evidence = smoke.run_smoke(args)

    assert evidence.status == "BLOCKED"
    assert evidence.failure_phase == "final_merge_observation"
    assert (evidence.captured_head, evidence.resulting_head) == ("h1", "h2")
    assert evidence.repair_actions == ("published repaired head",)


def test_missing_auto_coder_executable_is_blocked(tmp_path):
    with patch.object(smoke.subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(smoke.Blocked, match="executable is unavailable"):
            smoke.run_production(["auto-coder"], tmp_path, str(tmp_path / "config.toml"))


def test_inaccessible_fixture_writes_redacted_blocked_evidence(tmp_path, monkeypatch):
    from auto_coder.util.github_request_outcome import GitHubRequestError

    args = make_args(tmp_path)
    outcome = SimpleNamespace(status=404, classification=SimpleNamespace(value="not_found"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "ci",
            "--repository",
            args.repository,
            "--pr",
            "42",
            "--workspace",
            str(tmp_path),
            "--backend",
            "codex",
            "--evidence",
            str(args.evidence),
            "--assertion-workflow",
            args.assertion_workflow,
            "--assertion-job",
            args.assertion_job,
            "--assertion-step",
            args.assertion_step,
        ],
    )
    monkeypatch.setattr(smoke, "run_smoke", lambda _args: (_ for _ in ()).throw(GitHubRequestError(outcome)))

    assert smoke.main() == 2
    artifact = json.loads(args.evidence.read_text())
    assert artifact["status"] == "BLOCKED"
    assert artifact["failure_phase"] == "preflight"
    assert artifact["disposition"] == "designated GitHub repository or PR is inaccessible with the supplied credential"
