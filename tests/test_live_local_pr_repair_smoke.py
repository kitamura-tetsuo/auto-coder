"""Deterministic contract checks for the opt-in live smoke harness.

These tests intentionally do not claim to be the credentialed live E2E.
"""

import importlib.util
import json
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
    return Namespace(
        scenario=scenario,
        repository="fixture-owner/disposable",
        pr=42,
        workspace=str(tmp_path),
        backend="codex",
        evidence=tmp_path / "evidence.json",
        timeout=30,
    )


def test_harness_command_is_accepted_by_real_process_issues_parser(tmp_path):
    from auto_coder.cli_commands_main import process_issues

    args = make_args(tmp_path)
    command = smoke.production_command(args, tmp_path / "result.json")

    assert "--backend" not in command
    with process_issues.make_context("process-issues", command[2:]) as context:
        assert context.params["repo"] == args.repository
        assert context.params["only_target"] == "42"
        assert context.params["result_file"] == tmp_path / "result.json"


class BackendConfig:
    def __init__(self, groups, backend_types):
        self.groups = groups
        self.backend_types = backend_types

    def get_ordinary_priority_groups(self):
        return self.groups

    def get_backend_config(self, name):
        backend_type = self.backend_types.get(name)
        return SimpleNamespace(backend_type=backend_type) if backend_type else None


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
    return SimpleNamespace(head_sha=head, event=event, status=status, conclusion=conclusion, id=run_id, created_at=f"2026-10-19T00:00:{run_id:02d}Z")


def snapshot_for_runs(runs):
    observer = smoke.GitHubObserver.__new__(smoke.GitHubObserver)
    observer.owner = "fixture-owner"
    observer.repo = "disposable"
    observer.api = SimpleNamespace(
        pulls=SimpleNamespace(get=lambda *_args: SimpleNamespace(head=SimpleNamespace(sha="current"), base=SimpleNamespace(sha="base"), mergeable=True)),
        actions=SimpleNamespace(list_workflow_runs_for_repo=lambda *_args, **_kwargs: SimpleNamespace(workflow_runs=runs)),
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

    assert snapshot.run_ids == (3, 2, 1)
    assert snapshot.conclusion == "success"


def test_green_h2_with_deferred_authoritative_gate_is_not_passed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    args = make_args(tmp_path)
    before = smoke.Snapshot("h1", "base", True, (1,), "failure")
    after = smoke.Snapshot("h2", "base", True, (2,), "success")
    snapshots = iter((before, after, after))
    observer = SimpleNamespace(snapshot=lambda _pr: next(snapshots), merge_state=lambda _pr: "open:clean")
    monkeypatch.setenv("GITHUB_TOKEN", "redacted")
    monkeypatch.setattr(smoke, "validate_target", lambda *_args: None)
    monkeypatch.setattr(smoke, "verify_configured_local_backend", lambda *_args: None)
    monkeypatch.setattr(smoke, "GitHubObserver", lambda *_args: observer)

    calls = 0

    def run(command, _workspace):
        nonlocal calls
        calls += 1
        if calls == 2:
            Path(command[command.index("--result-file") + 1]).write_text(json.dumps({"target_number": 42, "target_type": "pr", "target_outcome": "deferred"}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(smoke, "run_production", run)
    evidence = smoke.run_smoke(args)

    assert evidence.status == "FAILED"
    assert evidence.gate_outcome == "deferred"
    assert evidence.failure_phase == "independent_review_or_merge"


def test_missing_auto_coder_executable_is_blocked(tmp_path):
    with patch.object(smoke.subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(smoke.Blocked, match="executable is unavailable"):
            smoke.run_production(["auto-coder"], tmp_path)


def test_inaccessible_fixture_writes_redacted_blocked_evidence(tmp_path, monkeypatch):
    from auto_coder.util.github_request_outcome import GitHubRequestError

    args = make_args(tmp_path)
    outcome = SimpleNamespace(status=404, classification=SimpleNamespace(value="not_found"))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "ci", "--repository", args.repository, "--pr", "42", "--workspace", str(tmp_path), "--backend", "codex", "--evidence", str(args.evidence)])
    monkeypatch.setattr(smoke, "run_smoke", lambda _args: (_ for _ in ()).throw(GitHubRequestError(outcome)))

    assert smoke.main() == 2
    artifact = json.loads(args.evidence.read_text())
    assert artifact["status"] == "BLOCKED"
    assert artifact["failure_phase"] == "preflight"
    assert artifact["disposition"] == "designated GitHub repository or PR is inaccessible with the supplied credential"
