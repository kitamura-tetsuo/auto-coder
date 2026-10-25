from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from auto_coder.beta_source_selection import BETA_REF, BetaSourceConflict, BetaSourceError, BetaSourceSelector, MutationCertainty, main
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
        self.drop_pointer_response = False
        self.pointer_readback_sha: str | None = None
        self.pointer_mutations = 0
        self.after_history = None
        self.failed_attempts: set[tuple[int, int]] = set()
        self.missing_jobs: set[tuple[int, int]] = set()
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
        if method == "GET" and path == "actions/workflows/test-source-for-runtime.yml/runs?branch=main&status=completed&per_page=100&page=1":
            ids = sorted({run_id for run_id, _attempt in self.executions})
            return GitDataResponse(200, {"total_count": len(ids), "workflow_runs": [{"id": run_id, "run_attempt": max(attempt for candidate, attempt in self.executions if candidate == run_id)} for run_id in ids]})
        if method == "GET" and path.startswith("actions/runs/") and "/attempts/" in path and path.endswith("/jobs?per_page=100"):
            run_id = int(path.split("/")[2])
            attempt = int(path.split("/attempts/")[1].split("/")[0])
            completed = self.executions.get((run_id, attempt), (None, "2026-09-01T00:00:00Z"))[1]
            jobs = [{"name": name, "status": "completed", "conclusion": "success", "run_attempt": attempt, "completed_at": completed} for name in REQUIRED_JOBS]
            if (run_id, attempt) in self.missing_jobs:
                jobs.pop()
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
            conclusion = "failure" if (run_id, attempt) in self.failed_attempts else "success"
            return GitDataResponse(200, {"id": run_id, "run_attempt": attempt, "workflow_id": 77, "head_sha": sha, "path": WORKFLOW_PATH, "head_branch": "main", "status": "completed", "conclusion": conclusion})
        if method == "GET" and path.startswith("git/ref/tags/"):
            full = "refs/tags/" + path.removeprefix("git/ref/tags/")
            value = self.refs.get(full)
            if full == BETA_REF and self.pointer_readback_sha is not None:
                value = ("commit", self.pointer_readback_sha)
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
            if ref == BETA_REF:
                self.pointer_mutations += 1
                if self.drop_pointer_response:
                    raise RuntimeError("response lost before an observable effect")
                self.refs[ref] = (kind, str(payload["sha"]))
                if self.pointer_response_lost:
                    raise RuntimeError("response lost")
            else:
                self.refs[ref] = (kind, str(payload["sha"]))
                if ref.endswith("-pointer-attempt") is False and ref.startswith("refs/tags/auto-coder-beta-selection-") and self.after_history:
                    self.after_history()
            return GitDataResponse(201)
        if method == "PATCH" and path == "git/refs/tags/auto-coder-beta-current" and payload:
            self.pointer_mutations += 1
            if self.before_pointer:
                self.before_pointer()
            if self.drop_pointer_response:
                raise RuntimeError("response lost before an observable effect")
            self.refs[BETA_REF] = ("commit", str(payload["sha"]))
            if self.pointer_response_lost:
                raise RuntimeError("response lost")
            return GitDataResponse(200)
        raise AssertionError((method, path, payload))


def selector(remote: GitHubBoundary) -> BetaSourceSelector:
    return BetaSourceSelector(remote, "owner/repo")  # type: ignore[arg-type]


