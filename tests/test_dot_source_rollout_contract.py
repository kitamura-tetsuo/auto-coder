"""Offline policy/transport planning only; NOT a fake live MCP acceptance test."""

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from scripts import management_mcp_rollout_smoke as smoke


def checkpoint(**updates):
    return replace(smoke.DotCheckpoint(schema=1, stage="observe"), **updates)


def test_offline_default_never_sends_and_never_claims_deployment(tmp_path):
    script = Path(smoke.__file__)
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path)},
        timeout=15,
    )
    assert result.returncode == 3
    report = json.loads(result.stdout.splitlines()[-1])
    assert report == {
        "verdict": "NOT_RUN",
        "stage": "observe",
        "reason": "Current authenticated status, original operation and child evidence require an independent live read",
        "observations": [],
        "positive_deployment_proven": False,
        "effect_dispatched": False,
    }
    assert (tmp_path / "auto-coder" / "dot-rollout-smoke.log").is_file()


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("observe", "NOT_RUN"),
        ("beta", "BLOCKED"),
        ("full", "BLOCKED"),
    ],
)
def test_stage_checkpoint_does_not_supply_positive_evidence(stage, expected):
    report = smoke.required_next_observation(checkpoint(stage=stage))
    assert report.verdict == expected
    assert report.stage == stage
    assert not report.positive_deployment_proven
    assert not report.effect_dispatched


@pytest.mark.parametrize("stage", ["observe", "beta", "full"])
@pytest.mark.parametrize("pending", ["SENT_UNKNOWN", "NEEDS_ATTENTION"])
def test_unknown_effect_precedes_every_stage_and_never_rekeys(stage, pending):
    original = checkpoint(
        stage=stage,
        idempotency_key="immutable-original-key",
        beta_target="beta-worker",
        beta_operation_id="original-operation",
        pending_effect_state=pending,
        pending_operation_id="original-operation",
    )
    report = smoke.required_next_observation(original)
    assert report.verdict == "BLOCKED"
    assert "original operation ID" in report.reason
    with pytest.raises(ValueError, match="original operation ID"):
        smoke.read_plan(original, "beta-worker", "")
    with pytest.raises(ValueError, match="pending original operation ID"):
        smoke.read_plan(original, "beta-worker", "unrelated-operation")
    plan = smoke.read_plan(original, "beta-worker", "original-operation")
    assert plan[1] == (
        "auto_coder_operation_get",
        {"target_id": "beta-worker", "operation_id": "original-operation"},
    )
    assert all(name in smoke.READ_TOOLS for name, _ in plan)
    assert [name for name, _ in plan[:2]] == ["auto_coder_whoami", "auto_coder_operation_get"]


@pytest.mark.parametrize("pending", ["SENT_UNKOWN", "READY", "COMPLETE"])
def test_unknown_pending_state_cannot_skip_original_effect_reconciliation(pending):
    original = checkpoint(beta_target="beta-worker", beta_operation_id="original-operation", pending_effect_state=pending)
    assert smoke.required_next_observation(original).verdict == "BLOCKED"
    with pytest.raises(ValueError, match="pending effect state"):
        smoke.read_plan(original, "beta-worker", "original-operation")


@pytest.mark.parametrize(
    "original,target,operation",
    [
        (checkpoint(beta_target="beta-worker", beta_operation_id="beta-op"), "release-worker", "beta-op"),
        (checkpoint(release_target="release-worker", release_operation_id="release-op"), "beta-worker", "release-op"),
        (checkpoint(release_target="release-worker", promotion_operation_id="promotion-op"), "beta-worker", "promotion-op"),
        (checkpoint(beta_operation_id="beta-op"), "beta-worker", "beta-op"),
    ],
)
def test_unknown_effect_rejects_cross_target_or_unbound_original_operation(original, target, operation):
    original = replace(original, pending_effect_state="SENT_UNKNOWN", pending_operation_id=operation)
    with pytest.raises(ValueError, match="original operation.*target"):
        smoke.read_plan(original, target, operation)


