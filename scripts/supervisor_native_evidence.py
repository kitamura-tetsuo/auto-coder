"""Bind Supervisor pytest inventories to actual GitHub Actions job executions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from loguru import logger
from supervisor_ci_profiles import MANAGEMENT_SKIPS, PROFILES, REPOSITORY, check_plan

from auto_coder.ci_shard_plan import VerifiedPlan, read_plan
from auto_coder.util.gh_cache import GitDataResponse, GitHubGitDataClient

RECEIPT_SCHEMA = "supervisor-native-job-receipt-v1"
WORKFLOW_PATH = ".github/workflows/supervisor-integration-tests.yml"
LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level}</level> | {file}:{function}:{line} | {message}"


class NativeEvidenceError(ValueError):
    """A native job, retained identity, or test receipt is not provable."""


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise NativeEvidenceError(f"{description} must be an object")
    return cast(dict[str, object], value)


def _read(path: Path) -> dict[str, object]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, UnicodeError, ValueError) as error:
        raise NativeEvidenceError(f"unreadable {path}: {error}") from error


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _api_object(response: GitDataResponse, operation: str) -> dict[str, object]:
    if response.status != 200:
        raise NativeEvidenceError(f"GitHub read-only {operation} returned HTTP {response.status}")
    return _object(response.data, operation)


def jobs_for_attempt(client: GitHubGitDataClient, run_id: int, attempt: int) -> list[dict[str, object]]:
    """Read all jobs; partial API pagination is never considered complete."""
    jobs: list[dict[str, object]] = []
    total: int | None = None
    for page in range(1, 21):
        data = _api_object(client.request("GET", f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}"), "workflow jobs")
        if type(data.get("total_count")) is not int or cast(int, data["total_count"]) < 0:
            raise NativeEvidenceError("GitHub native job count is missing")
        if total is None:
            total = cast(int, data["total_count"])
        if total != data["total_count"]:
            raise NativeEvidenceError("GitHub native job pagination count changed")
        page_jobs = data.get("jobs")
        if not isinstance(page_jobs, list):
            raise NativeEvidenceError("GitHub native jobs are missing")
        jobs.extend(_object(item, "native job") for item in page_jobs)
        if len(jobs) >= total:
            break
        if not page_jobs:
            raise NativeEvidenceError("GitHub native job list is truncated")
    if total is None or len(jobs) != total or len({job.get("id") for job in jobs}) != total:
        raise NativeEvidenceError("GitHub native job list is incomplete or has duplicate IDs")
    return jobs


def run_for_attempt(client: GitHubGitDataClient, run_id: int, attempt: int) -> dict[str, object]:
    run = _api_object(client.request("GET", f"actions/runs/{run_id}/attempts/{attempt}"), "workflow attempt")
    if run.get("id") != run_id or run.get("run_attempt") != attempt or run.get("path") != WORKFLOW_PATH:
        raise NativeEvidenceError("foreign workflow run/attempt or workflow path")
    return run


def native_signature(job: dict[str, object]) -> tuple[object, ...]:
    """The full, execution-specific signature excludes GitHub's alias job ID."""
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        raise NativeEvidenceError("native job has no complete step evidence")
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise NativeEvidenceError("native job did not finish successfully")
    signature = []
    for item in steps:
        step = _object(item, "native step")
        if step.get("status") != "completed" or step.get("conclusion") not in ("success", "skipped"):
            raise NativeEvidenceError(f"native step not successful or conditionally skipped: {step.get('name')}")
        if step.get("conclusion") == "success" and (not step.get("started_at") or not step.get("completed_at")):
            raise NativeEvidenceError("native step timestamps are missing")
        signature.append(tuple(step.get(name) for name in ("number", "name", "status", "conclusion", "started_at", "completed_at")))
    fields = tuple(job.get(name) for name in ("name", "runner_id", "runner_name", "started_at", "completed_at", "conclusion"))
    if not all(job.get(name) is not None for name in ("name", "runner_name", "started_at", "completed_at")):
        raise NativeEvidenceError("native job runner/timestamp identity is missing")
    return (*fields, tuple(signature))


