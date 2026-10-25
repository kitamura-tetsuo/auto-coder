from __future__ import annotations

import pytest
import yaml

from auto_coder.beta_source_selection import BETA_REF, BetaSourceConflict, BetaSourceError, BetaSourceSelector, MutationCertainty
from auto_coder.tested_source_refs import REQUIRED_JOBS, SCHEMA_VERSION, TEST_PROFILE, WORKFLOW_PATH
from auto_coder.tested_source_refs import TestedSourceRecord as SourceRecord
from auto_coder.tested_source_refs import serialize_record
from auto_coder.util.gh_cache import GitDataResponse

SHA_A = "a" * 40
SHA_B = "b" * 40
PROOF_A = "c" * 40
PROOF_B = "d" * 40


class GitHubBoundary:
    def __init__(self) -> None:
        self.executions = {(11, 1): (SHA_A, "2026-10-01T01:00:00Z")}
        self.proofs: dict[str, tuple[str, SourceRecord]] = {}
        self.tags: dict[str, dict[str, object]] = {}
        self.refs: dict[str, tuple[str, str]] = {}
        self.pointer_response_lost = False
        self.before_pointer = None
        self.calls: list[tuple[str, str]] = []
        self.add_proof(SHA_A, PROOF_A, 101, 1)

    def add_proof(self, sha: str, oid: str, run_id: int, attempt: int) -> None:
        record = SourceRecord(SCHEMA_VERSION, "owner/repo", sha, f"1.0+g{sha}", "e" * 64, "3.12", run_id, attempt, 77, WORKFLOW_PATH, TEST_PROFILE, "success")
        name = f"auto-coder-tested-{sha}"
        self.proofs[sha] = (oid, record)
        self.tags[oid] = {"tag": name, "message": serialize_record(record), "object": {"type": "commit", "sha": sha}}
        self.refs[f"refs/tags/{name}"] = ("tag", oid)

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        self.calls.append((method, path))
        if method == "GET" and path == f"actions/workflows/{WORKFLOW_PATH}/runs?branch=main&status=completed&per_page=100&page=1":
            ids = sorted({run_id for run_id, _attempt in self.executions})
            return GitDataResponse(200, {"total_count": len(ids), "workflow_runs": [{"id": run_id} for run_id in ids]})
        if method == "GET" and path.startswith("actions/runs/") and path.endswith("/attempts?per_page=100&page=1"):
            run_id = int(path.split("/")[2])
            items = [{"run_attempt": attempt, "status": "completed", "conclusion": "success"} for candidate, attempt in self.executions if candidate == run_id]
            return GitDataResponse(200, {"total_count": len(items), "workflow_runs": items})
        if method == "GET" and path.startswith("actions/runs/") and "/attempts/" in path and path.endswith("/jobs?per_page=100"):
            run_id = int(path.split("/")[2])
            attempt = int(path.split("/attempts/")[1].split("/")[0])
            completed = self.executions.get((run_id, attempt), (None, "2026-09-01T00:00:00Z"))[1]
            jobs = [{"name": name, "status": "completed", "conclusion": "success", "run_attempt": attempt, "completed_at": completed} for name in REQUIRED_JOBS]
            return GitDataResponse(200, {"total_count": len(jobs), "jobs": jobs})
        if method == "GET" and path.startswith("actions/runs/") and "/attempts/" in path:
            run_id = int(path.split("/")[2])
            attempt = int(path.rsplit("/", 1)[1])
            found = self.executions.get((run_id, attempt))
            if found is None:
                for sha, (_oid, proof) in self.proofs.items():
                    if (proof.run_id, proof.run_attempt) == (run_id, attempt):
                        found = (sha, "2026-09-01T00:00:00Z")
                        break
            if found is None:
                return GitDataResponse(404)
            sha, _completed = found
            return GitDataResponse(200, {"id": run_id, "run_attempt": attempt, "workflow_id": 77, "head_sha": sha, "path": WORKFLOW_PATH, "head_branch": "main", "status": "completed", "conclusion": "success"})
        if method == "GET" and path.startswith("git/ref/tags/"):
            full = "refs/tags/" + path.removeprefix("git/ref/tags/")
            value = self.refs.get(full)
            return GitDataResponse(200, {"ref": full, "object": {"type": value[0], "sha": value[1]}}) if value else GitDataResponse(404)
        if method == "GET" and path.startswith("git/tags/"):
            value = self.tags.get(path.removeprefix("git/tags/"))
            return GitDataResponse(200, value) if value else GitDataResponse(404)
        if method == "POST" and path == "git/tags" and payload:
            oid = f"{len(self.tags) + 1:040x}"
            self.tags[oid] = {"tag": payload["tag"], "message": payload["message"], "object": {"type": "commit", "sha": payload["object"]}}
            return GitDataResponse(201, {"sha": oid})
        if method == "POST" and path == "git/refs" and payload:
            ref = str(payload["ref"])
            kind = "tag" if str(payload["sha"]) in self.tags else "commit"
            self.refs[ref] = (kind, str(payload["sha"]))
            if ref == BETA_REF and self.pointer_response_lost:
                raise RuntimeError("response lost")
            return GitDataResponse(201)
        if method == "PATCH" and path == "git/refs/tags/auto-coder-beta-current" and payload:
            if self.before_pointer:
                self.before_pointer()
            self.refs[BETA_REF] = ("commit", str(payload["sha"]))
            if self.pointer_response_lost:
                raise RuntimeError("response lost")
            return GitDataResponse(200)
        raise AssertionError((method, path, payload))