def test_unknown_effect_requires_unique_original_operation_target_binding():
    original = checkpoint(
        beta_target="beta-worker",
        beta_operation_id="ambiguous-op",
        release_target="release-worker",
        release_operation_id="ambiguous-op",
        pending_effect_state="NEEDS_ATTENTION",
        pending_operation_id="ambiguous-op",
    )
    with pytest.raises(ValueError, match="original operation.*target"):
        smoke.read_plan(original, "beta-worker", "ambiguous-op")


@pytest.mark.parametrize(
    "original,target,operation",
    [
        (checkpoint(beta_target="beta-worker", beta_operation_id="beta-op"), "beta-worker", "beta-op"),
        (checkpoint(release_target="release-worker", release_operation_id="release-op"), "release-worker", "release-op"),
        (checkpoint(release_target="release-worker", promotion_operation_id="promotion-op"), "release-worker", "promotion-op"),
    ],
)
def test_pending_original_operation_read_has_one_authorized_target(original, target, operation):
    original = replace(original, pending_effect_state="NEEDS_ATTENTION", pending_operation_id=operation)
    plan = smoke.read_plan(original, target, operation)
    assert plan[1] == ("auto_coder_operation_get", {"target_id": target, "operation_id": operation})
    assert len(plan) == 4


def test_unrecognized_pending_effect_is_rejected_from_disk(tmp_path):
    raw = asdict(checkpoint(beta_target="beta-worker", beta_operation_id="beta-op", pending_effect_state="SENT_UNKOWN"))
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="unsupported pending effect state"):
        smoke.load_checkpoint(path)


def test_pending_effect_does_not_read_other_saved_operation_on_same_target():
    original = checkpoint(
        release_target="fixture-release",
        promotion_operation_id="promotion-unknown",
        release_operation_id="release-older",
        pending_effect_state="SENT_UNKNOWN",
        pending_operation_id="promotion-unknown",
    )
    with pytest.raises(ValueError, match="pending original operation ID"):
        smoke.read_plan(original, "fixture-release", "release-older")
    plan = smoke.read_plan(original, "fixture-release", "promotion-unknown")
    assert plan[1] == (
        "auto_coder_operation_get",
        {"target_id": "fixture-release", "operation_id": "promotion-unknown"},
    )


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"pending_effect_state": "SENT_UNKNOWN", "beta_target": "fixture-beta", "beta_operation_id": "op-1"}, "pending effect requires"),
        ({"pending_operation_id": "op-1", "beta_target": "fixture-beta", "beta_operation_id": "op-1"}, "requires a pending effect"),
        (
            {
                "pending_effect_state": "NEEDS_ATTENTION",
                "pending_operation_id": "missing",
                "beta_target": "fixture-beta",
                "beta_operation_id": "op-1",
            },
            "uniquely bound",
        ),
    ],
)
def test_checkpoint_refuses_unbound_pending_original_operation(tmp_path, updates, reason):
    raw = asdict(checkpoint())
    raw.update(updates)
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=reason):
        smoke.load_checkpoint(path)


def test_pending_original_identity_survives_resumption_without_reselecting_other_operation(tmp_path):
    original = checkpoint(
        stage="beta",
        repository="kitamura-tetsuo/auto-coder",
        issue_number="2608",
        task_id="dot-original",
        stage_revision="operator-original-rev2",
        beta_target="fixture-beta",
        beta_operation_id="unresolved-beta",
        release_target="fixture-release",
        release_operation_id="prior-release",
        pending_effect_state="NEEDS_ATTENTION",
        pending_operation_id="unresolved-beta",
    )
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(asdict(original)))
    loaded = smoke.load_checkpoint(path)
    assert loaded.pending_operation_id == "unresolved-beta"
    assert smoke.read_plan(loaded, "fixture-beta", "unresolved-beta")[1][0] == "auto_coder_operation_get"
    with pytest.raises(ValueError, match="pending original operation ID"):
        smoke.read_plan(loaded, "fixture-release", "prior-release")
    assert smoke.required_next_observation(loaded).verdict == "BLOCKED"


