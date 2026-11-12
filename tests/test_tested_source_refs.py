from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from scripts import check_source_test_shards

from auto_coder.ci_shard_plan import ShardContext, _canonical, _digest, allocate, build_plan, read_plan, write_plan
from auto_coder.source_ci_evidence import PLAN_JOB_NAME, RECEIPT_SCHEMA, SOURCE_JOB, WORKFLOW_NAME, _artifact_name
from auto_coder.source_ci_metrics import build_report, legacy_four_groups
from auto_coder.tested_source_refs import (
    REQUIRED_JOBS,
    SCHEMA_VERSION,
    TEST_PROFILE,
    WORKFLOW_PATH,
)
from auto_coder.tested_source_refs import TestedSourceError as SourceEvidenceError
from auto_coder.tested_source_refs import TestedSourcePublisher as SourcePublisher
from auto_coder.tested_source_refs import TestedSourceRecord as SourceRecord
from auto_coder.tested_source_refs import main as publication_main
from auto_coder.tested_source_refs import parse_record, record_from_checkout, serialize_record, verify_source_shard_attempt
from auto_coder.util.gh_cache import GitDataResponse
from tests.support.ci_browser_install import assert_conditional_chromium_install

SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG_OID = "c" * 40


def test_pull_request_full_source_selection_checks_out_head_without_publishing() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / WORKFLOW_PATH).read_text())
    assert "pull_request" in workflow["on"]
    steps = workflow["jobs"]["complete-source-shard"]["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    command = next(step for step in steps if step.get("name") == "Run complete required source selection")["run"]
    assert command.startswith("uv run python -m pytest -p scripts.ci_shard_plugin -p scripts.source_ci_receipt_plugin")
    assert "-m 'not opencode_live'" in command
    assert "--ci-plan=" in command and "--source-ci-receipt=" in command
    assert "--splits" not in command
    publisher = yaml.safe_load((root / ".github/workflows/publish-tested-source.yml").read_text())
    assert publisher["jobs"]["publish-tested-source"]["if"] == "github.event.workflow_run.event == 'push' && github.event.workflow_run.head_branch == 'main'"


def assert_required_workflow_profile(workflow: object) -> None:
    assert isinstance(workflow, dict)
    jobs = workflow["jobs"]
    quality = jobs["source-quality"]
    shard = jobs["complete-source-shard"]
    complete = jobs["complete-source-tests"]
    quality_runs = [step["run"] for step in quality["steps"] if isinstance(step, dict) and isinstance(step.get("run"), str)]
    assert quality_runs == [
        "uv sync --locked --dev --extra test",
        'uv run python -c \'from auto_coder.build_identity import resolve_build_identity; r=resolve_build_identity("."); assert r.state == "clean" and r.source_revision == "${{ github.sha }}"\'\n',
        "uv run black --check src/ tests/\nuv run isort --check-only src/ tests/\nuv run flake8 src/ tests/\nuv run mypy --config-file pyproject.toml -p auto_coder\n",
    ]
    plan = jobs["source-plan"]
    assert plan["name"] == PLAN_JOB_NAME
    assert "scripts/plan_ci_shards.py" in next(step["run"] for step in plan["steps"] if step.get("name") == "Collect complete current source tests and immutable shard plan")
    assert "-m 'not opencode_live'" in next(step["run"] for step in plan["steps"] if step.get("name") == "Collect complete current source tests and immutable shard plan")
    assert shard["needs"] == ["source-plan"]
    assert shard["strategy"] == {"fail-fast": False, "max-parallel": 12, "matrix": "${{ fromJSON(needs.source-plan.outputs.matrix) }}"}
    assert "runner.temp" not in str(shard.get("env", {}))
    complete_steps = {step.get("name"): step for step in shard["steps"] if isinstance(step, dict) and step.get("name")}
    assert_conditional_chromium_install(complete_steps["Install Chromium"])
    assert complete_steps["Verify real headless Chromium"] == {"name": "Verify real headless Chromium", "run": "uv run python scripts/browser_preflight.py"}
    run = complete_steps["Run complete required source selection"]["run"]
    assert "--ci-plan=" in run and "--ci-shard=${{ matrix.shard }}" in run
    assert "--source-ci-receipt=" in run and "-m 'not opencode_live'" in run
    assert complete_steps["Run complete required source selection"]["env"]["AUTO_CODER_CI_TIMING_DIR"] == "${{ runner.temp }}/source-shard/timing"
    assert "--splitting-algorithm" not in run
    assert run.endswith('--junitxml "$RUNNER_TEMP/source-shard/results.xml"')
    assert "if" not in complete_steps["Run complete required source selection"]
    assert "continue-on-error" not in complete_steps["Run complete required source selection"]
    fixture = complete_steps["Prepare Python-only management test fixture"]
    assert "if" not in fixture
    assert "continue-on-error" not in fixture
    assert shard["env"]["AUTO_CODER_REQUIRE_BROWSER"] == "1"
    assert complete["name"] == "Complete source tests with Chromium"
    assert complete["if"] == "always()"
    assert complete["needs"] == ["source-plan", "complete-source-shard"]
    dependency_rejection = next(step for step in complete["steps"] if step.get("name") == "Reject an unsuccessful matrix dependency")
    assert dependency_rejection == {
        "name": "Reject an unsuccessful matrix dependency",
        "if": "needs.source-plan.result != 'success' || needs.complete-source-shard.result != 'success'",
        "run": "exit 1",
    }
    aggregate = next(step for step in complete["steps"] if step.get("name") == "Verify exact current-attempt shard evidence")
    assert aggregate["run"].startswith("uv run python scripts/check_source_test_shards.py ")
    assert "if" not in aggregate
    assert "continue-on-error" not in aggregate
    assert "|| true" not in aggregate["run"]
    assert "set +e" not in aggregate["run"]
    assert "--plan" in aggregate["run"] and "--artifacts" in aggregate["run"]


def publish_workflow_result(
    workflow: object,
    remote: "FakeGitData",
    source_record: SourceRecord,
    gate_outcomes: dict[str, tuple[str, object]],
    *,
    unrelated_oci_conclusion: str = "failure",
):
    """Model the Actions boundary: workflow gates produce the observed run/jobs."""

    assert_required_workflow_profile(workflow)
    assert set(gate_outcomes) == REQUIRED_JOBS
    remote.job_outcomes = gate_outcomes
    unfinished = any(status != "completed" for status, _conclusion in gate_outcomes.values())
    failed = any(conclusion != "success" for _status, conclusion in gate_outcomes.values())
    remote.run_status = "in_progress" if unfinished else "completed"
    remote.run_conclusion = None if unfinished else ("failure" if failed else "success")
    assert unrelated_oci_conclusion in {"failure", "success"}
    return publisher(remote).publish(source_record, "2026-01-02T03:04:05Z")


def record(**changes: object) -> SourceRecord:
    value = SourceRecord(
        schema_version=SCHEMA_VERSION,
        repository="owner/repo",
        source_sha=SHA,
        distribution_version=f"2026.1.2.3+g{SHA}",
        lock_sha256="d" * 64,
        python_version="3.12",
        run_id=17,
        run_attempt=2,
        workflow_id=23,
        workflow_path=WORKFLOW_PATH,
        test_profile=TEST_PROFILE,
        test_result="success",
    )
    return replace(value, **changes)


class FakeGitData:
    def __init__(self) -> None:
        self.ref: dict[str, object] | None = None
        self.tags: dict[str, dict[str, object]] = {}
        self.creates = 0
        self.ref_status = 201
        self.run_status = "completed"
        self.run_conclusion: object = "success"
        self.current_attempt = 2
        self.source_sha = SHA
        self.unavailable_attempts: set[int] = set()
        self.missing_job: str | None = None
        self.job_outcomes: dict[str, tuple[str, object]] = {name: ("completed", "success") for name in REQUIRED_JOBS}
        self.tag_status = 201
        self.ref_exception_after_create = False
        self.ref_read_exception = False
        self.ref_oid_after_create: str | None = None
        self.requests: list[tuple[str, str]] = []

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        self.requests.append((method, path))
        if method == "GET" and path == "actions/runs/17":
            return GitDataResponse(
                200,
                {
                    "id": 17,
                    "run_attempt": self.current_attempt,
                    "workflow_id": 23,
                    "head_sha": self.source_sha,
                    "path": WORKFLOW_PATH,
                    "status": self.run_status,
                    "conclusion": self.run_conclusion,
                },
            )
        if method == "GET" and path.startswith("actions/runs/17/attempts/") and not path.endswith("/jobs?per_page=100"):
            attempt = int(path.rsplit("/", 1)[1])
            if attempt in self.unavailable_attempts:
                return GitDataResponse(404)
            return GitDataResponse(
                200,
                {
                    "id": 17,
                    "run_attempt": attempt,
                    "workflow_id": 23,
                    "head_sha": self.source_sha,
                    "path": WORKFLOW_PATH,
                    "status": "completed",
                    "conclusion": "success",
                },
            )
        if method == "GET" and "/attempts/" in path and path.endswith("/jobs?per_page=100"):
            attempt = int(path.split("/attempts/", 1)[1].split("/", 1)[0])
            jobs = [{"name": name, "status": self.job_outcomes[name][0], "conclusion": self.job_outcomes[name][1], "run_attempt": attempt} for name in REQUIRED_JOBS if name != self.missing_job]
            return GitDataResponse(200, {"total_count": len(jobs), "jobs": jobs})
        if method == "GET" and path.startswith("git/ref/tags/"):
            if self.ref_read_exception and self.ref is not None:
                raise RuntimeError("GitHub Git-data request unavailable")
            return GitDataResponse(200, self.ref) if self.ref is not None else GitDataResponse(404)
        if method == "GET" and path.startswith("git/tags/"):
            value = self.tags.get(path.removeprefix("git/tags/"))
            return GitDataResponse(200, value) if value is not None else GitDataResponse(404)
        if method == "POST" and path == "git/tags" and payload is not None:
            self.creates += 1
            if self.tag_status != 201:
                return GitDataResponse(self.tag_status)
            self.tags[TAG_OID] = {"tag": payload["tag"], "message": payload["message"], "object": {"type": "commit", "sha": payload["object"]}}
            return GitDataResponse(201, {"sha": TAG_OID})
        if method == "POST" and path == "git/refs" and payload is not None:
            # Model both a normal response and a response lost after GitHub committed it.
            self.ref = {"ref": payload["ref"], "object": {"type": "tag", "sha": self.ref_oid_after_create or payload["sha"]}}
            if self.ref_exception_after_create:
                raise RuntimeError("GitHub Git-data request unavailable")
            return GitDataResponse(self.ref_status)
        raise AssertionError((method, path, payload))


def publisher(remote: FakeGitData) -> SourcePublisher:
    return SourcePublisher(remote, "owner/repo")  # type: ignore[arg-type]


def source_plan(path: Path) -> Path:
    context = ShardContext.parse(
        {
            "schema_version": "ci-shard-context-v1",
            "repository": "owner/repo",
            "profile_id": TEST_PROFILE,
            "checkout_sha": SHA,
            "pytest_args": ["-m", "not opencode_live"],
            "harness_files": [],
            "semantic_environment": [],
            "origin": {
                "repository": "owner/repo",
                "workflow": WORKFLOW_NAME,
                "run_id": "17",
                "run_attempt": "2",
                "planning_attempt": "source-plan-17-2",
                "event_sha": OTHER_SHA,
            },
        }
    )
    profile = {"repository": "owner/repo", "profile_id": TEST_PROFILE, "configuration_digest": "sha256:" + "d" * 64, "python_major_minor": "3.12"}
    nodes = ("tests/test_a.py::test_one", "tests/test_b.py::test_two")
    plan = build_plan(context, profile, nodes, None)
    semantic = plan["semantic"]
    assert isinstance(semantic, dict)
    estimates = semantic["estimates"]
    assert isinstance(estimates, list)
    for entry in estimates:
        entry["estimated_ms"] = 420_000
    shards, capacity, limited = allocate(nodes, estimates)
    semantic.update(shards=shards, effective_capacity_ms=capacity, capacity_limited=limited, predicted_total_ms=840_000)
    plan["digest_sha256"] = _digest(semantic)
    write_plan(path, plan, ())
    return path


def _iso(seconds: int) -> str:
    return (datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class FakeShardEvidence:
    def __init__(self, directory: Path) -> None:
        self.plan_path = source_plan(directory / "plan.json")
        self.artifacts_root = directory / "artifacts"
        self.run_attempt = 2
        self.event_sha = OTHER_SHA
        self.source_sha = SHA
        self.jobs: list[dict[str, object]] = [
            {
                "name": PLAN_JOB_NAME,
                "run_id": 17,
                "run_attempt": 2,
                "id": 100,
                "status": "completed",
                "conclusion": "success",
                "started_at": _iso(20),
                "completed_at": _iso(400),
                "steps": [],
            }
        ]
        for shard in (1, 2):
            self.jobs.append(
                {
                    "name": f"Complete source tests ({shard}/2)",
                    "run_id": 17,
                    "run_attempt": 2,
                    "id": 100 + shard,
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": _iso(410),
                    "completed_at": _iso(1000),
                    "steps": [{"name": step, "conclusion": "success"} for step in ("Prepare Python-only management test fixture", "Install Chromium", "Verify real headless Chromium", "Run complete required source selection")],
                }
            )
        self.total_count: int | None = len(self.jobs)
        self.jobs_status = 200
        self.artifacts_status = 200
        self.artifacts: list[dict[str, object]] = [{"name": _artifact_name(17, 2, shard), "id": 400 + index, "expired": False, "created_at": _iso(800), "workflow_run": {"id": 17}} for index, shard in enumerate((None, 1, 2))]
        self.artifact_count: int | None = len(self.artifacts)
        self.requests: list[str] = []
        for shard in (1, 2):
            directory_path = self.artifacts_root / _artifact_name(17, 2, shard)
            directory_path.mkdir(parents=True)
            node = f"tests/test_{'a' if shard == 1 else 'b'}.py::test_{'one' if shard == 1 else 'two'}"
            plan, _ = read_plan(self.plan_path)
            receipt = {
                "schema_version": RECEIPT_SCHEMA,
                "repository": "owner/repo",
                "source_sha": SHA,
                "profile": plan.profile,
                "plan_digest": plan.digest,
                "inventory_digest": _digest(list(plan.inventory)),
                "shard": shard,
                "shard_count": 2,
                "run_id": 17,
                "run_attempt": 2,
                "planning_attempt": "source-plan-17-2",
                "workflow": WORKFLOW_NAME,
                "job_key": SOURCE_JOB,
                "invocation_id": f"invocation-{shard}",
                "started_at": _iso(500),
                "completed_at": _iso(700),
                "collected": list(plan.inventory),
                "assigned": [node],
                "selected": [node],
                "executed": [node],
                "reports": [{"nodeid": node, "phase": phase, "outcome": "passed", "wasxfail": None, "duration_ms": 100.0} for phase in ("setup", "call", "teardown")],
                "native_exit_status": 0,
                "complete": True,
            }
            (directory_path / "receipt.json").write_bytes(_canonical(receipt))

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        assert method == "GET" and payload is None
        self.requests.append(path)
        if path == "actions/runs/17/attempts/2":
            return GitDataResponse(200, {"id": 17, "run_attempt": self.run_attempt, "head_sha": self.source_sha, "path": WORKFLOW_PATH, "status": "in_progress", "run_started_at": _iso(0)})
        if path == "actions/runs/17/attempts/2/jobs?per_page=100":
            return GitDataResponse(self.jobs_status, {"total_count": self.total_count, "jobs": self.jobs})
        if path.startswith("actions/runs/17/attempts/2/jobs?per_page=100&page="):
            return GitDataResponse(self.jobs_status, {"total_count": self.total_count, "jobs": []})
        if path.startswith("actions/runs/17/artifacts?per_page=100"):
            page = int(path.split("&page=", 1)[1]) if "&page=" in path else 1
            return GitDataResponse(self.artifacts_status, {"total_count": self.artifact_count, "artifacts": self.artifacts[(page - 1) * 100 : page * 100]})
        raise AssertionError(path)


def run_actual_shard_gate(monkeypatch: pytest.MonkeyPatch, evidence: FakeShardEvidence) -> tuple[str, str]:
    """Execute the production aggregate CLI and translate its process outcome."""
    monkeypatch.setenv("GITHUB_TOKEN", "aggregate-read-token")
    monkeypatch.setattr(check_source_test_shards, "GitHubGitDataClient", lambda _token, _repository: evidence)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_source_test_shards.py",
            "--repository",
            "owner/repo",
            "--run-id",
            "17",
            "--run-attempt",
            "2",
            "--source-sha",
            SHA,
            "--plan",
            str(evidence.plan_path),
            "--artifacts",
            str(evidence.artifacts_root),
        ],
    )
    try:
        exit_code = check_source_test_shards.main()
    except SourceEvidenceError:
        return ("completed", "failure")
    assert exit_code == 0
    return ("completed", "success")


