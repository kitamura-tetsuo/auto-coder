from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

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
from auto_coder.tested_source_refs import parse_record, record_from_checkout, serialize_record
from auto_coder.util.gh_cache import GitDataResponse

SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG_OID = "c" * 40


def assert_required_workflow_profile(workflow: object) -> None:
    assert isinstance(workflow, dict)
    jobs = workflow["jobs"]
    quality = jobs["source-quality"]
    complete = jobs["complete-source-tests"]
    quality_runs = [step["run"] for step in quality["steps"] if isinstance(step, dict) and isinstance(step.get("run"), str)]
    assert quality_runs == [
        "uv sync --frozen --dev --extra test",
        'uv run python -c \'from auto_coder.build_identity import resolve_build_identity; r=resolve_build_identity("."); assert r.state == "clean" and r.source_revision == "${{ github.sha }}"\'\n',
        "uv run black --check src/ tests/\nuv run isort --check-only src/ tests/\nuv run flake8 src/ tests/\nuv run mypy --config-file pyproject.toml -p auto_coder\n",
    ]
    complete_steps = {step.get("name"): step for step in complete["steps"] if isinstance(step, dict) and step.get("name")}
    assert complete_steps["Install Chromium"] == {"name": "Install Chromium", "run": "uv run playwright install --with-deps chromium"}
    assert complete_steps["Verify real headless Chromium"] == {"name": "Verify real headless Chromium", "run": "uv run python scripts/browser_preflight.py"}
    assert complete_steps["Run complete required source selection"] == {"name": "Run complete required source selection", "run": "uv run pytest -m 'not opencode_live' -vv"}
    assert complete["env"]["AUTO_CODER_REQUIRE_BROWSER"] == "1"


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
    assert "uv run pytest -m 'not opencode_live'" in source
    assert 'AUTO_CODER_REQUIRE_BROWSER: "1"' in source

    narrowed = deepcopy(workflow)
    narrowed["jobs"]["complete-source-tests"]["steps"][-1]["run"] += " tests/test_tested_source_refs.py"
    narrowed_remote = FakeGitData()
    with pytest.raises(AssertionError):
        publish_workflow_result(narrowed, narrowed_remote, record(), {name: ("completed", "success") for name in REQUIRED_JOBS})
    assert narrowed_remote.creates == 0

    skipped = deepcopy(workflow)
    skipped["jobs"]["complete-source-tests"]["steps"][-1]["if"] = "false"
    skipped_remote = FakeGitData()
    with pytest.raises(AssertionError):
        publish_workflow_result(skipped, skipped_remote, record(), {name: ("completed", "success") for name in REQUIRED_JOBS})
    assert skipped_remote.creates == 0


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