def selector(remote: GitHubBoundary) -> BetaSourceSelector:
    return BetaSourceSelector(remote, "owner/repo")  # type: ignore[arg-type]


def test_successful_advance_writes_history_before_pointer_and_never_uses_oci() -> None:
    remote = GitHubBoundary()
    receipt = selector(remote).advance("r900-a1", None)
    assert receipt.certainty == MutationCertainty.CONFIRMED
    assert receipt.selected_sha == SHA_A
    history_create = next(index for index, call in enumerate(remote.calls) if call == ("POST", "git/refs"))
    pointer_create = max(index for index, call in enumerate(remote.calls) if call == ("POST", "git/refs"))
    assert history_create < pointer_create
    assert not any("docker" in path or "packages" in path for _method, path in remote.calls)


def test_retry_stays_bound_to_record_when_a_later_execution_exists() -> None:
    remote = GitHubBoundary()
    selector(remote).advance("r900-a1", None)
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T02:00:00Z")
    remote.add_proof(SHA_B, PROOF_B, 102, 1)
    receipt = selector(remote).advance("r900-a1", None)
    assert receipt.certainty == MutationCertainty.CONFIRMED
    assert receipt.selected_sha == SHA_A


def test_latest_uses_job_completion_and_rejects_ties() -> None:
    remote = GitHubBoundary()
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T00:30:00Z")
    remote.add_proof(SHA_B, PROOF_B, 102, 1)
    assert selector(remote).latest().source_sha == SHA_A
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T01:00:00Z")
    with pytest.raises(BetaSourceError, match="tied"):
        selector(remote).latest()


def test_missing_latest_proof_does_not_fall_back() -> None:
    remote = GitHubBoundary()
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T02:00:00Z")
    with pytest.raises(BetaSourceError, match="no immutable"):
        selector(remote).advance("r900-a1", None)
    assert BETA_REF not in remote.refs


def test_lost_pointer_response_is_confirmed_by_exact_readback() -> None:
    remote = GitHubBoundary()
    remote.pointer_response_lost = True
    assert selector(remote).advance("r900-a1", None).certainty == MutationCertainty.CONFIRMED


def test_manual_supersession_is_reported_without_overwrite() -> None:
    remote = GitHubBoundary()
    remote.refs[BETA_REF] = ("commit", SHA_B)
    receipt = selector(remote).advance("r900-a1", None)
    assert receipt.certainty == MutationCertainty.SUPERSEDED
    assert remote.refs[BETA_REF] == ("commit", SHA_B)


def test_workflow_is_independent_and_serialized() -> None:
    workflow = yaml.safe_load(open(".github/workflows/advance-beta-source.yml", encoding="utf-8"))
    assert workflow["concurrency"] == {"group": "advance-beta-source", "cancel-in-progress": False}
    assert workflow["permissions"] == {"actions": "read", "contents": "write"}
    rendered = str(workflow)
    assert "docker" not in rendered.lower()
    assert "github.run_id" in rendered and "github.run_attempt" in rendered
