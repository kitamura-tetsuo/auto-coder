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