def run_production_aggregate_decision(
    monkeypatch: pytest.MonkeyPatch,
    workflow: object,
    dependency_result: str | None,
    evidence: FakeShardEvidence,
) -> tuple[str, str]:
    """Execute the aggregate's production dependency decision before readback."""
    assert_required_workflow_profile(workflow)
    assert isinstance(workflow, dict)
    steps = workflow["jobs"]["complete-source-tests"]["steps"]
    rejection = next(step for step in steps if step.get("name") == "Reject an unsuccessful matrix dependency")
    if dependency_result != "success":
        result = subprocess.run(["bash", "-c", rejection["run"]], check=False)
        assert result.returncode != 0
        return ("completed", "failure")
    return run_actual_shard_gate(monkeypatch, evidence)


def publish_from_actual_shard_gate(
    monkeypatch: pytest.MonkeyPatch,
    workflow: object,
    evidence: FakeShardEvidence,
    publication: FakeGitData,
):
    aggregate_outcome = run_actual_shard_gate(monkeypatch, evidence)
    return publish_workflow_result(
        workflow,
        publication,
        record(),
        {
            "Source identity and quality": ("completed", "success"),
            "Complete source tests with Chromium": aggregate_outcome,
        },
    )


def test_source_shard_attempt_accepts_exact_complete_current_attempt(tmp_path: Path) -> None:
    remote = FakeShardEvidence(tmp_path)
    verify_source_shard_attempt(remote, 17, 2, SHA, plan_path=remote.plan_path, artifacts_root=remote.artifacts_root)  # type: ignore[arg-type]