def original_producer(current: dict[str, object], history: list[tuple[int, dict[str, object]]], current_attempt: int) -> tuple[int, dict[str, object]]:
    """Native failed-job reruns may reissue an ID for a previously run job."""
    signature = native_signature(current)
    matches = [(attempt, job) for attempt, job in history if attempt <= current_attempt and job.get("status") == "completed" and job.get("conclusion") == "success" and native_signature(job) == signature]
    if not matches:
        raise NativeEvidenceError("native job has no producing execution")
    earliest = min(attempt for attempt, _ in matches)
    origins = [(attempt, job) for attempt, job in matches if attempt == earliest]
    if len(origins) != 1:
        raise NativeEvidenceError("ambiguous original producing native job")
    # A duplicate actual job in a later attempt with identical original timing
    # and different ID is accepted only as a native alias of this sole origin.
    if len({(attempt, job.get("id")) for attempt, job in matches}) != len(matches):
        raise NativeEvidenceError("duplicated native job history")
    return origins[0]


@dataclass(frozen=True)
class EvidenceSelection:
    profile: str
    shard: int | None
    job: dict[str, object]
    producing_attempt: int
    producing_job: dict[str, object]
    artifact: Path
    receipt: dict[str, object]
    inventories: tuple[dict[str, object], ...]


def _inventory_ok(report: dict[str, object], plan: VerifiedPlan | None, profile: str, shard: int | None, sha: str, run_id: int, attempt: int) -> None:
    if report.get("schema_version") != "supervisor-native-inventory-v2" or report.get("checkout_sha") != sha or report.get("profile") != profile:
        raise NativeEvidenceError("native pytest inventory source/profile mismatch")
    if report.get("run_id") != str(run_id) or report.get("run_attempt") != str(attempt) or report.get("shard") != shard:
        raise NativeEvidenceError("native pytest inventory attempt/shard mismatch")
    expected = plan.shards[shard - 1] if plan is not None and shard is not None else None
    collected = report.get("collected")
    outcomes = report.get("outcomes")
    if not isinstance(collected, list) or not collected or any(not isinstance(item, str) for item in collected):
        raise NativeEvidenceError("native pytest collected inventory is empty or invalid")
    if len(set(collected)) != len(collected) or (expected is not None and collected != list(expected)):
        raise NativeEvidenceError("native pytest collected inventory differs from immutable assignment")
    if not isinstance(outcomes, dict) or set(outcomes) != set(collected) or not set(outcomes.values()) <= {"passed", "skipped"}:
        raise NativeEvidenceError("native pytest outcomes are missing, extra, failed, cancelled or forbidden-skipped")
    if profile != "management-self-update" and any(value == "skipped" for value in outcomes.values()):
        raise NativeEvidenceError("mandatory privileged/container test was skipped")
    reasons = report.get("skip_reasons")
    if not isinstance(reasons, dict) or set(reasons) != {name for name, outcome in outcomes.items() if outcome == "skipped"}:
        raise NativeEvidenceError("native skip reasons do not match reported skips")
    for node, reason in reasons.items():
        allowed = next((value for key, value in MANAGEMENT_SKIPS.items() if node == key or str(node).startswith(f"{key}[")), None) if profile == "management-self-update" else None
        if allowed is None or not isinstance(reason, str) or allowed not in reason:
            raise NativeEvidenceError("unauthorized or unproven native management skip")
    if any(report.get(field) for field in ("missing", "missing_selection", "unexpected", "repeated")):
        raise NativeEvidenceError("native pytest inventory contains incomplete/duplicate execution")
    if report.get("plan_digest") != (plan.digest if plan is not None else None):
        raise NativeEvidenceError("native pytest inventory plan digest mismatch")
    if report.get("plan_origin") != (plan.context.origin if plan is not None else None):
        raise NativeEvidenceError("native pytest inventory producing plan origin mismatch")
    if not isinstance(report.get("first_case_phase_epoch"), (float, int)) or not isinstance(report.get("last_case_phase_epoch"), (float, int)):
        raise NativeEvidenceError("native pytest case-phase timestamps absent")
    if cast(float, report["last_case_phase_epoch"]) < cast(float, report["first_case_phase_epoch"]):
        raise NativeEvidenceError("native pytest case-phase interval is reversed")


