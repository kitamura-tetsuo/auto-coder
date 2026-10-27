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
# A direct commit ref cannot distinguish two successful selections of one SHA.
# This second pointer names the exact immutable selection history tag object.
CURRENT_SELECTION_REF = "refs/tags/auto-coder-beta-selection-current"
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
            f"actions/workflows/{_WORKFLOW_API_ID}/runs?branch={SOURCE_BRANCH}&per_page=100",
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
                status = exact.get("status")
                conclusion = exact.get("conclusion")
                if status == "completed":
                    if not isinstance(conclusion, str):
                        raise BetaSourceError("completed run-attempt conclusion is unavailable")
                elif status in {"queued", "in_progress", "waiting", "pending", "requested"}:
                    if conclusion is not None:
                        raise BetaSourceError("active run-attempt conclusion is inconsistent")
                    continue
                else:
                    raise BetaSourceError("run-attempt status is unavailable or unsupported")
                if conclusion == "success":
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
        direct = tag.get("object")
        if record.serialize() != message or not isinstance(direct, dict) or direct.get("type") != "commit" or direct.get("sha") != record.source_sha:
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

    def _current_selection_oid(self) -> str | None:
        response = self.client.request("GET", "git/ref/tags/auto-coder-beta-selection-current")
        if response.status == 404:
            return None
        ref = _object(response, "current selection identity")
        target = ref.get("object")
        if ref.get("ref") != CURRENT_SELECTION_REF or not isinstance(target, dict) or target.get("type") != "tag" or not isinstance(target.get("sha"), str) or _SHA.fullmatch(target["sha"]) is None:
            raise BetaSourceConflict("current selection is not an immutable tag object")
        return target["sha"]

    def _confirm_current_selection(self, record: BetaSelectionRecord) -> BetaSelectionReceipt:
        """Confirm both pointers and the exact history identity, including same-SHA replacements."""
        name = self._history_name(record.operation_id)
        history = _object(self.client.request("GET", f"git/ref/tags/{name}"), "immutable selection ref")
        target = history.get("object")
        if history.get("ref") != f"refs/tags/{name}" or not isinstance(target, dict) or target.get("type") != "tag" or not isinstance(target.get("sha"), str) or _SHA.fullmatch(target["sha"]) is None:
            raise BetaSourceConflict("immutable selection identity is missing")
        oid = target["sha"]
        current_oid = self._current_selection_oid()
        if current_oid != oid:
            if current_oid is not None:
                previous_tag = _object(self.client.request("GET", f"git/tags/{current_oid}"), "current selection history object")
                previous_name = previous_tag.get("tag")
                if not isinstance(previous_name, str) or re.fullmatch(r"auto-coder-beta-selection-r[1-9][0-9]*", previous_name) is None:
                    raise BetaSourceConflict("current selection has no valid immutable operation identity")
                prior_ref = _object(self.client.request("GET", f"git/ref/tags/{previous_name}"), "prior selection history ref")
                prior_target = prior_ref.get("object")
                if not isinstance(prior_target, dict) or prior_target.get("type") != "tag" or prior_target.get("sha") != current_oid:
                    raise BetaSourceConflict("current selection history object is not immutable")
                current_record = self._read_record(previous_name)
                if current_record is None or current_record.serialize() != previous_tag.get("message") or current_record.repository != self.repository or current_record.operation_id != previous_name.removeprefix("auto-coder-beta-selection-"):
                    raise BetaSourceConflict("current selection record does not match its historical identity")
                if int(current_record.operation_id[1:]) >= int(record.operation_id[1:]):
                    return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SUPERSEDED, record.source_sha, self._pointer())
            marker = f"{name}-current-attempt"
            existing_attempt = self._read_record(marker)
            if existing_attempt is not None:
                if existing_attempt != record:
                    raise BetaSourceConflict("immutable current-selection attempt conflicts")
                # A response may have been lost after this marker. Neither an
                # old tag OID nor a missing ref proves that a new write is safe.
                return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SENT_UNKNOWN, record.source_sha, self._pointer())
            # The selection has a durable pointer-attempt record already. A
            # different current record can only be superseded by a newly
            # successful source-test selection, never by an older retry.
            expected = SourceExecution(record.selected_run_id, record.selected_run_attempt, record.source_sha, record.selected_completed_at)
            if self.latest() != expected:
                return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SUPERSEDED, record.source_sha, self._pointer())
            self._publish_immutable_record(marker, record, "current-selection attempt")
            try:
                if current_oid is None:
                    self.client.request("POST", "git/refs", {"ref": CURRENT_SELECTION_REF, "sha": oid})
                else:
                    self.client.request("PATCH", "git/refs/tags/auto-coder-beta-selection-current", {"sha": oid, "force": True})
            except RuntimeError:
                pass
        try:
            confirmed = self._pointer() == record.source_sha and self._current_selection_oid() == oid
        except (RuntimeError, BetaSourceError):
            confirmed = False
        return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.CONFIRMED if confirmed else MutationCertainty.SENT_UNKNOWN, record.source_sha, record.source_sha if confirmed else None)

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
        effect_name = self._effect_name(operation_id)
        effect = self._read_record(effect_name)
        if effect is not None:
            if effect != record:
                raise BetaSourceConflict("pointer-attempt record conflicts with selection history")
            if current == record.source_sha:
                return self._confirm_current_selection(record)
            if current != record.expected_previous_sha:
                return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SUPERSEDED, record.source_sha, current)
            # The first request may have reached GitHub. An absent pointer is
            # not evidence that resending is safe.
            return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SENT_UNKNOWN, record.source_sha, current)

        if current == record.source_sha and current != record.expected_previous_sha:
            # Matching source identity is not operation attribution. Without
            # this operation's durable pre-send record, another operation or
            # trusted writer established the current pointer.
            return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SUPERSEDED, record.source_sha, current)
        if current != record.expected_previous_sha:
            return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SUPERSEDED, record.source_sha, current)

        # This is provably the first pointer effect. Re-evaluate eligibility
        # even when history was left behind by a stopped prior invocation.
        selected = self.latest()
        expected = SourceExecution(record.selected_run_id, record.selected_run_attempt, record.source_sha, record.selected_completed_at)
        if selected != expected:
            raise BetaSourceConflict("a later eligible execution exists before the first pointer effect")
        self._publish_immutable_record(effect_name, record, "pointer-attempt")
        if current == record.source_sha:
            return self._confirm_current_selection(record)
        try:
            if current is None:
                response = self.client.request("POST", "git/refs", {"ref": BETA_REF, "sha": record.source_sha})
                if response.status not in (201, 422) and response.status < 500:
                    return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.REJECTED, record.source_sha, current)
            else:
                response = self.client.request("PATCH", "git/refs/tags/auto-coder-beta-current", {"sha": record.source_sha, "force": False})
                if response.status != 200 and response.status < 500:
                    return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.REJECTED, record.source_sha, current)
        except RuntimeError:
            pass
        try:
            observed = self._pointer()
        except (RuntimeError, BetaSourceError):
            return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SENT_UNKNOWN, record.source_sha, None)
        if observed == record.source_sha:
            return self._confirm_current_selection(record)
        return BetaSelectionReceipt(f"refs/tags/{name}", BETA_REF, MutationCertainty.SENT_UNKNOWN, record.source_sha, observed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--operation-id", required=True, help="Stable originating Actions run identity, formatted r<RUN_ID>")
    parser.add_argument("--expected-previous-sha")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise BetaSourceError("GITHUB_TOKEN is required")
    expected_previous_sha = args.expected_previous_sha or None
    if expected_previous_sha is not None and _SHA.fullmatch(expected_previous_sha) is None:
        raise BetaSourceError("expected previous source must be a full lowercase SHA-1")
    receipt = BetaSourceSelector(GitHubGitDataClient(token, args.repository), args.repository).advance(args.operation_id, expected_previous_sha)
    print(json.dumps(asdict(receipt), sort_keys=True))
    if receipt.certainty != MutationCertainty.CONFIRMED:
        raise BetaSourceError(f"beta source advance did not confirm: {receipt.certainty.value}")


if __name__ == "__main__":
    main()
