"""Immutable, fail-closed evidence that a Git commit passed source CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .build_identity import resolve_build_identity
from .logger_config import get_logger
from .util.gh_cache import GitDataResponse, GitHubGitDataClient

logger = get_logger(__name__)

SCHEMA_VERSION = 1
TEST_PROFILE = "source-required-v1"
WORKFLOW_PATH = ".github/workflows/test-source-for-runtime.yml"
REQUIRED_JOBS = frozenset({"Source identity and quality", "Complete source tests with Chromium"})
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+!-]{0,199}\Z")


class TestedSourceError(RuntimeError):
    """The tested-source evidence is conflicting, unavailable, or invalid."""


@dataclass(frozen=True)
class TestedSourceRecord:
    schema_version: int
    repository: str
    source_sha: str
    distribution_version: str
    lock_sha256: str
    python_version: str
    run_id: int
    run_attempt: int
    workflow_id: int
    workflow_path: str
    test_profile: str
    test_result: Literal["success"]


@dataclass(frozen=True)
class TestedSourceReadback:
    tag_ref: str
    tag_object_oid: str
    direct_commit_sha: str
    record: TestedSourceRecord


def tag_name(source_sha: str) -> str:
    _require_sha(source_sha, "source_sha")
    return f"auto-coder-tested-{source_sha}"


def _require_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise TestedSourceError(f"{field} must be a full lowercase SHA-1")
    return value


def validate_record(record: TestedSourceRecord) -> TestedSourceRecord:
    if type(record.schema_version) is not int or record.schema_version != SCHEMA_VERSION:
        raise TestedSourceError("schema_version must equal 1")
    if not isinstance(record.repository, str) or _REPOSITORY.fullmatch(record.repository) is None or len(record.repository) > 200:
        raise TestedSourceError("repository is invalid")
    _require_sha(record.source_sha, "source_sha")
    if not isinstance(record.lock_sha256, str) or _SHA256.fullmatch(record.lock_sha256) is None:
        raise TestedSourceError("lock_sha256 must be a lowercase SHA-256 digest")
    if not isinstance(record.distribution_version, str) or _VERSION.fullmatch(record.distribution_version) is None:
        raise TestedSourceError("distribution_version is invalid")
    if record.python_version != "3.12":
        raise TestedSourceError("python_version must be 3.12")
    for field, value in (("run_id", record.run_id), ("run_attempt", record.run_attempt), ("workflow_id", record.workflow_id)):
        if type(value) is not int or value <= 0:
            raise TestedSourceError(f"{field} must be a positive integer")
    if record.workflow_path != WORKFLOW_PATH:
        raise TestedSourceError("workflow_path is not the source-test workflow")
    if record.test_profile != TEST_PROFILE or record.test_result != "success":
        raise TestedSourceError("record does not describe the successful source-required-v1 profile")
    return record


def serialize_record(record: TestedSourceRecord) -> str:
    validate_record(record)
    return json.dumps(asdict(record), sort_keys=True, separators=(",", ":"))


def parse_record(payload: object) -> TestedSourceRecord:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > 2048:
        raise TestedSourceError("tag annotation is missing or too large")
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TestedSourceError("tag annotation is not canonical JSON") from exc
    expected = set(TestedSourceRecord.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != expected:
        raise TestedSourceError("tag annotation has missing or unexpected fields")
    try:
        record = TestedSourceRecord(**data)
    except TypeError as exc:
        raise TestedSourceError("tag annotation fields are invalid") from exc
    validate_record(record)
    if serialize_record(record) != payload:
        raise TestedSourceError("tag annotation is not canonical")
    return record


def _object(response: GitDataResponse, operation: str) -> dict[str, object]:
    if response.status != 200 or not isinstance(response.data, dict):
        raise TestedSourceError(f"GitHub {operation} is unavailable")
    return response.data


class TestedSourcePublisher:
    """Validate Actions evidence and create/read one immutable annotated ref."""

    def __init__(self, client: GitHubGitDataClient, repository: str) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise TestedSourceError("repository is invalid")
        self.client = client
        self.repository = repository

    def verify_run(self, record: TestedSourceRecord) -> None:
        validate_record(record)
        if record.repository != self.repository:
            raise TestedSourceError("run repository does not match publication repository")
        run = _object(self.client.request("GET", f"actions/runs/{record.run_id}"), "workflow run readback")
        expected = {
            "id": record.run_id,
            "run_attempt": record.run_attempt,
            "workflow_id": record.workflow_id,
            "head_sha": record.source_sha,
            "path": record.workflow_path,
            "status": "completed",
            "conclusion": "success",
        }
        if any(run.get(key) != value for key, value in expected.items()):
            raise TestedSourceError("workflow run is not the exact completed successful source run")

        response = self.client.request("GET", f"actions/runs/{record.run_id}/attempts/{record.run_attempt}/jobs?per_page=100")
        jobs_data = _object(response, "workflow jobs readback")
        jobs = jobs_data.get("jobs")
        if not isinstance(jobs, list) or jobs_data.get("total_count") != len(jobs):
            raise TestedSourceError("workflow job evidence is missing or incomplete")
        observed: set[str] = set()
        for item in jobs:
            if not isinstance(item, dict):
                raise TestedSourceError("workflow job evidence is malformed")
            name = item.get("name")
            if name in REQUIRED_JOBS:
                if item.get("status") != "completed" or item.get("conclusion") != "success" or item.get("run_attempt") != record.run_attempt:
                    raise TestedSourceError(f"required workflow job did not succeed: {name}")
                observed.add(str(name))
        if observed != REQUIRED_JOBS:
            raise TestedSourceError("required workflow job evidence is missing")

    def read(self, source_sha: str) -> TestedSourceReadback | None:
        name = tag_name(source_sha)
        response = self.client.request("GET", f"git/ref/tags/{name}")
        if response.status == 404:
            return None
        ref = _object(response, "tag ref readback")
        target = ref.get("object")
        if ref.get("ref") != f"refs/tags/{name}" or not isinstance(target, dict) or target.get("type") != "tag":
            raise TestedSourceError("tested-source ref is not an annotated tag")
        oid = _require_sha(target.get("sha"), "tag object oid")
        tag = _object(self.client.request("GET", f"git/tags/{oid}"), "tag object readback")
        direct = tag.get("object")
        if tag.get("tag") != name or not isinstance(direct, dict) or direct.get("type") != "commit":
            raise TestedSourceError("tested-source tag object is malformed")
        commit = _require_sha(direct.get("sha"), "direct commit sha")
        record = parse_record(tag.get("message"))
        if commit != source_sha or record.source_sha != source_sha or record.repository != self.repository:
            raise TestedSourceError("tested-source tag conflicts with the requested source")
        return TestedSourceReadback(f"refs/tags/{name}", oid, commit, record)

    def publish(self, record: TestedSourceRecord, tagger_date: str) -> TestedSourceReadback:
        self.verify_run(record)
        existing = self.read(record.source_sha)
        if existing is not None:
            self.verify_run(existing.record)
            return existing
        name = tag_name(record.source_sha)
        tag_response = self.client.request(
            "POST",
            "git/tags",
            {
                "tag": name,
                "message": serialize_record(record),
                "object": record.source_sha,
                "type": "commit",
                "tagger": {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com", "date": tagger_date},
            },
        )
        if tag_response.status != 201 or not isinstance(tag_response.data, dict):
            raise TestedSourceError("tag object creation is unavailable; publication outcome is uncertain")
        oid = _require_sha(tag_response.data.get("sha"), "created tag object oid")
        ref_response = self.client.request("POST", "git/refs", {"ref": f"refs/tags/{name}", "sha": oid})
        if ref_response.status not in (201, 422):
            # A lost create response is resolved only by strict readback below.
            logger.warning("Tested-source ref create response was unavailable; performing exact readback")
        observed = self.read(record.source_sha)
        if observed is None:
            raise TestedSourceError("tested-source ref is not observable; publication outcome is uncertain")
        if observed.tag_object_oid != oid or observed.record != record:
            raise TestedSourceError("tested-source ref conflicts with the object just created")
        self.verify_run(observed.record)
        return observed


def record_from_checkout(repository: str, source_sha: str, run_id: int, run_attempt: int, workflow_id: int, checkout: Path = Path(".")) -> TestedSourceRecord:
    identity = resolve_build_identity(checkout)
    if identity.state != "clean" or identity.source_revision != source_sha:
        raise TestedSourceError("checkout is not the clean exact source commit")
    if platform.python_version_tuple()[:2] != ("3", "12"):
        raise TestedSourceError("publisher must run with Python 3.12")
    lock = checkout / "uv.lock"
    if not lock.is_file():
        raise TestedSourceError("locked dependency input is unavailable")
    return validate_record(
        TestedSourceRecord(
            SCHEMA_VERSION,
            repository,
            source_sha,
            identity.version,
            hashlib.sha256(lock.read_bytes()).hexdigest(),
            "3.12",
            run_id,
            run_attempt,
            workflow_id,
            WORKFLOW_PATH,
            TEST_PROFILE,
            "success",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--workflow-id", required=True, type=int)
    parser.add_argument("--tagger-date", required=True)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise TestedSourceError("GITHUB_TOKEN is unavailable")
    record = record_from_checkout(args.repository, args.source_sha, args.run_id, args.run_attempt, args.workflow_id)
    result = TestedSourcePublisher(GitHubGitDataClient(token, args.repository), args.repository).publish(record, args.tagger_date)
    logger.info(f"Verified immutable tested-source ref {result.tag_ref} at {result.direct_commit_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