def native_job_name(profile: str, shard: int | None) -> str:
    return "Worker processes and bridge" if profile == "worker" else f"Supervisor {profile} (shard {shard})"


def capture(client: GitHubGitDataClient, *, profile: str, shard: int | None, plan_file: Path | None, inventories: tuple[Path, ...], destination: Path, environ: dict[str, str]) -> dict[str, object]:
    if not inventories or (profile == "worker") != (plan_file is None):
        raise NativeEvidenceError("worker and planned shard inventory parameters are inconsistent")
    plan = read_plan(plan_file)[0] if plan_file is not None else None
    if plan is not None:
        check_plan(profile, plan_file)
    run_id = int(environ["GITHUB_RUN_ID"])
    attempt = int(environ["GITHUB_RUN_ATTEMPT"])
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if plan is not None and (sha != plan.context.checkout_sha or plan.context.origin.get("run_id") != str(run_id)):
        raise NativeEvidenceError("recorded Supervisor plan is from another checkout/run")
    runner_name = environ["RUNNER_NAME"]
    expected_name = native_job_name(profile, shard)
    selected: list[dict[str, object]] = []
    for _attempt in range(8):
        jobs = jobs_for_attempt(client, run_id, attempt)
        selected = [job for job in jobs if job.get("name") == expected_name and job.get("runner_name") == runner_name and job.get("run_id") == run_id]
        if len(selected) == 1:
            break
        time.sleep(2)
    if len(selected) != 1 or type(selected[0].get("id")) is not int:
        raise NativeEvidenceError("could not uniquely identify the live producing Actions job")
    job = selected[0]
    for file in inventories:
        report = _read(file)
        _inventory_ok(report, plan, profile, shard, sha, run_id, attempt)
    payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "repository": REPOSITORY,
        "run_id": run_id,
        "run_attempt": attempt,
        "checkout_sha": sha,
        "event_sha": environ.get("GITHUB_SHA"),
        "profile": profile,
        "shard": shard,
        "plan_digest": plan.digest if plan is not None else None,
        "plan_origin_attempt": plan.context.origin.get("run_attempt") if plan is not None else None,
        "job_id": job["id"],
        "job_name": expected_name,
        "job_key": environ.get("GITHUB_JOB"),
        "runner_name": runner_name,
        "runner_id": job.get("runner_id"),
        "job_started_at": job.get("started_at"),
        "inventories": {file.name: _sha(file) for file in inventories},
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise NativeEvidenceError("native receipt destination already exists")
    destination.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return payload


def _logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, colorize=True, format=LOG_FORMAT)
    logger.add(directory / "supervisor-native-evidence.log", colorize=False, format=LOG_FORMAT, rotation="10 MB", retention=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, choices=(*PROFILES, "worker"))
    parser.add_argument("--shard", type=int)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--inventory", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _logging(args.output.parent)
    try:
        payload = capture(
            GitHubGitDataClient(os.environ.get("GITHUB_TOKEN", ""), REPOSITORY),
            profile=args.profile,
            shard=args.shard,
            plan_file=args.plan,
            inventories=tuple(args.inventory),
            destination=args.output,
            environ=dict(os.environ),
        )
        logger.info("Bound Supervisor {} shard {} to native job {}", args.profile, args.shard, payload["job_id"])
    except (NativeEvidenceError, OSError, ValueError, KeyError, RuntimeError) as error:
        logger.error("Supervisor native evidence refused: {}", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
