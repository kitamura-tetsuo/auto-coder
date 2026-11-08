"""Diagnostic comparison of actual source shards with the former four-way split."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .ci_shard_plan import VerifiedPlan, _object
from .source_ci_evidence import PLAN_JOB_NAME, _all_pages, _artifact_name, _time
from .util.gh_cache import GitHubGitDataClient


def legacy_four_groups(inventory: tuple[str, ...], snapshot: Path) -> tuple[tuple[str, ...], ...]:
    """Reproduce pytest-split duration_based_chunks on the SAME inventory."""
    content = json.loads(snapshot.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError("legacy duration snapshot must be a node-duration mapping")
    known = {node: content[node] for node in inventory if node in content}
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in known.values()):
        raise ValueError("legacy duration snapshot has invalid native durations")
    fallback = sum(known.values()) / len(known) if known else 1.0
    weights = [cast(float, known.get(node, fallback)) for node in inventory]
    target = sum(weights) / 4
    groups: list[list[str]] = [[], [], [], []]
    totals = [0.0, 0.0, 0.0, 0.0]
    group = 0
    for node, duration in zip(inventory, weights):
        if totals[group] >= target:
            group += 1
        if group >= 4:
            raise ValueError("legacy splitter cannot represent the original complete selection")
        groups[group].append(node)
        totals[group] += duration
    return tuple(tuple(item) for item in groups)


def _seconds(later: object, earlier: object) -> float:
    return round((_time(later, "later timestamp") - _time(earlier, "earlier timestamp")).total_seconds(), 3)


def build_report(client: GitHubGitDataClient, plan: VerifiedPlan, artifacts_root: Path, legacy_snapshot: Path, run_id: int, attempt: int, verified: bool) -> dict[str, object]:
    jobs = _all_pages(client, f"actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    run = client.request("GET", f"actions/runs/{run_id}/attempts/{attempt}")
    if run.status != 200:
        raise ValueError("native run timestamps unavailable")
    run_info = _object(run.data, "run")
    planned = _object(plan.context.origin, "plan.origin")
    shards: list[dict[str, object]] = []
    native_ms: dict[str, float] = {}
    for number, selected in enumerate(plan.shards, 1):
        name = f"Complete source tests ({number}/{len(plan.shards)})"
        matching = [job for job in jobs if job.get("name") == name]
        if len(matching) != 1:
            continue
        job = matching[0]
        test_steps = [step for step in cast(list[dict[str, object]], job.get("steps", [])) if step.get("name") == "Run complete required source selection"]
        native_step = test_steps[0] if len(test_steps) == 1 else None
        queue_seconds = _seconds(job["started_at"], job["created_at"]) if job.get("started_at") and job.get("created_at") else None
        runner_seconds = _seconds(job["completed_at"], job["started_at"]) if job.get("completed_at") and job.get("started_at") else None
        setup_seconds = _seconds(native_step["started_at"], job["started_at"]) if native_step is not None and native_step.get("started_at") and job.get("started_at") else None
        test_seconds = _seconds(native_step["completed_at"], native_step["started_at"]) if native_step is not None and native_step.get("completed_at") and native_step.get("started_at") else None
        shards.append({"shard": number, "nodes": len(selected), "queue_seconds": queue_seconds, "setup_seconds": setup_seconds, "test_seconds": test_seconds, "runner_work_seconds": runner_seconds})
        if verified:
            path = artifacts_root / _artifact_name(run_id, attempt, number) / "receipt.json"
            receipt = json.loads(path.read_text(encoding="utf-8"))
            for entry in receipt["reports"]:
                native_ms[entry["nodeid"]] = native_ms.get(entry["nodeid"], 0.0) + entry["duration_ms"]
    plan_jobs = [job for job in jobs if job.get("name") == PLAN_JOB_NAME]
    quality_jobs = [job for job in jobs if job.get("name") == "Source identity and quality"]
    aggregate_jobs = [job for job in jobs if job.get("name") == "Complete source tests with Chromium"]
    all_required = plan_jobs + quality_jobs + aggregate_jobs + [job for job in jobs if str(job.get("name", "")).startswith("Complete source tests (")]
    last_completed = max((_time(job["completed_at"], "job completion") for job in all_required if job.get("completed_at")), default=None)
    end_to_end = _seconds(last_completed.isoformat(), run_info["run_started_at"]) if last_completed is not None and all(job.get("completed_at") for job in all_required) and len(all_required) == len(plan.shards) + 3 else None
    elapsed = round((datetime.now(timezone.utc) - _time(run_info["run_started_at"], "run start")).total_seconds(), 3)
    result: dict[str, object] = {
        "schema_version": "source-ci-metrics-v1",
        "repository": plan.context.repository,
        "source_sha": plan.context.checkout_sha,
        "run_id": run_id,
        "run_attempt": attempt,
        "planning_attempt": planned["planning_attempt"],
        "plan_digest": plan.digest,
        "inventory_nodes": len(plan.inventory),
        "qualification": "verified" if verified else "not_verified",
        "ten_minute_target_seconds": 600,
        "estimated_work_budget_seconds": 420,
        "shards": shards,
        "native_test_critical_path_seconds": max((cast(float, shard["test_seconds"]) for shard in shards if shard["test_seconds"] is not None), default=None),
        "total_runner_work_seconds": round(sum(cast(float, shard["runner_work_seconds"]) for shard in shards if shard["runner_work_seconds"] is not None), 3),
        "end_to_end_required_check_seconds": end_to_end,
        "end_to_end_so_far_seconds": elapsed,
    }
    if verified and set(native_ms) == set(plan.inventory):
        old_groups = legacy_four_groups(plan.inventory, legacy_snapshot)
        result["same_inventory_native_comparison"] = {
            "basis": "current actual native pytest phase durations on identical current source nodes; old four-way assignments are reconstructed from the previous checked-in duration_based_chunks snapshot, not a separate observed run",
            "current_planned_shards_native_critical_path_seconds": round(max(sum(native_ms[node] for node in group) for group in plan.shards) / 1000, 3),
            "former_four_shards_native_critical_path_seconds": round(max(sum(native_ms[node] for node in group) for group in old_groups) / 1000, 3),
            "former_four_shards_nodes": [len(group) for group in old_groups],
        }
    return result