def test_actions_artifact_pagination_uses_all_pages(tmp_path: Path) -> None:
    remote = FakeShardEvidence(tmp_path)
    remote.artifacts = [{"name": f"older-run-{n}", "id": 1000 + n, "expired": False, "created_at": _iso(-100), "workflow_run": {"id": 16}} for n in range(100)] + remote.artifacts
    remote.artifact_count = len(remote.artifacts)
    verify_source_shard_attempt(remote, 17, 2, SHA, plan_path=remote.plan_path, artifacts_root=remote.artifacts_root)  # type: ignore[arg-type]
    assert "actions/runs/17/artifacts?per_page=100&page=2" in remote.requests


def test_same_inventory_native_four_way_comparison_is_diagnostic_only(tmp_path: Path) -> None:
    remote = FakeShardEvidence(tmp_path)
    plan, _raw = read_plan(remote.plan_path)
    snapshot = tmp_path / "historical-durations.json"
    snapshot.write_text(json.dumps({node: 20.0 for node in plan.inventory}), encoding="utf-8")
    assert [len(group) for group in legacy_four_groups(plan.inventory, snapshot)] == [1, 1, 0, 0]
    report = build_report(remote, plan, remote.artifacts_root, snapshot, 17, 2, verified=True)  # type: ignore[arg-type]
    assert report["qualification"] == "verified"
    assert report["end_to_end_required_check_seconds"] is None
    comparison = report["same_inventory_native_comparison"]
    assert isinstance(comparison, dict)
    assert comparison["current_planned_shards_native_critical_path_seconds"] == 0.3
    assert comparison["former_four_shards_native_critical_path_seconds"] == 0.3