class RealGitDataBoundary(GitHubBoundary):
    """Controlled Actions transport backed by real Git objects and refs."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository
        subprocess.run(["git", "init", "--bare", str(repository)], check=True, capture_output=True)
        self.real_ready = False
        super().__init__()
        self.real_ready = True
        commit = self._git("commit-tree", self._git("mktree", input=""), input="source\n", env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com"})
        self.executions = {(11, 1): (commit, "2026-10-01T01:00:00Z")}
        self.proofs.clear()
        self.tags.clear()
        self.refs.clear()
        self.add_proof(commit, "", 101, 1)

    def _git(self, *args: str, input: str | None = None, env: dict[str, str] | None = None) -> str:
        process_env = os.environ.copy()
        process_env.update(env or {})
        return subprocess.run(["git", f"--git-dir={self.repository}", *args], input=input, text=True, capture_output=True, check=True, env=process_env).stdout.strip()

    def add_proof(self, sha: str, oid: str, run_id: int, attempt: int) -> None:
        if not getattr(self, "real_ready", False):
            return super().add_proof(sha, oid, run_id, attempt)
        record = SourceRecord(SCHEMA_VERSION, "owner/repo", sha, f"1.0+g{sha}", "e" * 64, "3.12", run_id, attempt, 77, WORKFLOW_PATH, TEST_PROFILE, "success")
        name = f"auto-coder-tested-{sha}"
        tag_input = f"object {sha}\ntype commit\ntag {name}\ntagger test <test@example.com> 0 +0000\n\n{serialize_record(record)}\n"
        tag_oid = self._git("mktag", input=tag_input)
        self._git("update-ref", f"refs/tags/{name}", tag_oid)
        self.proofs[sha] = (tag_oid, record)

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        if method == "GET" and path.startswith("git/ref/tags/"):
            ref = "refs/tags/" + path.removeprefix("git/ref/tags/")
            found = subprocess.run(["git", f"--git-dir={self.repository}", "rev-parse", "--verify", ref], text=True, capture_output=True)
            if found.returncode:
                return GitDataResponse(404)
            oid = found.stdout.strip()
            return GitDataResponse(200, {"ref": ref, "object": {"type": self._git("cat-file", "-t", oid), "sha": oid}})
        if method == "GET" and path.startswith("git/tags/"):
            oid = path.removeprefix("git/tags/")
            raw = self._git("cat-file", "-p", oid)
            headers, message = raw.split("\n\n", 1)
            fields = dict(line.split(" ", 1) for line in headers.splitlines() if line.split(" ", 1)[0] in {"object", "type", "tag"})
            return GitDataResponse(200, {"tag": fields["tag"], "message": message, "object": {"type": fields["type"], "sha": fields["object"]}})
        if method == "POST" and path == "git/tags" and payload:
            tag_input = f"object {payload['object']}\ntype commit\ntag {payload['tag']}\ntagger test <test@example.com> 0 +0000\n\n{payload['message']}\n"
            return GitDataResponse(201, {"sha": self._git("mktag", input=tag_input)})
        if method == "POST" and path == "git/refs" and payload:
            ref = str(payload["ref"])
            if subprocess.run(["git", f"--git-dir={self.repository}", "show-ref", "--verify", "--quiet", ref]).returncode == 0:
                return GitDataResponse(422)
            self._git("update-ref", ref, str(payload["sha"]))
            if ref == BETA_REF:
                self.pointer_mutations += 1
            return GitDataResponse(201)
        if method == "PATCH" and path == "git/refs/tags/auto-coder-beta-current" and payload:
            self.pointer_mutations += 1
            self._git("update-ref", BETA_REF, str(payload["sha"]))
            return GitDataResponse(200)
        return super().request(method, path, payload)


def test_successful_advance_writes_history_before_pointer_and_never_uses_oci() -> None:
    remote = GitHubBoundary()
    receipt = selector(remote).advance("r900", None)
    assert receipt.certainty == MutationCertainty.CONFIRMED
    assert receipt.selected_sha == SHA_A
    history_create = next(index for index, call in enumerate(remote.calls) if call == ("POST", "git/refs"))
    pointer_create = max(index for index, call in enumerate(remote.calls) if call == ("POST", "git/refs"))
    assert history_create < pointer_create
    assert not any("docker" in path or "packages" in path for _method, path in remote.calls)


def test_cli_workflow_input_publishes_real_git_objects(_use_custom_subprocess_mock, monkeypatch, tmp_path: Path, capsys) -> None:
    remote = RealGitDataBoundary(tmp_path / "producer.git")
    monkeypatch.setenv("GITHUB_TOKEN", "controlled-token")
    monkeypatch.setattr("auto_coder.beta_source_selection.GitHubGitDataClient", lambda *_args: remote)
    monkeypatch.setattr(sys, "argv", ["beta-source-selection", "--repository", "owner/repo", "--operation-id", "r900"])

    main()

    output = json.loads(capsys.readouterr().out)
    selected = remote.executions[(11, 1)][0]
    assert output["certainty"] == "CONFIRMED"
    assert remote._git("rev-parse", BETA_REF) == selected
    history_oid = remote._git("rev-parse", "refs/tags/auto-coder-beta-selection-r900")
    assert remote._git("cat-file", "-t", history_oid) == "tag"
    assert remote._git("rev-parse", "refs/tags/auto-coder-beta-selection-r900^{}") == selected


def test_retry_stays_bound_to_record_when_a_later_execution_exists() -> None:
    remote = GitHubBoundary()
    selector(remote).advance("r900", None)
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T02:00:00Z")
    remote.add_proof(SHA_B, PROOF_B, 102, 1)
    receipt = selector(remote).advance("r900", None)
    assert receipt.certainty == MutationCertainty.CONFIRMED
    assert receipt.selected_sha == SHA_A
    assert receipt.history_ref == "refs/tags/auto-coder-beta-selection-r900"


def test_latest_uses_job_completion_and_rejects_ties() -> None:
    remote = GitHubBoundary()
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T00:30:00Z")
    remote.add_proof(SHA_B, PROOF_B, 102, 1)
    assert selector(remote).latest().source_sha == SHA_A
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T01:00:00Z")
    with pytest.raises(BetaSourceError, match="tied"):
        selector(remote).latest()


def test_failed_latest_run_is_ignored_but_incomplete_success_fails_closed() -> None:
    remote = GitHubBoundary()
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T02:00:00Z")
    remote.add_proof(SHA_B, PROOF_B, 102, 1)
    remote.failed_attempts.add((12, 1))
    assert selector(remote).latest().source_sha == SHA_A

    remote.failed_attempts.clear()
    remote.missing_jobs.add((12, 1))
    with pytest.raises(BetaSourceError, match="required source-test job"):
        selector(remote).latest()


def test_later_same_sha_new_run_reuses_original_immutable_proof() -> None:
    remote = GitHubBoundary()
    remote.executions[(12, 1)] = (SHA_A, "2026-10-01T02:00:00Z")

    receipt = selector(remote).advance("r903", None)

    assert receipt.certainty == MutationCertainty.CONFIRMED
    history_oid = remote.refs["refs/tags/auto-coder-beta-selection-r903"][1]
    annotation = json.loads(str(remote.tags[history_oid]["message"]))
    assert (annotation["selected_run_id"], annotation["selected_run_attempt"]) == (12, 1)
    assert (annotation["proof_run_id"], annotation["proof_run_attempt"]) == (101, 1)


def test_missing_latest_proof_does_not_fall_back() -> None:
    remote = GitHubBoundary()
    remote.executions[(12, 1)] = (SHA_B, "2026-10-01T02:00:00Z")
    with pytest.raises(BetaSourceError, match="no immutable"):
        selector(remote).advance("r900", None)
    assert BETA_REF not in remote.refs


def test_lost_pointer_response_is_confirmed_by_exact_readback() -> None:
    remote = GitHubBoundary()
    remote.pointer_response_lost = True
    assert selector(remote).advance("r900", None).certainty == MutationCertainty.CONFIRMED


def test_supported_numbered_attempt_routes_select_later_same_run_attempt() -> None:
    remote = GitHubBoundary()
    remote.executions[(11, 2)] = (SHA_A, "2026-10-01T02:00:00Z")

    receipt = selector(remote).advance("r904", None)

    history_oid = remote.refs["refs/tags/auto-coder-beta-selection-r904"][1]
    annotation = json.loads(str(remote.tags[history_oid]["message"]))
    assert (annotation["selected_run_id"], annotation["selected_run_attempt"]) == (11, 2)
    assert (annotation["proof_run_id"], annotation["proof_run_attempt"]) == (101, 1)
    assert receipt.certainty == MutationCertainty.CONFIRMED
    assert ("GET", "actions/runs/11/attempts/1") in remote.calls
    assert ("GET", "actions/runs/11/attempts/2") in remote.calls
    assert not any(path.endswith("/attempts?per_page=100&page=1") for _method, path in remote.calls)


def test_missing_numbered_attempt_fails_closed_without_older_fallback() -> None:
    remote = GitHubBoundary()
    remote.executions[(11, 2)] = (SHA_A, "2026-10-01T02:00:00Z")
    del remote.executions[(11, 1)]

    with pytest.raises(BetaSourceError, match="numbered run attempt"):
        selector(remote).latest()


def test_later_execution_after_history_blocks_first_effect_and_retry() -> None:
    remote = GitHubBoundary()

    def publish_later() -> None:
        remote.executions[(12, 1)] = (SHA_B, "2026-10-01T02:00:00Z")
        remote.add_proof(SHA_B, PROOF_B, 102, 1)

    remote.after_history = publish_later
    with pytest.raises(BetaSourceConflict, match="later eligible"):
        selector(remote).advance("r900", None)
    assert remote.pointer_mutations == 0
    assert "refs/tags/auto-coder-beta-selection-r900" in remote.refs
    assert "refs/tags/auto-coder-beta-selection-r900-pointer-attempt" not in remote.refs

    remote.after_history = None
    with pytest.raises(BetaSourceConflict, match="later eligible"):
        selector(remote).advance("r900", None)
    assert remote.pointer_mutations == 0


def test_ambiguous_create_with_absent_readback_never_resends() -> None:
    remote = GitHubBoundary()
    remote.drop_pointer_response = True

    first = selector(remote).advance("r900", None)
    second = selector(remote).advance("r900", None)

    assert first.certainty == second.certainty == MutationCertainty.SENT_UNKNOWN
    assert remote.pointer_mutations == 1
    assert "refs/tags/auto-coder-beta-selection-r900-pointer-attempt" in remote.refs


def test_update_response_loss_and_mismatching_readback_are_reconciled() -> None:
    remote = GitHubBoundary()
    remote.executions = {(12, 1): (SHA_B, "2026-10-01T02:00:00Z")}
    remote.add_proof(SHA_B, PROOF_B, 102, 1)
    remote.refs[BETA_REF] = ("commit", SHA_A)
    remote.pointer_response_lost = True
    assert selector(remote).advance("r901", SHA_A).certainty == MutationCertainty.CONFIRMED
    assert remote.pointer_mutations == 1

    mismatch = GitHubBoundary()
    mismatch.pointer_readback_sha = SHA_B
    receipt = selector(mismatch).advance("r902", SHA_B)
    assert receipt.certainty == MutationCertainty.SENT_UNKNOWN
    assert receipt.observed_sha == SHA_B


def test_manual_supersession_is_reported_without_overwrite() -> None:
    remote = GitHubBoundary()
    remote.refs[BETA_REF] = ("commit", SHA_B)
    receipt = selector(remote).advance("r900", None)
    assert receipt.certainty == MutationCertainty.SUPERSEDED
    assert remote.refs[BETA_REF] == ("commit", SHA_B)


def test_workflow_is_independent_and_serialized() -> None:
    workflow = yaml.safe_load(open(".github/workflows/advance-beta-source.yml", encoding="utf-8"))
    assert workflow["concurrency"] == {"group": "advance-beta-source", "cancel-in-progress": False}
    assert workflow["permissions"] == {"actions": "read", "contents": "write"}
    rendered = str(workflow)
    assert "docker" not in rendered.lower()
    assert "--operation-id 'r${{ github.run_id }}'" in rendered
    assert "github.run_attempt" not in rendered
