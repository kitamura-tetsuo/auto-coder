"""Fail-closed Supervisor shard aggregation across native Actions rerun attempts."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import cast

from supervisor_ci_profiles import PROFILES, REPOSITORY, check_plan
from supervisor_native_evidence import (
    RECEIPT_SCHEMA,
    EvidenceSelection,
    NativeEvidenceError,
    _inventory_ok,
    _object,
    _read,
    _sha,
    jobs_for_attempt,
    native_job_name,
    original_producer,
    run_for_attempt,
)

from auto_coder.ci_shard_plan import VerifiedPlan, read_plan
from auto_coder.util.gh_cache import GitHubGitDataClient

REQUIRED_GROUPS = {"worker-bridge", "operator-upgrade", "prepare-management", "prepare-fresh-host", "prepare-containers", *PROFILES}
ARTIFACT = re.compile(r"supervisor-integration-(worker-bridge|operator-upgrade|management-self-update-shard-[0-9]+|fresh-host-shard-[0-9]+|isolated-containers-shard-[0-9]+)-([0-9]+)-([0-9]+)\Z")


def _status(results: object) -> None:
    values = _object(results, "REQUIRED_RESULTS")
    if set(values) != REQUIRED_GROUPS:
        raise NativeEvidenceError(f"missing or unexpected required groups: {set(values) ^ REQUIRED_GROUPS}")
    failed = {name: _object(data, name).get("result") for name, data in values.items() if _object(data, name).get("result") != "success"}
    if failed:
        raise NativeEvidenceError(f"native required jobs did not succeed: {failed}")


def _plans(directory: Path, sha: str, event_sha: str, run_id: int, current_attempt: int) -> dict[str, VerifiedPlan]:
    plans: dict[str, VerifiedPlan] = {}
    for profile in PROFILES:
        path = directory / profile / "plan.json"
        context, _count = check_plan(profile, path)
        plan, _raw = read_plan(path)
        if context.checkout_sha != sha or context.origin.get("event_sha") != event_sha:
            raise NativeEvidenceError(f"{profile}: plan tested source is not the actual Actions checkout/event")
        if context.origin.get("run_id") != str(run_id) or context.origin.get("workflow") != "Supervisor Integration Tests":
            raise NativeEvidenceError(f"{profile}: plan is from another workflow/run")
        recorded_attempt = context.origin.get("run_attempt")
        if not isinstance(recorded_attempt, str) or not recorded_attempt.isdigit() or not 1 <= int(recorded_attempt) <= current_attempt:
            raise NativeEvidenceError(f"{profile}: invalid producing planning attempt")
        if _read(directory / profile / "context.json") != context.json():
            raise NativeEvidenceError(f"{profile}: plan and original context artifact differ")
        plans[profile] = plan
    return plans


def _receipts(directory: Path, run_id: int, current_attempt: int) -> dict[tuple[str, int | None, int, int], tuple[Path, dict[str, object]]]:
    receipts: dict[tuple[str, int | None, int, int], tuple[Path, dict[str, object]]] = {}
    for folder in sorted(directory.iterdir()):
        if not folder.is_dir():
            raise NativeEvidenceError("unexpected non-artifact file in Supervisor evidence directory")
        match = ARTIFACT.fullmatch(folder.name)
        if match is None:
            raise NativeEvidenceError(f"unknown native Supervisor artifact: {folder.name}")
        description, artifact_run, artifact_attempt = match.groups()
        if int(artifact_run) != run_id or not 1 <= int(artifact_attempt) <= current_attempt:
            raise NativeEvidenceError("foreign run/attempt artifact supplied to Supervisor gate")
        receipt_file = folder / "receipt.json"
        if not receipt_file.is_file():
            # Failed earlier attempts may leave diagnostics but no valid receipt.
            continue
        receipt = _read(receipt_file)
        profile = receipt.get("profile")
        shard = receipt.get("shard")
        if not isinstance(profile, str) or (shard is not None and type(shard) is not int):
            raise NativeEvidenceError("malformed native receipt profile/shard")
        expected_description = (
            "worker-bridge"
            if profile == "worker"
            else "operator-upgrade"
            if profile == "operator-upgrade"
            else f"{profile}-shard-{shard}"
        )
        if description != expected_description:
            raise NativeEvidenceError("native artifact label and receipt profile/shard differ")
        if receipt.get("schema_version") != RECEIPT_SCHEMA or receipt.get("repository") != REPOSITORY:
            raise NativeEvidenceError("foreign or malformed Supervisor receipt")
        if receipt.get("run_id") != run_id or receipt.get("run_attempt") != int(artifact_attempt) or type(receipt.get("job_id")) is not int:
            raise NativeEvidenceError("native artifact/run/attempt/job attribution mismatch")
        key = (profile, cast(int | None, shard), int(artifact_attempt), cast(int, receipt["job_id"]))
        if key in receipts:
            raise NativeEvidenceError("duplicate receipt for a producing native job")
        receipts[key] = (folder, receipt)
    return receipts


def _select(
    name: str,
    profile: str,
    shard: int | None,
    current_jobs: list[dict[str, object]],
    history: list[tuple[int, dict[str, object]]],
    receipts: dict[tuple[str, int | None, int, int], tuple[Path, dict[str, object]]],
    plans: dict[str, VerifiedPlan],
    sha: str,
    event_sha: str,
    run_id: int,
    attempt: int,
) -> EvidenceSelection:
    matches = [job for job in current_jobs if job.get("name") == name]
    if len(matches) != 1:
        raise NativeEvidenceError(f"missing or duplicate current native job: {name}")
    job = matches[0]
    producer_attempt, producer = original_producer(job, history, attempt)
    if producer.get("run_id") != run_id or producer.get("run_attempt") != producer_attempt or type(producer.get("id")) is not int:
        raise NativeEvidenceError(f"{name}: original job has foreign run/attempt identity")
    identity = (profile, shard, producer_attempt, cast(int, producer["id"]))
    result = receipts.get(identity)
    if result is None:
        raise NativeEvidenceError(f"{name}: missing original producing-job receipt (retained alias cannot prove PASS)")
    folder, receipt = result
    plan = plans.get(profile)
    if receipt.get("checkout_sha") != sha or receipt.get("event_sha") != event_sha or receipt.get("job_name") != name:
        raise NativeEvidenceError(f"{name}: original receipt source/name mismatch")
    if receipt.get("plan_digest") != (plan.digest if plan is not None else None):
        raise NativeEvidenceError(f"{name}: mixed original plan digest")
    if plan is not None and receipt.get("plan_origin_attempt") != plan.context.origin.get("run_attempt"):
        raise NativeEvidenceError(f"{name}: original plan producing attempt changed")
    if receipt.get("runner_name") != producer.get("runner_name") or receipt.get("runner_id") != producer.get("runner_id") or receipt.get("job_started_at") != producer.get("started_at"):
        raise NativeEvidenceError(f"{name}: original receipt runner/timestamp differs from native job")
    if receipt.get("job_key") != ("worker-bridge" if profile == "worker" else profile):
        raise NativeEvidenceError(f"{name}: original receipt logical Actions job key mismatch")
    files = _object(receipt.get("inventories"), "receipt.inventories")
    if profile == "worker":
        expected_names = ("worker-bridge.json", "worker-daemon.json")
    elif profile == "operator-upgrade":
        expected_names = ("operator-upgrade.json",)
    else:
        expected_names = (f"{profile}-{shard}.json",)
    if set(files) != set(expected_names):
        raise NativeEvidenceError(f"{name}: missing or extra original native pytest inventories")
    inventories = []
    for filename in expected_names:
        path = folder / filename
        if _sha(path) != files[filename]:
            raise NativeEvidenceError(f"{name}: native pytest inventory digest mismatch")
        report = _read(path)
        _inventory_ok(report, plan, profile, shard, sha, run_id, producer_attempt)
        if report.get("event_sha") != event_sha or report.get("job_key") != receipt.get("job_key") or report.get("runner_name") != receipt.get("runner_name"):
            raise NativeEvidenceError(f"{name}: report and producing job identity disagree")
        # _inventory_ok enforces the five management-only skip reasons for exact
        # functions and their parameterizations, and rejects all other skips.
        inventories.append(report)
    return EvidenceSelection(profile, shard, job, producer_attempt, producer, folder, receipt, tuple(inventories))


def _timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise NativeEvidenceError("native job timestamp is missing")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _metrics(selections: list[EvidenceSelection], plan_dir: Path) -> dict[str, object]:
    metrics: dict[str, object] = {}
    for profile in (*PROFILES, "worker", "operator-upgrade"):
        group = [item for item in selections if item.profile == profile]
        phases = [(cast(float, report["first_case_phase_epoch"]), cast(float, report["last_case_phase_epoch"])) for item in group for report in item.inventories]
        jobs = [(_timestamp(item.producing_job.get("started_at")), _timestamp(item.producing_job.get("completed_at"))) for item in group]
        changes = sorted([(start, 1) for start, _end in phases] + [(end, -1) for _start, end in phases])
        active = peak = 0
        for _when, delta in changes:
            active += delta
            peak = max(peak, active)
        overlap = sum(max(0.0, min(a[1], b[1]) - max(a[0], b[0])) for index, a in enumerate(phases) for b in phases[index + 1 :])
        waves: list[dict[str, object]] = []
        for start, end in sorted(phases):
            if not waves or start > cast(float, waves[-1]["end_epoch"]):
                waves.append({"start_epoch": start, "end_epoch": end, "partitions": 1})
            else:
                waves[-1]["end_epoch"] = max(cast(float, waves[-1]["end_epoch"]), end)
                waves[-1]["partitions"] = cast(int, waves[-1]["partitions"]) + 1
        queue = [max(0.0, start - _timestamp(item.producing_job["created_at"])) for item, (start, _end) in zip(group, jobs) if item.producing_job.get("created_at")]
        plan_semantic = _object(_read(plan_dir / profile / "plan.json").get("semantic"), "plan semantic") if profile in PROFILES else {}
        metrics[profile] = {
            "shards": len(group),
            "queue_seconds": queue,
            "runner_work_seconds": sum(end - start for start, end in jobs),
            "test_phase_seconds": sum(end - start for start, end in phases),
            "setup_before_test_seconds": [max(0.0, phase[0] - job[0]) for phase, job in zip(phases, jobs)] if len(phases) == len(jobs) else None,
            "phase_overlap_pair_seconds": overlap,
            "peak_overlapping_test_phases": peak,
            "observed_execution_waves": waves,
            "test_phase_critical_path_seconds": max((end for _start, end in phases), default=0) - min((start for start, _end in phases), default=0),
            "predicted_work_ms": plan_semantic.get("predicted_total_ms"),
            "capacity_limited": plan_semantic.get("capacity_limited"),
            "effective_capacity_ms": plan_semantic.get("effective_capacity_ms"),
            "over_budget_nodes": [node for shard in cast(list[dict[str, object]], plan_semantic.get("shards", [])) for node in cast(list[str], shard["over_budget_nodes"])],
        }
    return metrics


def evaluate(
    client: GitHubGitDataClient,
    results: object,
    plan_dir: Path,
    artifact_dir: Path,
    sha: str,
    event_sha: str,
    run_id: int,
    attempt: int,
) -> tuple[list[EvidenceSelection], dict[str, object]]:
    _status(results)
    if not re.fullmatch("[0-9a-f]{40}", sha) or not re.fullmatch("[0-9a-f]{40}", event_sha) or attempt < 1 or run_id < 1:
        raise NativeEvidenceError("actual workflow source/run identifiers are invalid")
    plans = _plans(plan_dir, sha, event_sha, run_id, attempt)
    receipts = _receipts(artifact_dir, run_id, attempt)
    history: list[tuple[int, dict[str, object]]] = []
    head_sha: object = None
    current_jobs: list[dict[str, object]] = []
    for number in range(1, attempt + 1):
        run = run_for_attempt(client, run_id, number)
        if head_sha is None:
            head_sha = run.get("head_sha")
        if run.get("head_sha") != head_sha:
            raise NativeEvidenceError("mixed source in native workflow attempts")
        jobs = jobs_for_attempt(client, run_id, number)
        history.extend((number, job) for job in jobs)
        if number == attempt:
            current_jobs = jobs
    expected = {
        native_job_name("worker", None): ("worker", None),
        native_job_name("operator-upgrade", None): ("operator-upgrade", None),
    }
    for profile, plan in plans.items():
        for shard in range(1, len(plan.shards) + 1):
            expected[native_job_name(profile, shard)] = (profile, shard)
    observed = [job.get("name") for job in current_jobs if isinstance(job.get("name"), str) and (job["name"] in {"Worker processes and bridge", "Operator upgrade production-origin Docker"} or any(cast(str, job["name"]).startswith(f"Supervisor {profile} (") for profile in PROFILES))]
    if sorted(observed) != sorted(expected):
        raise NativeEvidenceError("missing/duplicate/extra native Supervisor matrix jobs")
    selections = [_select(name, profile, shard, current_jobs, history, receipts, plans, sha, event_sha, run_id, attempt) for name, (profile, shard) in expected.items()]
    for profile, plan in plans.items():
        ordered = [node for item in sorted((item for item in selections if item.profile == profile), key=lambda item: cast(int, item.shard)) for report in item.inventories for node in cast(list[str], report["collected"])]
        if tuple(ordered) != plan.inventory:
            raise NativeEvidenceError(f"{profile}: actual aggregate does not cover the complete immutable profile inventory exactly once")
    return selections, _metrics(selections, plan_dir)