def test_one_recovery_attempt_is_terminal_for_unchanged_failure():
    original = checkpoint(stage="beta", failure_signature="failed-start-sha-v1", recovery_attempts=1)
    report = smoke.required_next_observation(original)
    assert report.verdict == "BLOCKED"
    assert "exhausted" in report.reason


@pytest.mark.parametrize(
    "updates",
    [
        {"stage": "admin"},
        {"schema": True},
        {"recovery_attempts": True},
        {"recovery_attempts": 2},
        {"recovery_attempts": 1},
        {"source_sha": "main"},
        {"source_sha": "A" * 40},
        {"idempotency_key": "invalid key"},
        {"beta_target": 123},
        {"beta_target": "bad\x00target"},
        {"task_id": "bad task"},
        {"repository": "https://github.com/owner/repo"},
        {"repository": "owner/repo", "issue_number": "00042"},
        {"issue_number": "2608"},
        {"repository": "owner/repo", "pr_number": "-42"},
        {"stage_revision": "invalid revision"},
    ],
)
def test_checkpoint_refuses_invalid_authority_and_identity(tmp_path, updates):
    raw = asdict(checkpoint())
    raw.update(updates)
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        smoke.load_checkpoint(path)


@pytest.mark.parametrize("field", ["bearer", "refresh_token", "unsafe_mutation_key"])
def test_checkpoint_refuses_secret_or_unsupported_fields(tmp_path, field):
    raw = asdict(checkpoint())
    raw[field] = "unexpected"
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="exactly"):
        smoke.load_checkpoint(path)


def test_checkpoint_roundtrips_all_immutable_identifiers_without_elevation(tmp_path):
    original = checkpoint(
        stage="full",
        task_id="task-2608-original",
        repository="kitamura-tetsuo/auto-coder",
        issue_number="2608",
        pr_number="2835",
        stage_revision="operator-stage-rev-04",
        source_sha="a" * 40,
        ci_run_id="452",
        ci_run_attempt="2",
        tested_source_ref="refs/tags/auto-coder-tested-" + "a" * 40,
        tested_source_oid="a" * 40,
        beta_target="fixture-beta",
        beta_acceptance_ref="refs/tags/beta-accepted",
        beta_acceptance_oid="b" * 40,
        promotion_operation_id="promotion-original",
        promotion_catalog_oid="c" * 40,
        release_operation_id="release-original",
        idempotency_key="stable-unchanged-key",
    )
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(asdict(original)))
    loaded = smoke.load_checkpoint(path)
    assert loaded == original
    report = smoke.required_next_observation(loaded)
    assert report.verdict == "NOT_RUN"
    assert report.stage == "full"
    assert report.positive_deployment_proven is False


@pytest.mark.parametrize(
    "url",
    [
        "http://production.example/mcp",
        "https://foo.example/other",
        "https://foo.example/mcp?bearer=secret",
        "https://user:pass@foo.example/mcp",
        "file:///mcp",
        "https://foo.example/mcp#fragment",
    ],
)
def test_transport_does_not_send_bearer_to_unapproved_origin_profile(url):
    with pytest.raises(ValueError):
        smoke.checked_endpoint(url)


def test_read_only_transport_allows_explicit_https_or_loopback():
    assert smoke.checked_endpoint("https://fixture.example/mcp") == "https://fixture.example/mcp"
    assert smoke.checked_endpoint("http://127.0.0.1:8790/mcp") == "http://127.0.0.1:8790/mcp"


@pytest.mark.parametrize(
    "tool",
    [
        "auto_coder_deploy_release_source",
        "auto_coder_deploy_beta_source",
        "auto_coder_promote_accepted_source_release",
        "auto_coder_reevaluate_target",
        "auto_coder_update_management_service",
        "auto_coder_start_worker",
    ],
)
def test_runtime_tool_allowlist_explicitly_refuses_mutation(tool):
    with pytest.raises(ValueError, match="never dispatches"):
        smoke.checked_tool(tool, {"target_id": "fixture-beta"})


