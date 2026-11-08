"""Exact current-attempt source-test receipts and native Actions evidence."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from .ci_shard_plan import VerifiedPlan, _canonical, _digest, _object, _pairs, _strings, read_plan
from .util.gh_cache import GitHubGitDataClient

RECEIPT_SCHEMA = "source-ci-shard-receipt-v1"
WORKFLOW_PATH = ".github/workflows/test-source-for-runtime.yml"
WORKFLOW_NAME = "Test Source for Runtime"
SOURCE_JOB = "complete-source-shard"
PLAN_JOB_NAME = "Plan complete source tests"
PHASES = ("setup", "call", "teardown")


class SourceEvidenceError(ValueError):
    """A complete native source-test qualification cannot be established."""


def _time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise SourceEvidenceError(f"{label} has no timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceEvidenceError(f"{label} has an invalid timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise SourceEvidenceError(f"{label} has no timezone")
    return result.astimezone(timezone.utc)


def _native_phases(executed: tuple[str, ...], reports: object) -> bool:
    """Accept native prerequisite skips/xfails, but never invent a PASS phase."""
    if not isinstance(reports, list):
        return False
    phases: dict[str, dict[str, dict[str, object]]] = {node: {} for node in executed}
    for entry in reports:
        if not isinstance(entry, dict) or set(entry) != {"nodeid", "phase", "outcome", "wasxfail", "duration_ms"}:
            return False
        node, phase, outcome = entry["nodeid"], entry["phase"], entry["outcome"]
        if node not in phases or phase not in PHASES or phase in phases[cast(str, node)]:
            return False
        if outcome not in ("passed", "skipped", "failed") or (entry["wasxfail"] is not None and not isinstance(entry["wasxfail"], str)):
            return False
        duration = entry["duration_ms"]
        if isinstance(duration, bool) or not isinstance(duration, (float, int)) or not 0 <= duration < float("inf"):
            return False
        phases[cast(str, node)][cast(str, phase)] = entry
    for reports_by_phase in phases.values():
        setup = reports_by_phase.get("setup")
        call = reports_by_phase.get("call")
        teardown = reports_by_phase.get("teardown")
        if setup is None or teardown is None or teardown["outcome"] != "passed":
            return False
        if setup["outcome"] == "passed":
            if call is None or call["outcome"] == "failed":
                return False
        elif setup["outcome"] == "skipped":
            if call is not None:
                return False
        else:
            return False
    return True


def receipt_complete(payload: dict[str, object], plan: VerifiedPlan, shard: int) -> bool:
    """Derive completeness from native phases and the original full plan."""
    try:
        assigned = _strings(payload.get("assigned"), "assigned", allow_empty=False)
        collected = _strings(payload.get("collected"), "collected", allow_empty=False)
        selected = _strings(payload.get("selected"), "selected", allow_empty=False)
        executed = _strings(payload.get("executed"), "executed", allow_empty=False)
    except ValueError:
        return False
    return collected == plan.inventory and assigned == selected == executed == plan.shards[shard - 1] and type(payload.get("native_exit_status")) is int and payload["native_exit_status"] == 0 and _native_phases(executed, payload.get("reports"))


def validate_receipt(payload: dict[str, object], plan: VerifiedPlan, shard: int, run_id: int, attempt: int, *, job: dict[str, object] | None = None) -> None:
    origin = plan.context.origin
    expected = {
        "schema_version": RECEIPT_SCHEMA,
        "repository": plan.context.repository,
        "source_sha": plan.context.checkout_sha,
        "profile": plan.profile,
        "plan_digest": plan.digest,
        "inventory_digest": _digest(list(plan.inventory)),
        "shard": shard,
        "shard_count": len(plan.shards),
        "run_id": run_id,
        "run_attempt": attempt,
        "planning_attempt": origin.get("planning_attempt"),
        "workflow": WORKFLOW_NAME,
        "job_key": SOURCE_JOB,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise SourceEvidenceError(f"shard {shard} receipt does not match plan/run/actual attempt identity")
    if not isinstance(payload.get("invocation_id"), str) or not payload["invocation_id"]:
        raise SourceEvidenceError(f"shard {shard} has no producing invocation identity")
    began = _time(payload.get("started_at"), "receipt start")
    ended = _time(payload.get("completed_at"), "receipt completion")
    if ended < began:
        raise SourceEvidenceError("receipt ends before execution starts")
    if payload.get("complete") is not True or not receipt_complete(payload, plan, shard):
        raise SourceEvidenceError(f"shard {shard} has incomplete or failed native pytest evidence")
    if job is not None:
        started = _time(job.get("started_at"), "native job start")
        finished = _time(job.get("completed_at"), "native job completion")
        if began < started - timedelta(seconds=5) or ended > finished + timedelta(seconds=5):
            raise SourceEvidenceError(f"shard {shard} receipt was not produced inside its native job")


def _object_response(client: GitHubGitDataClient, path: str, label: str) -> dict[str, object]:
    response = client.request("GET", path)
    if response.status != 200:
        raise SourceEvidenceError(f"{label} Actions API readback unavailable")
    return _object(response.data, label)


def _all_pages(client: GitHubGitDataClient, path: str, key: str) -> list[dict[str, object]]:
    """Read every Actions API page; never mistake a first page for all jobs."""
    collected: list[dict[str, object]] = []
    expected: int | None = None
    page = 1
    seen_ids: set[int] = set()
    while True:
        suffix = "?per_page=100" if page == 1 else f"?per_page=100&page={page}"
        payload = _object_response(client, path + suffix, key)
        total = payload.get("total_count")
        if type(total) is not int or total < 0 or total > 10_000 or (expected is not None and total != expected):
            raise SourceEvidenceError(f"Actions {key} total_count unavailable or inconsistent")
        expected = total
        value = payload.get(key)
        if not isinstance(value, list) or len(value) > 100 or (not value and len(collected) < total):
            raise SourceEvidenceError(f"Actions {key} result page is missing or truncated")
        for item in value:
            entry = _object(item, key)
            identifier = entry.get("id")
            if type(identifier) is not int or identifier in seen_ids:
                raise SourceEvidenceError(f"Actions {key} result ID is missing or duplicated")
            seen_ids.add(identifier)
            collected.append(entry)
        if len(collected) > total:
            raise SourceEvidenceError(f"Actions {key} over-reported the complete result set")
        if len(collected) == total:
            return collected
        page += 1


def _artifact_name(run_id: int, attempt: int, shard: int | None = None) -> str:
    prefix = f"source-test-plan-{run_id}-{attempt}" if shard is None else f"source-test-shard-{run_id}-{attempt}-{shard}"
    return prefix


def verify_current_source_attempt(client: GitHubGitDataClient, run_id: int, attempt: int, source_sha: str, plan_path: Path, artifacts_root: Path) -> VerifiedPlan:
    """Reject partial reruns, latest-job aliases, mixed plans and false-green jobs."""
    if type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0:
        raise SourceEvidenceError("run and attempt must be positive integers")
    plan, _raw = read_plan(plan_path)
    origin = plan.context.origin
    if (
        plan.context.checkout_sha != source_sha
        or origin.get("repository") != plan.context.repository
        or origin.get("workflow") != WORKFLOW_NAME
        or origin.get("run_id") != str(run_id)
        or origin.get("run_attempt") != str(attempt)
        or origin.get("planning_attempt") != f"source-plan-{run_id}-{attempt}"
        or not isinstance(origin.get("event_sha"), str)
    ):
        raise SourceEvidenceError("plan is not from this exact current source workflow attempt")
    run = _object_response(client, f"actions/runs/{run_id}/attempts/{attempt}", "run")
    # Actions run.head_sha identifies the PR head; github.sha in a PR job is
    # the separate ephemeral merge checkout used by source-quality.
    if any(run.get(key) != value for key, value in {"id": run_id, "run_attempt": attempt, "head_sha": source_sha, "path": WORKFLOW_PATH}.items()):
        raise SourceEvidenceError("source run/attempt/tested SHA or workflow path is foreign")
    attempt_started = _time(run.get("run_started_at"), "current attempt start")
    jobs = _all_pages(client, f"actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    expected_names = [PLAN_JOB_NAME, *(f"Complete source tests ({i}/{len(plan.shards)})" for i in range(1, len(plan.shards) + 1))]
    relevant = [job for job in jobs if job.get("name") == PLAN_JOB_NAME or str(job.get("name", "")).startswith("Complete source tests (")]
    if Counter(str(job.get("name")) for job in relevant) != Counter(expected_names):
        raise SourceEvidenceError("planned source jobs are omitted, duplicated, stale or unexpected")
    job_by_name = {cast(str, job["name"]): job for job in relevant}
    for name, job in job_by_name.items():
        if any(job.get(key) != value for key, value in {"run_id": run_id, "run_attempt": attempt, "status": "completed", "conclusion": "success"}.items()):
            raise SourceEvidenceError(f"native source job is not successful in current attempt: {name}")
        if _time(job.get("started_at"), "job start") < attempt_started - timedelta(seconds=5):
            raise SourceEvidenceError(f"old-produced job aliased as current attempt: {name}")
        _time(job.get("completed_at"), "job completion")
        if name != PLAN_JOB_NAME:
            steps = job.get("steps")
            if not isinstance(steps, list):
                raise SourceEvidenceError(f"mandatory native steps unavailable: {name}")
            names = ("Prepare Python-only management test fixture", "Install Chromium", "Verify real headless Chromium", "Run complete required source selection")
            for step_name in names:
                matches = [step for step in steps if isinstance(step, dict) and step.get("name") == step_name]
                if len(matches) != 1 or matches[0].get("conclusion") != "success":
                    raise SourceEvidenceError(f"mandatory native step did not succeed: {name}/{step_name}")
    artifacts = _all_pages(client, f"actions/runs/{run_id}/artifacts", "artifacts")
    expected_artifacts = [_artifact_name(run_id, attempt), *(_artifact_name(run_id, attempt, shard) for shard in range(1, len(plan.shards) + 1))]
    for name in expected_artifacts:
        found = [artifact for artifact in artifacts if artifact.get("name") == name]
        if len(found) != 1 or found[0].get("expired") is not False or not isinstance(found[0].get("id"), int):
            raise SourceEvidenceError(f"current-attempt source artifact missing/duplicate/expired: {name}")
        creator = _time(found[0].get("created_at"), "artifact creation")
        workflow_run = _object(found[0].get("workflow_run"), "artifact.workflow_run")
        if workflow_run.get("id") != run_id or creator < attempt_started - timedelta(seconds=5):
            raise SourceEvidenceError(f"artifact was produced by another run/attempt: {name}")
    for number in range(1, len(plan.shards) + 1):
        name = _artifact_name(run_id, attempt, number)
        path = artifacts_root / name / "receipt.json"
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
            if raw != _canonical(payload):
                raise SourceEvidenceError("receipt is not canonical immutable JSON")
            parsed = _object(payload, "receipt")
        except (OSError, UnicodeError, ValueError) as error:
            raise SourceEvidenceError(f"unreadable receipt for shard {number}") from error
        validate_receipt(parsed, plan, number, run_id, attempt, job=job_by_name[f"Complete source tests ({number}/{len(plan.shards)})"])
    return plan
