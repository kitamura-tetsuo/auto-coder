"""Read-only, exact Git evidence for an operator-selected beta source revision.

The tested-source proof and the later beta-selected execution are independent
successful Actions attempts. A mutable SHA alone cannot identify the selection
when a second operation selects the very same commit.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from auto_coder.beta_source_selection import BETA_REF, CURRENT_SELECTION_REF, BetaSelectionRecord, BetaSourceSelector, SourceExecution, _timestamp
from auto_coder.tested_source_refs import REQUIRED_JOBS, TestedSourcePublisher
from auto_coder.util.gh_cache import GitDataResponse, GitHubGitDataClient

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OID = re.compile(r"[0-9a-f]{40}\Z")
_NAME = re.compile(r"auto-coder-beta-selection-(r[1-9][0-9]*)\Z")


class SourceEvidenceError(RuntimeError):
    """The current beta selection or source execution is not authoritative."""


@dataclass(frozen=True)
class RequiredJob:
    name: str
    job_id: int
    status: str
    conclusion: str
    completed_at: str


@dataclass(frozen=True)
class VerifiedBetaEvidence:
    repository: str
    source_sha: str
    distribution_version: str
    lock_sha256: str
    tested_source_ref: str
    tested_source_oid: str
    tested_source_record: str
    proof_run_id: int
    proof_run_attempt: int
    proof_jobs: tuple[RequiredJob, ...]
    beta_ref: str
    beta_pointer_sha: str
    selection_ref: str
    selection_oid: str
    selection_record: str
    selection_operation_id: str
    selected_run_id: int
    selected_run_attempt: int
    selected_workflow_id: int
    selected_workflow_path: str
    selected_branch: str
    selected_status: str
    selected_conclusion: str
    selected_completed_at: str
    selected_jobs: tuple[RequiredJob, ...]

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def _document(response: GitDataResponse, purpose: str) -> dict[str, object]:
    if response.status != 200 or not isinstance(response.data, dict):
        raise SourceEvidenceError(f"{purpose} unavailable")
    return response.data


class BetaEvidenceReader:
    """Consume only configured GitHub REST reads; never advance a beta ref."""

    def __init__(self, client: GitHubGitDataClient, repository: str) -> None:
        self.client = client
        self.repository = repository

    def read_current(self) -> VerifiedBetaEvidence:
        """Read the confirmed selection, without discovering a newer CI run.

        A sampler must distinguish a verified replacement (including another
        same-SHA selection) from missing or inconsistent remote evidence.
        """
        selector = BetaSourceSelector(self.client, self.repository)
        source_sha = selector._pointer()
        if source_sha is None:
            raise SourceEvidenceError("current beta source unavailable")
        evidence = self.read(source_sha)
        if selector._pointer() != evidence.source_sha or selector._current_selection_oid() != evidence.selection_oid:
            raise SourceEvidenceError("beta selection moved during observation")
        return evidence

    def _jobs(self, run_id: int, attempt: int) -> tuple[RequiredJob, ...]:
        data = _document(self.client.request("GET", f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"), "source required jobs")
        values = data.get("jobs")
        if not isinstance(values, list) or type(data.get("total_count")) is not int or data["total_count"] != len(values):
            raise SourceEvidenceError("required job observation incomplete")
        results: list[RequiredJob] = []
        for name in sorted(REQUIRED_JOBS):
            matches = [item for item in values if isinstance(item, dict) and item.get("name") == name]
            if len(matches) != 1:
                raise SourceEvidenceError("required job is missing or duplicated")
            item = matches[0]
            if type(item.get("id")) is not int or item["id"] <= 0 or item.get("run_attempt") != attempt or item.get("status") != "completed" or item.get("conclusion") != "success" or not isinstance(item.get("completed_at"), str):
                raise SourceEvidenceError("required job is not completed successfully")
            results.append(RequiredJob(name, item["id"], "completed", "success", item["completed_at"]))
        return tuple(results)

    def read(self, source_sha: str) -> VerifiedBetaEvidence:
        if not isinstance(source_sha, str) or _SHA.fullmatch(source_sha) is None:
            raise SourceEvidenceError("source SHA must be an exact full Git commit")
        selector = BetaSourceSelector(self.client, self.repository)
        pointer_sha = selector._pointer()
        if pointer_sha != source_sha:
            raise SourceEvidenceError("selected beta pointer differs from the requested source")

        current = _document(self.client.request("GET", "git/ref/tags/auto-coder-beta-selection-current"), "current beta selection")
        target = current.get("object")
        if current.get("ref") != CURRENT_SELECTION_REF or not isinstance(target, dict) or target.get("type") != "tag" or not isinstance(target.get("sha"), str) or _OID.fullmatch(target["sha"]) is None:
            raise SourceEvidenceError("current beta selection is not an annotated history tag")
        oid = target["sha"]
        tag = _document(self.client.request("GET", f"git/tags/{oid}"), "current selection tag object")
        name = tag.get("tag")
        message = tag.get("message")
        match = _NAME.fullmatch(name) if isinstance(name, str) else None
        if match is None or not isinstance(message, str) or len(message) > 4096:
            raise SourceEvidenceError("current selection tag is not a bounded operation record")
        assert isinstance(name, str)
        record = selector._read_record(name)
        tagged_commit = tag.get("object")
        if not isinstance(record, BetaSelectionRecord) or record.serialize() != message or not isinstance(tagged_commit, dict) or tagged_commit.get("type") != "commit" or tagged_commit.get("sha") != source_sha:
            raise SourceEvidenceError("current selection record differs from its immutable history")
        history = _document(self.client.request("GET", f"git/ref/tags/{name}"), "selection history ref")
        history_target = history.get("object")
        if history.get("ref") != f"refs/tags/{name}" or not isinstance(history_target, dict) or history_target.get("type") != "tag" or history_target.get("sha") != oid:
            raise SourceEvidenceError("current selection history identity has changed")
        if record.schema_version != 1 or record.repository != self.repository or record.operation_id != match.group(1) or record.source_sha != source_sha or record.selected_run_attempt <= 0:
            raise SourceEvidenceError("selection operation identity conflicts with its source")

        publisher = TestedSourcePublisher(self.client, self.repository)
        proof = publisher.read(source_sha)
        if proof is None or proof.tag_ref != record.tested_source_ref or proof.tag_object_oid != record.tested_source_oid or (proof.record.run_id, proof.record.run_attempt) != (record.proof_run_id, record.proof_run_attempt):
            raise SourceEvidenceError("immutable tested-source proof differs from the selection")
        publisher.verify_run(proof.record, recorded_attempt=True)
        original_jobs = self._jobs(proof.record.run_id, proof.record.run_attempt)
        selected: SourceExecution = selector.execution(record.selected_run_id, record.selected_run_attempt)
        selected_jobs = self._jobs(record.selected_run_id, record.selected_run_attempt)
        if selected.source_sha != source_sha or selected.completed_at != record.selected_completed_at or _timestamp(selected.completed_at) != max(_timestamp(item.completed_at) for item in selected_jobs):
            raise SourceEvidenceError("selected execution or completion time conflicts")
        selected_run = _document(self.client.request("GET", f"actions/runs/{record.selected_run_id}/attempts/{record.selected_run_attempt}"), "selected source run")
        if selected_run.get("workflow_id") != proof.record.workflow_id or selected_run.get("head_branch") != "main":
            raise SourceEvidenceError("selected source workflow identity differs")

        return VerifiedBetaEvidence(
            self.repository,
            source_sha,
            proof.record.distribution_version,
            proof.record.lock_sha256,
            proof.tag_ref,
            proof.tag_object_oid,
            json.dumps(asdict(proof.record), sort_keys=True, separators=(",", ":")),
            proof.record.run_id,
            proof.record.run_attempt,
            original_jobs,
            BETA_REF,
            pointer_sha,
            CURRENT_SELECTION_REF,
            oid,
            record.serialize(),
            record.operation_id,
            selected.run_id,
            selected.run_attempt,
            proof.record.workflow_id,
            proof.record.workflow_path,
            "main",
            "completed",
            "success",
            selected.completed_at,
            selected_jobs,
        )

    def matches_original(self, original: str) -> bool:
        """Revalidate without ever adopting a later same-SHA proof or selection."""
        try:
            data = json.loads(original)
            if not isinstance(data, dict) or not isinstance(data.get("source_sha"), str):
                return False
            return self.read(data["source_sha"]).canonical() == original
        except (ValueError, TypeError, SourceEvidenceError, RuntimeError):
            return False