def test_mcp_read_plan_never_contains_a_mutation_or_endpoint_selection():
    original = checkpoint(stage="full", beta_target="fixture-beta", beta_operation_id="operation-123")
    plan = smoke.read_plan(original, "fixture-beta", "operation-123")
    assert [name for name, _ in plan] == [
        "auto_coder_whoami",
        "auto_coder_status",
        "auto_coder_deployment_status",
        "auto_coder_operation_get",
    ]
    assert all("url" not in arguments and "command" not in arguments for _, arguments in plan)


def test_missing_token_refuses_before_any_transport(tmp_path):
    script = Path(smoke.__file__)
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path)}
    env.pop("AUTO_CODER_MCP_ACCESS_TOKEN", None)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--endpoint",
            "https://never-contacted-fixture.invalid/mcp",
            "--target-id",
            "fixture-beta",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 2
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["verdict"] == "BLOCKED"
    assert report["observations"] == []
    assert report["effect_dispatched"] is False
    assert "never-contacted-fixture" not in result.stderr


def test_read_only_transport_refuses_missing_original_operation_during_unknown():
    with pytest.raises(ValueError, match="original operation ID"):
        smoke.read_plan(checkpoint(pending_effect_state="SENT_UNKNOWN"), "beta-worker", "")


def test_checkpoint_preserves_original_work_and_stage_revision_across_cli_resume(tmp_path):
    original = checkpoint(
        stage="beta",
        task_id="dot-issue-2608",
        repository="kitamura-tetsuo/auto-coder",
        issue_number="2608",
        pr_number="2835",
        stage_revision="operator-decision-20261111-rev2",
        source_sha="a" * 40,
        tested_source_ref="refs/heads/original-reviewed-feature",
        tested_source_oid="a" * 40,
        beta_target="fixture-beta",
        ci_run_id="43010413128",
        ci_run_attempt="1",
    )
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(asdict(original)))
    assert smoke.load_checkpoint(path) == original
    result = subprocess.run(
        [sys.executable, str(Path(smoke.__file__)), "--checkpoint", str(path)],
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path)},
        timeout=15,
    )
    assert result.returncode == 3
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["verdict"] == "NOT_RUN" and report["effect_dispatched"] is False
    assert smoke.load_checkpoint(path).task_id == "dot-issue-2608"
    assert smoke.load_checkpoint(path).stage_revision == "operator-decision-20261111-rev2"
    assert smoke.load_checkpoint(path).tested_source_ref == "refs/heads/original-reviewed-feature"


@pytest.mark.parametrize("missing", ["task_id", "repository", "issue_number", "stage_revision"])
def test_beta_rollout_cannot_proceed_without_original_work_identity_or_stage_revision(missing):
    values = {
        "stage": "beta",
        "task_id": "original-task",
        "repository": "kitamura-tetsuo/auto-coder",
        "issue_number": "2608",
        "stage_revision": "operator-stage-rev2",
        "source_sha": "a" * 40,
        "ci_run_id": "43010413128",
        "tested_source_ref": "refs/heads/original-reviewed-feature",
    }
    values[missing] = ""
    report = smoke.required_next_observation(checkpoint(**values))
    assert report.verdict == "BLOCKED"
    assert "work identity or operator stage revision" in report.reason
    assert not report.effect_dispatched


@pytest.mark.parametrize(
    "original,target,operation",
    [
        (checkpoint(), "fixture-beta", "unclaimed-read"),
        (
            checkpoint(beta_target="fixture-beta", beta_operation_id="real-original"),
            "fixture-beta",
            "another-operation",
        ),
        (
            checkpoint(beta_target="fixture-beta", beta_operation_id="real-original"),
            "fixture-release",
            "real-original",
        ),
        (
            checkpoint(beta_target="fixture-beta", beta_operation_id="duplicated", release_target="fixture-release", release_operation_id="duplicated"),
            "fixture-beta",
            "duplicated",
        ),
    ],
)
def test_diagnostic_operation_read_must_match_unique_checkpoint_original_binding(original, target, operation):
    with pytest.raises(ValueError, match="original operation"):
        smoke.read_plan(original, target, operation)


