from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.tested_source_refs import (
    REQUIRED_JOBS,
    SCHEMA_VERSION,
    TEST_PROFILE,
    WORKFLOW_PATH,
)
from auto_coder.tested_source_refs import TestedSourceError as SourceEvidenceError
from auto_coder.tested_source_refs import TestedSourcePublisher as SourcePublisher
from auto_coder.tested_source_refs import TestedSourceRecord as SourceRecord
from auto_coder.tested_source_refs import parse_record, record_from_checkout, serialize_record
from auto_coder.util.gh_cache import GitDataResponse

SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG_OID = "c" * 40


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
        self.missing_job: str | None = None
        self.tag_status = 201

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        if method == "GET" and path == "actions/runs/17":
            return GitDataResponse(
                200,
                {
                    "id": 17,
                    "run_attempt": 2,
                    "workflow_id": 23,
                    "head_sha": SHA,
                    "path": WORKFLOW_PATH,
                    "status": self.run_status,
                    "conclusion": self.run_conclusion,
                },
            )
        if method == "GET" and path.endswith("/attempts/2/jobs?per_page=100"):
            jobs = [{"name": name, "status": "completed", "conclusion": "success", "run_attempt": 2} for name in REQUIRED_JOBS if name != self.missing_job]
            return GitDataResponse(200, {"total_count": len(jobs), "jobs": jobs})
        if method == "GET" and path.startswith("git/ref/tags/"):
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
            self.ref = {"ref": payload["ref"], "object": {"type": "tag", "sha": payload["sha"]}}
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


def test_lost_ref_create_response_is_recovered_only_by_exact_readback() -> None:
    remote = FakeGitData()
    remote.ref_status = 503
    assert publisher(remote).publish(record(), "2026-01-02T03:04:05Z").tag_object_oid == TAG_OID
    assert remote.creates == 1


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


def test_checkout_record_uses_real_clean_git_identity_and_locked_input(tmp_path: Path, _use_real_commands) -> None:
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.email", "test@example.invalid"], check=True)
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", tmp_path, "add", "uv.lock"], check=True)
    subprocess.run(["git", "-C", tmp_path, "commit", "-qm", "fixture"], check=True)
    sha = subprocess.run(["git", "-C", tmp_path, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    observed = record_from_checkout("owner/repo", sha, 17, 2, 23, tmp_path)
    assert observed.source_sha == sha
    assert len(observed.lock_sha256) == 64

    (tmp_path / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    with pytest.raises(SourceEvidenceError, match="clean exact source"):
        record_from_checkout("owner/repo", sha, 17, 2, 23, tmp_path)