@pytest.mark.parametrize(
    "defect",
    ["missing", "duplicate", "failed", "cancelled", "skipped", "unfinished", "stale", "truncated", "unavailable", "wrong_sha", "old_alias", "missing_receipt", "failed_native", "wrong_receipt_attempt", "missing_artifact", "truncated_artifacts"],
)
def test_source_shard_attempt_rejects_incomplete_or_mixed_evidence(defect: str, tmp_path: Path) -> None:
    remote = FakeShardEvidence(tmp_path)
    if defect == "missing":
        remote.jobs.pop()
        remote.total_count = len(remote.jobs)
    elif defect == "duplicate":
        remote.jobs.append(dict(remote.jobs[0]))
        remote.total_count = len(remote.jobs)
    elif defect == "failed":
        remote.jobs[0]["conclusion"] = "failure"
    elif defect in {"cancelled", "skipped"}:
        remote.jobs[0]["conclusion"] = defect
    elif defect == "unfinished":
        remote.jobs[0]["status"] = "in_progress"
        remote.jobs[0]["conclusion"] = None
    elif defect == "stale":
        remote.jobs[0]["run_attempt"] = 1
    elif defect == "truncated":
        remote.total_count = len(remote.jobs) + 1
    elif defect == "unavailable":
        remote.jobs_status = 503
    elif defect == "old_alias":
        remote.jobs[1]["started_at"] = _iso(-3600)
    elif defect == "missing_artifact":
        remote.artifacts.pop()
        remote.artifact_count = len(remote.artifacts)
    elif defect == "truncated_artifacts":
        remote.artifact_count = len(remote.artifacts) + 1
    elif defect in {"missing_receipt", "failed_native", "wrong_receipt_attempt"}:
        path = remote.artifacts_root / _artifact_name(17, 2, 1) / "receipt.json"
        if defect == "missing_receipt":
            path.unlink()
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if defect == "failed_native":
                payload["native_exit_status"] = 1
            else:
                payload["run_attempt"] = 1
            path.write_bytes(_canonical(payload))
    else:
        remote.source_sha = OTHER_SHA
    with pytest.raises(SourceEvidenceError):
        verify_source_shard_attempt(remote, 17, 2, SHA, plan_path=remote.plan_path, artifacts_root=remote.artifacts_root)  # type: ignore[arg-type]


