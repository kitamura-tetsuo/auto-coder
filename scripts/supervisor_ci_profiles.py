"""The exact, profile-qualified selections used by Supervisor Actions shards."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from auto_coder.ci_shard_plan import ShardContext, ShardPlanError, read_plan

REPOSITORY = "kitamura-tetsuo/auto-coder"
PROFILES = ("management-self-update", "fresh-host", "isolated-containers")

OPERATOR_REQUIRED = (
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_prepare_apply_status_real_docker",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[lost-stop]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[lost-start]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[postlaunch-proxy-failed]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[retained-unresolved-work]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[missing-source-credential]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[missing-source-evidence]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[missing-source-consent]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[changed-successor-config]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[changed-candidate-image]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[predecessor-generation]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[predecessor-management-generation]",
    "tests/test_management_bootstrap_upgrade_container.py::test_disposable_operator_failure_fences_real_docker[host-process-interrupted]",
)


MANAGEMENT_SKIPS = {
    f"tests/test_management_fresh_host_integration.py::{name}": "host root is required to observe the disposable Docker proxy"
    for name in (
        "test_real_docker_published_listener_is_owned_by_replacement",
        "test_real_docker_listener_rejects_mismatched_container_address",
        "test_real_docker_listener_rejects_changed_container_inspection",
        "test_real_listener_accepts_only_mount_order_changes",
    )
}
MANAGEMENT_SKIPS["tests/test_management_fresh_host_integration.py::test_real_prior_supervisor_binds_instance_and_published_port"] = "production-profile image is not available locally"

FRESH_REQUIRED = tuple(f"tests/test_management_archived_cutover_container.py::test_real_second_cutover_preserves_all_owner_and_key_exclusions[{variant}]" for variant in ("complete", "host-interrupted", "omitted-ancestor", "stopped-prior")) + tuple(
    f"tests/test_management_installed_fresh_container.py::test_installed_initial_abc_real_handoff[{variant}]" for variant in ("complete", "before-stop", "before-forward-start", "ready-corrupt", "host-interrupted", "source-unavailable")
)

CONTAINER_REQUIRED = (
    tuple(
        f"tests/test_supervisor_container_integration.py::{name}"
        for name in (
            "test_production_entrypoint_is_private_pid1_without_docker_socket",
            "test_release_beta_held_drain_replay_restart_and_crash_reconstruction",
            "test_real_process_issues_bootstrap_and_staged_ready_binding",
            "test_independently_prepared_staged_restart_preserves_held_intent",
        )
    )
    + (
        "tests/test_worker_network_integration.py::test_three_target_manager_restart_peer_isolation_and_lost_response",
        "tests/test_management_beta_deploy_container.py::test_authenticated_beta_source_installs_real_version_and_reconciles_after_lost_reply",
        "tests/test_management_new_instance_first_launch_docker.py::test_one_genuinely_new_docker_launch_and_retained_grant_exact_child_readback[False]",
        "tests/test_management_new_instance_first_launch_docker.py::test_one_genuinely_new_docker_launch_and_retained_grant_exact_child_readback[True]",
        "tests/test_management_new_instance_first_launch_docker.py::test_new_docker_loopback_mcp_reaches_original_authenticated_child_from_host",
        "tests/test_management_new_instance_first_launch_docker.py::test_new_docker_loopback_refuses_live_prior_host_listener_without_first_claim",
        "tests/test_management_new_instance_first_launch_docker.py::test_real_different_os_uid_cannot_claim_original_new_docker_first_start",
        "tests/test_management_new_instance_first_launch_docker.py::test_new_docker_loopback_refuses_live_prior_network_listener_without_claim",
        "tests/test_management_new_instance_first_launch_docker.py::test_new_docker_rejects_prior_process_joining_network_after_durable_claim",
        "tests/test_management_new_instance_first_launch_docker.py::test_new_docker_refuses_external_first_start_racing_final_engine_command",
        "tests/test_management_new_docker_network_policy.py::test_loopback_publication_refuses_legacy_or_unverifiable_docker_engine[27.5.1-False]",
        "tests/test_management_new_docker_network_policy.py::test_dedicated_new_bridge_rejects_ipv6_even_with_masquerading_disabled[True]",
        "tests/test_management_new_instance_first_launch_docker.py::test_real_new_first_launch_never_reports_readiness_from_invalid_readback[revoked-grant]",
        "tests/test_management_new_instance_first_launch_docker.py::test_real_new_first_launch_never_reports_readiness_from_invalid_readback[stale-observation]",
        "tests/test_management_new_instance_first_launch_docker.py::test_real_new_start_refuses_changed_original_with_zero_docker_launches[bootstrap_config]",
        "tests/test_management_new_instance_first_launch_docker.py::test_killed_first_host_preserves_consumed_claim_without_docker_start",
    )
    + tuple(
        f"tests/test_beta_runtime_smoke_container.py::{name}"
        for name in (
            "test_local_smoke_uses_installed_worker_real_progress_and_original_journal",
            "test_local_smoke_diagnostics_do_not_advance_real_worker_progress",
            "test_local_smoke_preserves_original_evidence_across_real_faults",
            "test_local_smoke_later_selection_preserves_proof_and_publication_authority",
            "test_local_smoke_selected_retry_and_same_sha_supersession",
            "test_local_smoke_distinct_revision_and_worker_lifecycle_outcomes",
        )
    )
)

FRESH_SELECTORS = (
    "tests/test_management_fresh_host_integration.py::test_real_docker_published_listener_is_owned_by_replacement",
    "tests/test_management_fresh_host_integration.py::test_real_docker_listener_rejects_mismatched_container_address",
    "tests/test_management_fresh_host_integration.py::test_real_docker_listener_rejects_changed_container_inspection",
    "tests/test_management_fresh_host_integration.py::test_real_listener_accepts_only_mount_order_changes",
    "tests/test_management_fresh_host_integration.py::test_real_prior_supervisor_binds_instance_and_published_port",
    "tests/test_management_fresh_end_to_end.py",
    "tests/test_management_archived_cutover_container.py",
    "tests/test_management_retained_startup_container.py",
    "tests/test_management_proxy_watcher_process_integration.py",
    *FRESH_REQUIRED[4:],
)

SELECTORS = {
    "management-self-update": (
        "-m",
        "supervisor_integration",
        "tests/test_management_self_update_integration.py",
        "tests/test_management_self_update_failures.py",
        "tests/test_management_clean_restart_integration.py",
        "tests/test_management_fresh_host_integration.py",
    ),
    "fresh-host": ("-o", "timeout=240", *FRESH_SELECTORS),
    "isolated-containers": (
        "-m",
        "supervisor_integration",
        "tests/test_supervisor_container_integration.py",
        "tests/test_worker_network_integration.py",
        "tests/test_management_beta_deploy_container.py",
        "tests/test_beta_runtime_smoke_container.py",
        "tests/test_management_new_instance_docker_created.py",
        "tests/test_management_new_docker_network_policy.py",
        "tests/test_management_new_instance_first_launch_docker.py",
    ),
}

# These tracked inputs are fingerprinted in addition to source HEAD, the lock,
# the effective pytest configuration, and the selected environment variables.
HARNESS = (
    "tests/conftest.py",
    "tests/ci_timing_plugin.py",
    "tests/fixtures/ci_timing.py",
    "scripts/supervisor_ci_profiles.py",
    "scripts/supervisor_ci_results.py",
    "scripts/ci_shard_plugin.py",
    "scripts/plan_ci_shards.py",
    "src/auto_coder/ci_shard_plan.py",
)
SEMANTIC_ENV = (
    "PYTHONPATH",
    "SUPERVISOR_ALLOWED_SKIPS",
    "SUPERVISOR_REQUIRED_NODEIDS",
    "AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION",
    "AUTO_CODER_INTEGRATION_UV_CACHE_DIR",
)


def profile_args(profile: str) -> tuple[str, ...]:
    if profile not in PROFILES:
        raise ShardPlanError(f"unknown Supervisor profile: {profile}")
    return ("-p", "supervisor_ci_results", *SELECTORS[profile])


def anchors(profile: str) -> tuple[str, ...]:
    return FRESH_REQUIRED if profile == "fresh-host" else CONTAINER_REQUIRED if profile == "isolated-containers" else ()


def context(profile: str, *, environ: dict[str, str] | None = None, checkout: str | None = None) -> ShardContext:
    env = environ if environ is not None else dict(os.environ)
    sha = checkout or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    return ShardContext.parse(
        {
            "schema_version": "ci-shard-context-v1",
            "repository": REPOSITORY,
            "profile_id": profile,
            "checkout_sha": sha,
            "pytest_args": list(profile_args(profile)),
            "harness_files": list(HARNESS),
            "semantic_environment": list(SEMANTIC_ENV),
            "origin": {
                "repository": REPOSITORY,
                "workflow": env.get("GITHUB_WORKFLOW"),
                "run_id": env.get("GITHUB_RUN_ID"),
                "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
                "planning_attempt": f"{profile}-{env.get('GITHUB_RUN_ATTEMPT', 'local')}",
                "event_sha": env.get("GITHUB_SHA"),
            },
        }
    )


def check_plan(profile: str, path: Path) -> tuple[ShardContext, int]:
    plan, _raw = read_plan(path)
    if plan.context.profile_id != profile or plan.context.repository != REPOSITORY:
        raise ShardPlanError("Supervisor plan is for a different profile/repository")
    if plan.context.pytest_args != profile_args(profile):
        raise ShardPlanError("Supervisor plan does not use the original full selectors")
    if plan.context.harness_files != HARNESS or plan.context.semantic_environment != SEMANTIC_ENV:
        raise ShardPlanError("Supervisor plan has an incomplete or stale harness/environment fingerprint")
    if not set(anchors(profile)).issubset(plan.inventory):
        raise ShardPlanError("Supervisor full-profile required anchor is absent (possibly a lost marker)")
    return plan.context, len(plan.shards)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("configure", "context", "args", "matrix", "verify"))
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--github-env", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.operation == "configure":
        if args.github_env is None:
            parser.error("configure requires --github-env")
        values = {
            "SUPERVISOR_REQUIRED_NODEIDS": json.dumps(anchors(args.profile), separators=(",", ":")),
        }
        if args.profile == "management-self-update":
            values["SUPERVISOR_ALLOWED_SKIPS"] = json.dumps(MANAGEMENT_SKIPS, separators=(",", ":"))
        with args.github_env.open("a", encoding="utf-8") as stream:
            for name, value in values.items():
                stream.write(f"{name}={value}\n")
    elif args.operation == "context":
        if args.output is None:
            parser.error("context requires --output")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(context(args.profile).json(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    elif args.operation == "args":
        for value in profile_args(args.profile):
            print(value)
    else:
        if args.plan is None:
            parser.error("matrix/verify requires --plan")
        _context, count = check_plan(args.profile, args.plan)
        if args.operation == "matrix":
            if args.github_output is None:
                parser.error("matrix requires --github-output")
            matrix = {"include": [{"shard": number} for number in range(1, count + 1)]}
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write("matrix=" + json.dumps(matrix, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