def test_diagnostic_operation_read_accepts_exact_original_binding_without_dispatch():
    original = checkpoint(beta_target="fixture-beta", beta_operation_id="real-original")
    plan = smoke.read_plan(original, "fixture-beta", "real-original")
    assert plan[-1] == (
        "auto_coder_operation_get",
        {"target_id": "fixture-beta", "operation_id": "real-original"},
    )
    assert all(name in smoke.READ_TOOLS for name, _ in plan)


@pytest.mark.parametrize("missing", ["ci_run_attempt", "tested_source_oid", "beta_target"])
def test_beta_refuses_incomplete_original_ci_or_selected_target(missing):
    evidence = {
        "stage": "beta",
        "task_id": "original-task",
        "repository": "kitamura-tetsuo/auto-coder",
        "issue_number": "2608",
        "stage_revision": "operator-revision",
        "source_sha": "a" * 40,
        "ci_run_id": "43010413128",
        "ci_run_attempt": "1",
        "tested_source_ref": "refs/heads/reviewed-source",
        "tested_source_oid": "a" * 40,
        "beta_target": "fixture-beta",
    }
    evidence[missing] = ""
    report = smoke.required_next_observation(checkpoint(**evidence))
    assert report.verdict == "BLOCKED"
    assert not report.effect_dispatched


def test_full_cannot_skip_original_beta_and_tested_source_lineage():
    evidence = checkpoint(
        stage="full",
        task_id="original-task",
        repository="kitamura-tetsuo/auto-coder",
        issue_number="2608",
        stage_revision="operator-revision",
        beta_acceptance_ref="refs/tags/accepted-beta",
        beta_acceptance_oid="b" * 40,
        promotion_operation_id="promotion-original",
        promotion_catalog_oid="c" * 40,
    )
    report = smoke.required_next_observation(evidence)
    assert report.verdict == "BLOCKED"
    assert not report.effect_dispatched


@pytest.mark.parametrize(
    "active_stage,active_revision,error",
    [
        ("observe", "", "stage differs"),
        ("beta", "operator-new-revision", "revision differs"),
        ("beta", "", "current operator stage revision"),
        ("full", "operator-original-revision", "stage differs"),
    ],
)
def test_live_resumption_requires_matching_current_operator_scope(active_stage, active_revision, error):
    saved = checkpoint(
        stage="beta",
        task_id="original-task",
        repository="kitamura-tetsuo/auto-coder",
        issue_number="2608",
        stage_revision="operator-original-revision",
    )
    with pytest.raises(smoke.OperatorScopeError, match=error):
        smoke.confirm_operator_scope(saved, active_stage, active_revision)


def test_operator_scope_confirmation_does_not_supply_current_grant_or_success():
    saved = checkpoint(
        stage="beta",
        task_id="original-task",
        repository="kitamura-tetsuo/auto-coder",
        issue_number="2608",
        stage_revision="operator-original-revision",
    )
    smoke.confirm_operator_scope(saved, "beta", "operator-original-revision")
    result = smoke.required_next_observation(saved)
    assert result.verdict == "BLOCKED"
    assert not result.positive_deployment_proven
    assert not result.effect_dispatched


def test_cli_refuses_stale_operator_revision_before_mcp_transport(tmp_path):
    saved = checkpoint(
        stage="beta",
        task_id="original-task",
        repository="kitamura-tetsuo/auto-coder",
        issue_number="2608",
        stage_revision="operator-original-revision",
        beta_target="fixture-beta",
    )
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(asdict(saved)))
    result = subprocess.run(
        [
            sys.executable,
            str(Path(smoke.__file__)),
            "--checkpoint",
            str(path),
            "--endpoint",
            "https://never-contacted-fixture.invalid/mcp",
            "--target-id",
            "fixture-beta",
            "--operator-stage",
            "beta",
            "--operator-stage-revision",
            "operator-current-revision",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path), "AUTO_CODER_MCP_ACCESS_TOKEN": "disposable-unused-test-token"},
        timeout=15,
    )
    assert result.returncode == 2
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["verdict"] == "BLOCKED"
    assert "revision differs" in report["reason"]
    assert report["observations"] == []
    assert not report["effect_dispatched"]
    assert not report["positive_deployment_proven"]
    assert smoke.load_checkpoint(path) == saved
