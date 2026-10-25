"""Select the beta source ref from immutable, successfully tested Git evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath

from .tested_source_refs import REQUIRED_JOBS, WORKFLOW_PATH, TestedSourcePublisher
from .util.gh_cache import GitDataResponse, GitHubGitDataClient

BETA_REF = "refs/tags/auto-coder-beta-current"
SOURCE_BRANCH = "main"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OPERATION = re.compile(r"r[1-9][0-9]*\Z")
_WORKFLOW_API_ID = PurePosixPath(WORKFLOW_PATH).name


class BetaSourceError(RuntimeError):
    """Source selection is unavailable, ineligible, uncertain, or conflicting."""


class BetaSourceConflict(BetaSourceError):
    """An immutable record or mutable pointer has an unexpected binding."""


class MutationCertainty(str, Enum):
    NOT_SENT = "NOT_SENT"
    SENT_UNKNOWN = "SENT_UNKNOWN"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class SourceExecution:
    run_id: int
    run_attempt: int
    source_sha: str
    completed_at: str


@dataclass(frozen=True)
class BetaSelectionRecord:
    schema_version: int
    repository: str
    operation_id: str
    source_sha: str
    selected_run_id: int
    selected_run_attempt: int
    selected_completed_at: str
    tested_source_ref: str
    tested_source_oid: str
    proof_run_id: int
    proof_run_attempt: int
    expected_previous_sha: str | None

    def serialize(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class BetaSelectionReceipt:
    history_ref: str
    beta_ref: str
    certainty: MutationCertainty
    selected_sha: str
    observed_sha: str | None


def _object(response: GitDataResponse, operation: str, status: int = 200) -> dict[str, object]:
    if response.status != status or not isinstance(response.data, dict):
        raise BetaSourceError(f"{operation} is unavailable or malformed")
    return response.data


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BetaSourceError("required job completion timestamp is missing")
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise BetaSourceError("required job completion timestamp is malformed") from exc


class BetaSourceSelector:
    """Create one immutable operation record, then reconcile one mutable ref."""

    def __init__(self, client: GitHubGitDataClient, repository: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise BetaSourceError("repository is invalid")
        self.client = client
        self.repository = repository
        self.proofs = TestedSourcePublisher(client, repository)

    def _all(self, path: str, field: str, operation: str) -> list[object]:
        values: list[object] = []
        expected_total: int | None = None
        page = 1
        while True:
            response = _object(self.client.request("GET", f"{path}&page={page}"), operation)
            items = response.get(field)
            total = response.get("total_count")
            if not isinstance(items, list) or type(total) is not int or total < 0:
                raise BetaSourceError(f"{operation} is incomplete")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise BetaSourceError(f"{operation} changed while being observed")
            values.extend(items)
            if len(items) < 100:
                break
            page += 1
        if len(values) != expected_total:
            raise BetaSourceError(f"{operation} is incomplete")
        return values

    def execution(self, run_id: int, attempt: int) -> SourceExecution:
        run = _object(self.client.request("GET", f"actions/runs/{run_id}/attempts/{attempt}"), "source-test run")
        expected = {"id": run_id, "run_attempt": attempt, "path": WORKFLOW_PATH, "head_branch": SOURCE_BRANCH, "status": "completed", "conclusion": "success"}
        if any(run.get(key) != value for key, value in expected.items()):
            raise BetaSourceError("run is not the configured completed-success source execution")
        sha = run.get("head_sha")
        if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
            raise BetaSourceError("source execution has no full commit SHA")
        jobs = _object(self.client.request("GET", f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"), "source-test jobs")
        values = jobs.get("jobs")
        if not isinstance(values, list) or jobs.get("total_count") != len(values):
            raise BetaSourceError("source-test job observation is incomplete")
        completions: list[tuple[datetime, str]] = []
        for name in REQUIRED_JOBS:
            matches = [job for job in values if isinstance(job, dict) and job.get("name") == name]
            if len(matches) != 1 or matches[0].get("run_attempt") != attempt or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
                raise BetaSourceError(f"required source-test job is not authoritatively successful: {name}")
            raw = matches[0].get("completed_at")
            completions.append((_timestamp(raw), str(raw)))
        return SourceExecution(run_id, attempt, sha, max(completions)[1])

    def latest(self) -> SourceExecution:
        runs = self._all(
            f"actions/workflows/{_WORKFLOW_API_ID}/runs?branch={SOURCE_BRANCH}&status=completed&per_page=100",
            "workflow_runs",
            "source-test run listing",
        )
        executions: list[SourceExecution] = []
        for run in runs:
            if not isinstance(run, dict) or type(run.get("id")) is not int or type(run.get("run_attempt")) is not int or run["run_attempt"] <= 0:
                raise BetaSourceError("source-test run listing is malformed")
            # GitHub has no attempts collection. The authoritative latest
            # run_attempt bounds the documented numbered-attempt endpoint.
            # Every numbered attempt must remain observable so an omitted
            # successful retry cannot silently change completion ordering.
            for attempt in range(1, run["run_attempt"] + 1):
                exact = _object(self.client.request("GET", f"actions/runs/{run['id']}/attempts/{attempt}"), "numbered run attempt")
                if exact.get("id") != run["id"] or exact.get("run_attempt") != attempt:
                    raise BetaSourceError("numbered run-attempt observation is inconsistent")
                if exact.get("status") == "completed" and exact.get("conclusion") == "success":
                    executions.append(self.execution(run["id"], attempt))
        if not executions:
            raise BetaSourceError("no eligible successful source execution exists")
        greatest = max(_timestamp(item.completed_at) for item in executions)
        winners = [item for item in executions if _timestamp(item.completed_at) == greatest]
        if len(winners) != 1:
            raise BetaSourceError("latest successful completion is tied")
        proof = self.proofs.read(winners[0].source_sha)
        if proof is None:
            raise BetaSourceError("latest source execution has no immutable tested-source proof")
        self.proofs.verify_run(proof.record, recorded_attempt=True)
        return winners[0]

    def _history_name(self, operation_id: str) -> str:
        if _OPERATION.fullmatch(operation_id) is None:
            raise BetaSourceError("operation identity must be r<RUN>")
        return f"auto-coder-beta-selection-{operation_id}"

    @staticmethod
    def _effect_name(operation_id: str) -> str:
        return f"auto-coder-beta-selection-{operation_id}-pointer-attempt"

    def _read_record(self, name: str) -> BetaSelectionRecord | None:
        response = self.client.request("GET", f"git/ref/tags/{name}")
        if response.status == 404:
            return None
        ref = _object(response, "selection history ref")
        target = ref.get("object")
        if ref.get("ref") != f"refs/tags/{name}" or not isinstance(target, dict) or target.get("type") != "tag":
            raise BetaSourceConflict("selection history ref is not an annotated tag")
        tag = _object(self.client.request("GET", f"git/tags/{target.get('sha')}"), "selection history object")
        message = tag.get("message")
        if tag.get("tag") != name or not isinstance(message, str):
            raise BetaSourceConflict("selection history object is malformed")
        try:
            data = json.loads(message)
            record = BetaSelectionRecord(**data)
        except (json.JSONDecodeError, TypeError) as exc:
            raise BetaSourceConflict("selection history annotation is invalid") from exc
        if record.serialize() != message or tag.get("object") != {"type": "commit", "sha": record.source_sha}:
            raise BetaSourceConflict("selection history content or commit anchor conflicts")
        return record

    def _pointer(self) -> str | None:
        response = self.client.request("GET", "git/ref/tags/auto-coder-beta-current")
        if response.status == 404:
            return None
        ref = _object(response, "beta pointer")
        target = ref.get("object")
        if ref.get("ref") != BETA_REF or not isinstance(target, dict) or target.get("type") != "commit" or not isinstance(target.get("sha"), str):
            raise BetaSourceConflict("beta pointer is not a direct commit ref")
        return str(target["sha"])

    def _publish_immutable_record(self, name: str, record: BetaSelectionRecord, operation: str) -> None:
        created = _object(
            self.client.request("POST", "git/tags", {"tag": name, "message": record.serialize(), "object": record.source_sha, "type": "commit"}),
            f"{operation} tag creation",
            201,
        )
        oid = created.get("sha")
        if not isinstance(oid, str) or _SHA.fullmatch(oid) is None:
            raise BetaSourceError(f"{operation} tag creation returned no object identity")
        try:
            self.client.request("POST", "git/refs", {"ref": f"refs/tags/{name}", "sha": oid})
        except RuntimeError:
            pass
        if self._read_record(name) != record:
            raise BetaSourceConflict(f"immutable {operation} record could not be confirmed")

    def advance(self, operation_id: str, expected_previous_sha: str | None) -> BetaSelectionReceipt:
        name = self._history_name(operation_id)
        record = self._read_record(name)
        if record is None:
            selected = self.latest()
            proof = self.proofs.read(selected.source_sha)
            if proof is None:
                raise BetaSourceError("selected proof disappeared")
            record = BetaSelectionRecord(1, self.repository, operation_id, selected.source_sha, selected.run_id, selected.run_attempt, selected.completed_at, proof.tag_ref, proof.tag_object_oid, proof.record.run_id, proof.record.run_attempt, expected_previous_sha)
            self._publish_immutable_record(name, record, "selection")
        elif record.repository != self.repository or record.operation_id != operation_id:
            raise BetaSourceConflict("operation record belongs to a different operation")

        proof = self.proofs.read(record.source_sha)
        if proof is None or proof.tag_ref != record.tested_source_ref or proof.tag_object_oid != record.tested_source_oid or (proof.record.run_id, proof.record.run_attempt) != (record.proof_run_id, record.proof_run_attempt):
            raise BetaSourceConflict("tested-source proof no longer matches the immutable selection")
        self.proofs.verify_run(proof.record, recorded_attempt=True)
        self.execution(record.selected_run_id, record.selected_run_attempt)
        current = self._pointer()
        if current == record.source_sha:
            return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.CONFIRMED, record.source_sha, current)
        if current != record.expected_previous_sha:
            return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SUPERSEDED, record.source_sha, current)

        effect_name = self._effect_name(operation_id)
        effect = self._read_record(effect_name)
        if effect is not None:
            if effect != record:
                raise BetaSourceConflict("pointer-attempt record conflicts with selection history")
            # The first request may have reached GitHub. An absent pointer is
            # not evidence that resending is safe.
            return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SENT_UNKNOWN, record.source_sha, current)

        # This is provably the first pointer effect. Re-evaluate eligibility
        # even when history was left behind by a stopped prior invocation.
        selected = self.latest()
        expected = SourceExecution(record.selected_run_id, record.selected_run_attempt, record.source_sha, record.selected_completed_at)
        if selected != expected:
            raise BetaSourceConflict("a later eligible execution exists before the first pointer effect")
        self._publish_immutable_record(effect_name, record, "pointer-attempt")
        try:
            if current is None:
                response = self.client.request("POST", "git/refs", {"ref": BETA_REF, "sha": record.source_sha})
                if response.status not in (201, 422):
                    return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.REJECTED, record.source_sha, current)
            else:
                response = self.client.request("PATCH", "git/refs/tags/auto-coder-beta-current", {"sha": record.source_sha, "force": False})
                if response.status != 200:
                    return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.REJECTED, record.source_sha, current)
        except RuntimeError:
            pass
        try:
            observed = self._pointer()
        except (RuntimeError, BetaSourceError):
            return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SENT_UNKNOWN, record.source_sha, None)
        certainty = MutationCertainty.CONFIRMED if observed == record.source_sha else MutationCertainty.SENT_UNKNOWN
        return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, certainty, record.source_sha, observed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--operation-id", required=True, help="Stable originating Actions run identity, formatted r<RUN_ID>")
    parser.add_argument("--expected-previous-sha")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise BetaSourceError("GITHUB_TOKEN is required")
    receipt = BetaSourceSelector(GitHubGitDataClient(token, args.repository), args.repository).advance(args.operation_id, args.expected_previous_sha)
    print(json.dumps(asdict(receipt), sort_keys=True))
    if receipt.certainty != MutationCertainty.CONFIRMED:
        raise BetaSourceError(f"beta source advance did not confirm: {receipt.certainty.value}")


if __name__ == "__main__":
    main()
