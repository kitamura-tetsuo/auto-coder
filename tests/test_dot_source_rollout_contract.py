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
        check=True,
    )
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
        pending_effect_state=pending,
    )
    report = smoke.required_next_observation(original)
    assert report.verdict == "BLOCKED"
    assert "original operation ID" in report.reason
    with pytest.raises(ValueError, match="original operation ID"):
        smoke.read_plan(original, "beta-worker", "")
    plan = smoke.read_plan(original, "beta-worker", "original-operation")
    assert plan[-1] == (
        "auto_coder_operation_get",
        {"target_id": "beta-worker", "operation_id": "original-operation"},
    )
    assert all(name in smoke.READ_TOOLS for name, _ in plan)


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
        source_sha="a" * 40,
        ci_run_id="452",
        ci_run_attempt="2",
        tested_source_ref="refs/tags/auto-coder-tested-" + "a" * 40,
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
    plan = smoke.read_plan(checkpoint(stage="full"), "fixture-beta", "operation-123")
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