def test_actual_aggregate_success_is_the_publication_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workflow = yaml.safe_load(Path(WORKFLOW_PATH).read_text(encoding="utf-8"))
    publication = FakeGitData()

    result = publish_from_actual_shard_gate(monkeypatch, workflow, FakeShardEvidence(tmp_path), publication)

    assert result.direct_commit_sha == SHA
    assert publication.creates == 1


@pytest.mark.parametrize(
    ("dependency_result", "expected_outcome", "reads_evidence"),
    [
        ("success", ("completed", "success"), True),
        ("failure", ("completed", "failure"), False),
        ("cancelled", ("completed", "failure"), False),
        ("skipped", ("completed", "failure"), False),
        (None, ("completed", "failure"), False),
    ],
)
def test_production_aggregate_dependency_decision_fails_before_readback(
    dependency_result: str | None,
    expected_outcome: tuple[str, str],
    reads_evidence: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow = yaml.safe_load(Path(WORKFLOW_PATH).read_text(encoding="utf-8"))
    evidence = FakeShardEvidence(tmp_path)

    outcome = run_production_aggregate_decision(monkeypatch, workflow, dependency_result, evidence)

    assert outcome == expected_outcome
    assert bool(evidence.requests) is reads_evidence


@pytest.mark.parametrize("defect", ["missing", "duplicate", "failed", "cancelled", "skipped", "unfinished", "stale", "truncated", "unavailable"])
def test_actual_aggregate_failure_cannot_authorize_publication(defect: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workflow = yaml.safe_load(Path(WORKFLOW_PATH).read_text(encoding="utf-8"))
    evidence = FakeShardEvidence(tmp_path)
    if defect == "missing":
        evidence.jobs.pop()
        evidence.total_count = len(evidence.jobs)
    elif defect == "duplicate":
        evidence.jobs.append(dict(evidence.jobs[0]))
        evidence.total_count = len(evidence.jobs)
    elif defect in {"failed", "cancelled", "skipped"}:
        evidence.jobs[0]["conclusion"] = defect
    elif defect == "unfinished":
        evidence.jobs[0].update(status="in_progress", conclusion=None)
    elif defect == "stale":
        evidence.jobs[0]["run_attempt"] = 1
    elif defect == "truncated":
        evidence.total_count = len(evidence.jobs) + 1
    else:
        evidence.jobs_status = 503
    publication = FakeGitData()

    with pytest.raises(SourceEvidenceError, match="completed successful"):
        publish_from_actual_shard_gate(monkeypatch, workflow, evidence, publication)

    assert publication.creates == 0


def test_canonical_annotation_round_trip_and_rejects_extra_fields() -> None:
    payload = serialize_record(record())
    assert parse_record(payload) == record()
    with pytest.raises(SourceEvidenceError, match="missing or unexpected"):
        parse_record(payload[:-1] + ',"secret":"value"}')


def test_exact_tag_is_created_once_and_same_sha_rerun_reuses_it() -> None:
    remote = FakeGitData()
    first = publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    second = publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    assert first == second
    assert first.direct_commit_sha == SHA
    assert first.tag_object_oid == TAG_OID
    assert remote.creates == 1


def test_successful_later_attempt_reuses_original_attempt_tag_without_writes() -> None:
    remote = FakeGitData()
    original = publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    remote.current_attempt = 3

    reused = publisher(remote).publish(record(run_attempt=3), "2026-01-02T04:04:05Z")

    assert reused == original
    assert remote.creates == 1
    assert ("GET", "actions/runs/17/attempts/2") in remote.requests


def test_unavailable_original_attempt_refuses_same_sha_rerun_without_writes() -> None:
    remote = FakeGitData()
    publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    remote.current_attempt = 3
    remote.unavailable_attempts.add(2)

    with pytest.raises(SourceEvidenceError, match="workflow run readback is unavailable"):
        publisher(remote).publish(record(run_attempt=3), "2026-01-02T04:04:05Z")
    assert remote.creates == 1


def test_failed_current_rerun_is_refused_before_reusing_original_tag() -> None:
    remote = FakeGitData()
    publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    remote.current_attempt = 3
    remote.run_conclusion = "failure"

    with pytest.raises(SourceEvidenceError, match="completed successful"):
        publisher(remote).publish(record(run_attempt=3), "2026-01-02T04:04:05Z")
    assert remote.creates == 1


def test_lost_ref_create_response_is_recovered_only_by_exact_readback() -> None:
    remote = FakeGitData()
    remote.ref_status = 503
    assert publisher(remote).publish(record(), "2026-01-02T03:04:05Z").tag_object_oid == TAG_OID
    assert remote.creates == 1


def test_transport_exception_after_ref_create_is_reconciled_without_retry() -> None:
    remote = FakeGitData()
    remote.ref_exception_after_create = True

    assert publisher(remote).publish(record(), "2026-01-02T03:04:05Z").tag_object_oid == TAG_OID
    assert remote.creates == 1
    assert remote.requests.count(("POST", "git/refs")) == 1
    assert remote.requests[-1] == ("GET", "actions/runs/17/attempts/2/jobs?per_page=100")


def test_transport_exception_with_unavailable_readback_stays_uncertain() -> None:
    remote = FakeGitData()
    remote.ref_exception_after_create = True
    remote.ref_read_exception = True

    with pytest.raises(SourceEvidenceError, match="outcome is uncertain"):
        publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    assert remote.creates == 1
    assert remote.requests.count(("POST", "git/refs")) == 1


def test_transport_exception_readback_of_different_object_is_conflict() -> None:
    remote = FakeGitData()
    remote.ref_exception_after_create = True
    remote.ref_oid_after_create = OTHER_SHA
    remote.tags[OTHER_SHA] = {
        "tag": f"auto-coder-tested-{SHA}",
        "message": serialize_record(record(distribution_version="9.9.9")),
        "object": {"type": "commit", "sha": SHA},
    }

    with pytest.raises(SourceEvidenceError, match="object just created"):
        publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    assert remote.creates == 1
    assert remote.requests.count(("POST", "git/refs")) == 1


def test_lost_tag_object_create_response_stays_uncertain_without_blind_retry() -> None:
    remote = FakeGitData()
    remote.tag_status = 503
    with pytest.raises(SourceEvidenceError, match="outcome is uncertain"):
        publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    assert remote.creates == 1
    assert remote.ref is None


def test_conflicting_existing_tag_is_never_rewritten() -> None:
    remote = FakeGitData()
    remote.ref = {"ref": f"refs/tags/auto-coder-tested-{SHA}", "object": {"type": "tag", "sha": TAG_OID}}
    remote.tags[TAG_OID] = {"tag": f"auto-coder-tested-{SHA}", "message": serialize_record(record(source_sha=OTHER_SHA)), "object": {"type": "commit", "sha": OTHER_SHA}}
    with pytest.raises(SourceEvidenceError, match="conflicts"):
        publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    assert remote.creates == 0


@pytest.mark.parametrize(("status", "conclusion"), [("in_progress", None), ("completed", "failure"), ("completed", "cancelled")])
def test_failed_or_unfinished_workflow_refuses_publication(status: str, conclusion: object) -> None:
    remote = FakeGitData()
    remote.run_status = status
    remote.run_conclusion = conclusion
    with pytest.raises(SourceEvidenceError, match="completed successful"):
        publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    assert remote.creates == 0


def test_missing_required_job_refuses_publication() -> None:
    remote = FakeGitData()
    remote.missing_job = next(iter(REQUIRED_JOBS))
    with pytest.raises(SourceEvidenceError, match="job evidence is missing"):
        publisher(remote).publish(record(), "2026-01-02T03:04:05Z")
    assert remote.creates == 0


def test_workflows_keep_source_authority_independent_from_legacy_image_publish() -> None:
    source = open(".github/workflows/test-source-for-runtime.yml", encoding="utf-8").read()
    workflow = yaml.safe_load(source)
    assert_required_workflow_profile(workflow)
    publication = open(".github/workflows/publish-tested-source.yml", encoding="utf-8").read()
    legacy = open(".github/workflows/publish-beta.yml", encoding="utf-8").read()
    assert "docker" not in source.lower()
    assert "packages: write" not in source
    assert "contents: write" in publication
    assert "workflow_run:" in publication
    assert "docker/build-push-action" in legacy
    assert "docker push" in legacy
    assert "uv run black --check src/ tests/" in source
    assert "uv run mypy --config-file pyproject.toml -p auto_coder" in source
    assert "-m 'not opencode_live' -vv" in source
    assert 'AUTO_CODER_REQUIRE_BROWSER: "1"' in source

    narrowed = deepcopy(workflow)
    narrowed_selection = next(step for step in narrowed["jobs"]["complete-source-shard"]["steps"] if step.get("name") == "Run complete required source selection")
    narrowed_selection["run"] += " tests/test_tested_source_refs.py"
    narrowed_remote = FakeGitData()
    with pytest.raises(AssertionError):
        publish_workflow_result(narrowed, narrowed_remote, record(), {name: ("completed", "success") for name in REQUIRED_JOBS})
    assert narrowed_remote.creates == 0

    skipped = deepcopy(workflow)
    skipped_selection = next(step for step in skipped["jobs"]["complete-source-shard"]["steps"] if step.get("name") == "Run complete required source selection")
    skipped_selection["if"] = "false"
    skipped_remote = FakeGitData()
    with pytest.raises(AssertionError):
        publish_workflow_result(skipped, skipped_remote, record(), {name: ("completed", "success") for name in REQUIRED_JOBS})
    assert skipped_remote.creates == 0

    masked_shard = deepcopy(workflow)
    masked_selection = next(step for step in masked_shard["jobs"]["complete-source-shard"]["steps"] if step.get("name") == "Run complete required source selection")
    masked_selection["continue-on-error"] = True
    masked_shard_remote = FakeGitData()
    with pytest.raises(AssertionError):
        publish_workflow_result(masked_shard, masked_shard_remote, record(), {name: ("completed", "success") for name in REQUIRED_JOBS})
    assert masked_shard_remote.creates == 0

    masked_fixture = deepcopy(workflow)
    fixture = next(step for step in masked_fixture["jobs"]["complete-source-shard"]["steps"] if step.get("name") == "Prepare Python-only management test fixture")
    fixture["continue-on-error"] = True
    masked_fixture_remote = FakeGitData()
    with pytest.raises(AssertionError):
        publish_workflow_result(masked_fixture, masked_fixture_remote, record(), {name: ("completed", "success") for name in REQUIRED_JOBS})
    assert masked_fixture_remote.creates == 0

    disabled_validator = deepcopy(workflow)
    validator = next(step for step in disabled_validator["jobs"]["complete-source-tests"]["steps"] if step.get("name") == "Verify exact current-attempt shard evidence")
    validator["if"] = "false"
    disabled_validator_remote = FakeGitData()
    with pytest.raises(AssertionError):
        publish_workflow_result(disabled_validator, disabled_validator_remote, record(), {name: ("completed", "success") for name in REQUIRED_JOBS})
    assert disabled_validator_remote.creates == 0


def test_checkout_record_uses_real_clean_git_identity_and_locked_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
    workflow = yaml.safe_load(Path(".github/workflows/test-source-for-runtime.yml").read_text(encoding="utf-8"))
    assert_required_workflow_profile(workflow)
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", tmp_path, "remote", "add", "origin", "https://github.com/owner/repo.git"], check=True)
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", tmp_path, "add", "uv.lock"], check=True)
    subprocess.run(["git", "-C", tmp_path, "commit", "-qm", "fixture"], check=True)
    sha = subprocess.run(["git", "-C", tmp_path, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    observed = record_from_checkout("owner/repo", sha, 17, 2, 23, tmp_path)
    assert observed.source_sha == sha
    assert len(observed.lock_sha256) == 64

    with pytest.raises(SourceEvidenceError, match="configured publication repository"):
        record_from_checkout("other/repo", sha, 17, 2, 23, tmp_path)

    clients_created = 0

    def forbidden_client(*_args: object, **_kwargs: object) -> None:
        nonlocal clients_created
        clients_created += 1

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "not-logged")
    monkeypatch.setenv("AUTO_CODER_PUBLICATION_REPOSITORY", "other/repo")
    monkeypatch.setattr("auto_coder.tested_source_refs.GitHubGitDataClient", forbidden_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tested_source_refs",
            "--repository",
            "other/repo",
            "--source-sha",
            sha,
            "--run-id",
            "17",
            "--run-attempt",
            "2",
            "--workflow-id",
            "23",
            "--tagger-date",
            "2026-01-02T03:04:05Z",
        ],
    )
    with pytest.raises(SourceEvidenceError, match="configured publication repository"):
        publication_main()
    assert clients_created == 0
    monkeypatch.chdir(Path(__file__).parent.parent)

    publication_remote = FakeGitData()
    publication_remote.source_sha = sha
    successful_gates = {name: ("completed", "success") for name in REQUIRED_JOBS}
    published = publish_workflow_result(workflow, publication_remote, observed, successful_gates, unrelated_oci_conclusion="failure")
    assert published.direct_commit_sha == sha
    assert publication_remote.creates == 1

    for failing_gates in (
        {**successful_gates, "Source identity and quality": ("completed", "failure")},
        {**successful_gates, "Complete source tests with Chromium": ("in_progress", None)},
    ):
        failed_remote = FakeGitData()
        failed_remote.source_sha = sha
        with pytest.raises(SourceEvidenceError, match="completed successful"):
            publish_workflow_result(workflow, failed_remote, observed, failing_gates)
        assert failed_remote.creates == 0

    def remote_with_existing(existing_record: SourceRecord) -> FakeGitData:
        remote = FakeGitData()
        remote.source_sha = sha
        remote.ref = {"ref": f"refs/tags/auto-coder-tested-{sha}", "object": {"type": "tag", "sha": TAG_OID}}
        remote.tags[TAG_OID] = {
            "tag": f"auto-coder-tested-{sha}",
            "message": serialize_record(existing_record),
            "object": {"type": "commit", "sha": sha},
        }
        return remote

    for conflicting in (
        replace(observed, distribution_version="9.9.9"),
        replace(observed, lock_sha256="e" * 64),
    ):
        remote = remote_with_existing(conflicting)
        with pytest.raises(SourceEvidenceError, match="source/build identity"):
            publisher(remote).publish(observed, "2026-01-02T03:04:05Z")
        assert remote.creates == 0

    rerun_remote = remote_with_existing(observed)
    rerun_remote.current_attempt = 3
    reused = publisher(rerun_remote).publish(replace(observed, run_attempt=3), "2026-01-02T04:04:05Z")
    assert reused.record == observed
    assert rerun_remote.creates == 0

    (tmp_path / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    with pytest.raises(SourceEvidenceError, match="clean exact source"):
        record_from_checkout("owner/repo", sha, 17, 2, 23, tmp_path)
