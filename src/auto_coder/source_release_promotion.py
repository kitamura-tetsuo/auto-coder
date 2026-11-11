"""Explicit accepted-Git-source promotion with exact, readback-only recovery."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from .beta_source_selection import BETA_REF, CURRENT_SELECTION_REF, BetaSelectionRecord, BetaSourceError, BetaSourceSelector
from .management.beta_runtime_smoke import PROFILE_ID, REQUIRED_PREDICATES, SmokeProfile
from .management.source_acceptance import SourceAcceptanceError, SourceAcceptancePublisher
from .management.source_evidence import BetaEvidenceReader, SourceEvidenceError
from .source_release_catalog import (
    RELEASE_SELECTION_REF,
    RELEASE_WORKFLOW,
    SHA,
    SourceReleaseCandidate,
    SourceReleaseCatalog,
    SourceReleaseConflict,
    SourceReleaseError,
    SourceReleasePolicy,
    SourceReleaseRecord,
    SourceReleaseRequest,
    _digest,
    _object,
    _sha,
    canonical,
    digest,
)
from .tested_source_refs import WORKFLOW_PATH, TestedSourceError, TestedSourcePublisher
from .util.gh_cache import GitHubGitDataClient


def _mapping(value: object, purpose: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SourceReleaseError(f"{purpose} is missing or malformed")
    return value


def _finite(value: object, purpose: str) -> float:
    if not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise SourceReleaseError(f"{purpose} must be a finite positive timestamp")
    return float(value)


def _same(expected: object, actual: object, purpose: str) -> None:
    if type(expected) is not type(actual) or expected != actual:
        raise SourceReleaseError(f"{purpose} does not match the original immutable evidence")


def _required_string(value: object, purpose: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise SourceReleaseError(f"{purpose} is absent or malformed")
    return value


@dataclass(frozen=True)
class ValidatedAcceptance:
    local_evidence_id: str
    local_evidence_content_sha256: str
    beta_instance_id: str
    profile_id: str
    profile_revision: str


class AcceptedSourceValidator:
    """Check the original producer evidence, not a replacement live beta sample."""

    def __init__(self, client: GitHubGitDataClient, policy: SourceReleasePolicy, *, clock: Callable[[], float] = time.time) -> None:
        self.client, self.policy, self.clock = client, policy, clock

    def validate(self, request: SourceReleaseCandidate) -> ValidatedAcceptance:
        request.validate(self.policy)
        proofs = TestedSourcePublisher(self.client, self.policy.source_repository)
        proof = proofs.read(request.source_sha)
        if proof is None or proof.tag_ref != request.tested_source_ref or proof.tag_object_oid != request.tested_source_oid:
            raise SourceReleaseError("original tested-source ref/object is absent or mismatched")
        _same((request.ci_run_id, request.ci_run_attempt), (proof.record.run_id, proof.record.run_attempt), "original CI run")
        _same(request.tested_source_record_sha256, digest(asdict(proof.record)), "source-test record digest")
        proofs.verify_run(proof.record, recorded_attempt=True)

        acceptance_id = request.acceptance_ref.removeprefix("refs/tags/auto-coder-beta-acceptance-")
        accepted = SourceAcceptancePublisher(self.client, self.policy.source_repository).read(acceptance_id)
        if accepted is None or accepted.tag_ref != request.acceptance_ref or accepted.tag_object_oid != request.acceptance_oid or accepted.direct_commit_sha != request.source_sha:
            raise SourceReleaseError("exact published acceptance ref/object/commit is absent or mismatched")
        record = _mapping(json.loads(accepted.annotation), "published acceptance")
        local = _mapping(record.get("local_evidence"), "original local runtime evidence")
        _same(request.source_sha, local.get("source_sha"), "acceptance source")
        _same(self.policy.source_repository, local.get("repository"), "acceptance repository")
        _same(acceptance_id, local.get("operation_id"), "acceptance operation")
        _same("PASS", local.get("local_result"), "original local result")
        local_id = _required_string(local.get("local_evidence_id"), "local evidence ID")
        local_digest = _digest(record.get("local_evidence_content_sha256"), "local evidence digest")
        _same(record.get("local_evidence_id"), local_id, "acceptance local evidence ID")
        _same(f"{PROFILE_ID}:{acceptance_id}", local_id, "local evidence operation ID")

        profile = _mapping(local.get("profile"), "original runtime profile")
        try:
            captured_profile = SmokeProfile.from_document(profile)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise SourceReleaseError("original runtime profile is invalid") from exc
        if not any(item.profile_id == captured_profile.profile_id and item.content_revision == captured_profile.content_revision for item in self.policy.accepted_profiles):
            raise SourceReleaseError("original runtime profile/revision is not operator-allowed")

        instance_id = _required_string(local.get("instance_id"), "accepted beta instance")
        if instance_id not in self.policy.accepted_beta_instance_ids or local.get("channel") != "beta":
            raise SourceReleaseError("accepted beta instance/channel is not operator-allowed")
        window = _mapping(local.get("window"), "original acceptance window")
        opened, deadline = _finite(window.get("opened_at"), "window opening"), _finite(window.get("deadline"), "original deadline")
        completed = _finite(local.get("completed_at"), "acceptance completion")
        now = _finite(self.clock(), "promotion validation clock")
        if not math.isclose(deadline - opened, captured_profile.total_seconds, rel_tol=0, abs_tol=1e-3) or not opened < completed < deadline or completed > now or now - completed > self.policy.maximum_evidence_age_seconds:
            raise SourceReleaseError("acceptance is stale, in the future, or outside its original bounded window")

        binding = _mapping(local.get("binding"), "original accepted operation")
        _same(acceptance_id, binding.get("operation_id"), "original operation binding")
        _same(self.policy.source_repository, binding.get("source_repository"), "original source repository")
        _required_string(binding.get("intent_fingerprint"), "original intent fingerprint")
        _mapping(binding.get("accepted_authority"), "original authority receipt")
        intent = _mapping(binding.get("intent"), "original operation intent")
        target = _mapping(intent.get("target"), "accepted target")
        for field, expected in (("instance_id", instance_id), ("channel", "beta"), ("repository", local.get("worker_repository"))):
            _same(expected, target.get(field), f"target {field}")
        _same("run_beta_acceptance", intent.get("action"), "original operation action")
        params = intent.get("parameters")
        if not isinstance(params, list) or len(params) < 4:
            raise SourceReleaseError("original acceptance operation parameters are missing")
        values: dict[str, object] = {}
        for raw in params:
            pair = _mapping(raw, "original operation parameter")
            key = _required_string(pair.get("name"), "original operation parameter name")
            if key in values:
                raise SourceReleaseError("duplicate original operation parameter")
            values[key] = pair.get("value")
        _same(request.source_sha, values.get("source_sha"), "operation candidate")
        for key in ("deployment_operation_id", "supervisor_generation", "deployment_generation"):
            _required_string(values.get(key), f"original {key}")

        capture = _mapping(local.get("capture"), "original installed runtime capture")
        captured_at = _finite(capture.get("captured_at"), "original runtime capture time")
        if not opened <= captured_at < deadline:
            raise SourceReleaseError("runtime capture is outside the original acceptance window")
        source = _mapping(capture.get("source"), "original source and CI capture")
        self._source(source, request, proof)
        _same(f"refs/tags/auto-coder-beta-selection-{source['selection_operation_id']}", capture.get("selection_record_ref"), "original immutable selection record ref")
        deployment = _mapping(capture.get("deployment"), "original deployment capture")
        worker = _mapping(capture.get("worker"), "original installed worker capture")
        self._installed(source, deployment, worker, intent, values)
        self._samples(local, worker, captured_profile, opened, deadline, completed)
        return ValidatedAcceptance(local_id, local_digest, instance_id, captured_profile.profile_id, captured_profile.content_revision)

    def _source(self, source: dict[str, object], request: SourceReleaseCandidate, proof: object) -> None:
        record = proof.record  # type: ignore[attr-defined]
        expected = {
            "repository": self.policy.source_repository,
            "source_sha": request.source_sha,
            "distribution_version": record.distribution_version,
            "lock_sha256": record.lock_sha256,
            "tested_source_ref": request.tested_source_ref,
            "tested_source_oid": request.tested_source_oid,
            "tested_source_record": canonical(asdict(record)),
            "proof_run_id": request.ci_run_id,
            "proof_run_attempt": request.ci_run_attempt,
            "beta_ref": BETA_REF,
            "beta_pointer_sha": request.source_sha,
            "selected_workflow_id": record.workflow_id,
            "selected_workflow_path": WORKFLOW_PATH,
            "selected_branch": "main",
            "selected_status": "completed",
            "selected_conclusion": "success",
        }
        for key, value in expected.items():
            _same(value, source.get(key), f"captured source {key}")
        reader = BetaEvidenceReader(self.client, self.policy.source_repository)
        _same([asdict(item) for item in reader._jobs(record.run_id, record.run_attempt)], source.get("proof_jobs"), "original proof jobs")
        selector = BetaSourceSelector(self.client, self.policy.source_repository)
        selected_run, attempt = source.get("selected_run_id"), source.get("selected_run_attempt")
        if type(selected_run) is not int or type(attempt) is not int or selected_run <= 0 or attempt <= 0:
            raise SourceReleaseError("original beta selected Actions attempt is invalid")
        execution = selector.execution(selected_run, attempt)
        _same(request.source_sha, execution.source_sha, "selected execution source")
        _same(execution.completed_at, source.get("selected_completed_at"), "selected execution completion")
        _same([asdict(item) for item in reader._jobs(selected_run, attempt)], source.get("selected_jobs"), "selected original Actions jobs")
        selected = _object(self.client.request("GET", f"actions/runs/{selected_run}/attempts/{attempt}"), "selected workflow identity")
        _same(record.workflow_id, selected.get("workflow_id"), "selected workflow identity")
        _same("main", selected.get("head_branch"), "selected source branch")
        selection_record = source.get("selection_record")
        if not isinstance(selection_record, str) or len(selection_record) > 4096:
            raise SourceReleaseError("original selected history is missing")
        try:
            data = json.loads(selection_record)
            if not isinstance(data, dict) or set(data) != set(BetaSelectionRecord.__dataclass_fields__):
                raise ValueError("invalid selected history")
            selection = BetaSelectionRecord(**data)
        except (ValueError, TypeError) as exc:
            raise SourceReleaseError("original selected history is malformed") from exc
        if selection.serialize() != selection_record or selection.schema_version != 1:
            raise SourceReleaseError("original selected history is not canonical version 1")
        for field, expected_value in (
            ("repository", self.policy.source_repository),
            ("source_sha", request.source_sha),
            ("selected_run_id", selected_run),
            ("selected_run_attempt", attempt),
            ("selected_completed_at", execution.completed_at),
            ("tested_source_ref", request.tested_source_ref),
            ("tested_source_oid", request.tested_source_oid),
            ("proof_run_id", request.ci_run_id),
            ("proof_run_attempt", request.ci_run_attempt),
        ):
            _same(expected_value, getattr(selection, field), f"selected history {field}")
        _same(selection.operation_id, source.get("selection_operation_id"), "selected history operation")
        name = f"auto-coder-beta-selection-{selection.operation_id}"
        _same(CURRENT_SELECTION_REF, source.get("selection_ref"), "original captured selection pointer ref")
        oid = _sha(source.get("selection_oid"), "selected history object")
        ref = _object(self.client.request("GET", f"git/ref/tags/{name}"), "original immutable beta selection")
        _same(f"refs/tags/{name}", ref.get("ref"), "history ref identity")
        _same({"type": "tag", "sha": oid}, {key: _mapping(ref.get("object"), "history ref target").get(key) for key in ("type", "sha")}, "history object identity")
        annotation = _object(self.client.request("GET", f"git/tags/{oid}"), "original selection annotation")
        _same(name, annotation.get("tag"), "history tag name")
        _same(selection_record, annotation.get("message"), "history content")
        _same({"type": "commit", "sha": request.source_sha}, {key: _mapping(annotation.get("object"), "history direct commit").get(key) for key in ("type", "sha")}, "history direct target")

    @staticmethod
    def _installed(source: dict[str, object], deployment: dict[str, object], worker: dict[str, object], intent: dict[str, object], values: dict[str, object]) -> None:
        _same("succeeded", deployment.get("phase"), "successful deployment phase")
        _same("SUCCEEDED", deployment.get("stage"), "successful deployment stage")
        _same(values["deployment_operation_id"], deployment.get("operation_id"), "accepted deployment operation")
        _required_string(deployment.get("intent_fingerprint"), "original deployment intent fingerprint")
        _digest(deployment.get("snapshot_content_sha256"), "original lifecycle snapshot content identity")
        for key in ("operation_revision", "snapshot_revision"):
            value = deployment.get(key)
            if type(value) is not int or value < 0:
                raise SourceReleaseError(f"original deployment {key} is invalid")
        _same(source, deployment.get("accepted_evidence"), "deployment's original source proof")
        staged = _mapping(deployment.get("staged"), "installed source staging")
        launched = _mapping(deployment.get("new_worker"), "launched worker")
        for name, expected in (("source_sha", source["source_sha"]), ("distribution_version", source["distribution_version"]), ("lock_sha256", source["lock_sha256"]), ("runtime_profile", "python-3.12")):
            _same(expected, staged.get(name), f"staged {name}")
        ready = _digest(staged.get("content_identity"), "installed READY receipt identity")
        installed = _required_string(staged.get("installed_runtime_id"), "installed runtime ID")
        incarnation = _required_string(intent.get("expected_incarnation"), "expected worker incarnation")
        _same(installed, intent.get("expected_artifact"), "expected installed runtime")
        for name, expected in (("runtime_id", installed), ("process_run_id", incarnation), ("deployment_generation", values["deployment_generation"])):
            _same(expected, launched.get(name), f"launched {name}")
        _same(values["supervisor_generation"], deployment.get("supervisor_generation"), "original Supervisor incarnation")
        for name, expected in (
            ("supervisor_generation", values["supervisor_generation"]),
            ("process_run_id", incarnation),
            ("deployment_generation", values["deployment_generation"]),
            ("runtime_id", installed),
            ("installed_runtime_id", installed),
            ("ready_content_identity", ready),
            ("lock_sha256", source["lock_sha256"]),
        ):
            _same(expected, worker.get(name), f"installed worker {name}")
        for field, origin in (("source_revision", "build_embedded"), ("distribution_version", "installed_distribution_metadata")):
            provenance = _mapping(worker.get(field), f"installed {field}")
            _same(origin, provenance.get("origin"), f"installed {field} origin")
            _same(source["source_sha"] if field == "source_revision" else source["distribution_version"], provenance.get("value"), f"installed {field} identity")

    @staticmethod
    def _samples(local: dict[str, object], worker: dict[str, object], profile: SmokeProfile, opened: float, deadline: float, completed: float) -> None:
        checks = local.get("checks")
        if checks != [{"check": "local_result", "disposition": "PASS", "reason": "same_runtime_producer_heartbeats_advanced"}] or local.get("reason") != "same_runtime_producer_heartbeats_advanced":
            raise SourceReleaseError("claimed local PASS conflicts with original terminal check")
        samples = local.get("samples")
        if not isinstance(samples, list) or not profile.minimum_samples <= len(samples) <= 128:
            raise SourceReleaseError("original heartbeat sample count is insufficient")
        previous_time, previous_qualified_seq = opened, None
        nonces: set[str] = set()
        qualifying_count = 0
        for sample_index, item in enumerate(samples):
            sample = _mapping(item, f"original sample {sample_index}")
            sampled = _finite(sample.get("sampled_at"), "original sample time")
            if not previous_time <= sampled < deadline or (sample_index and sampled < previous_time + profile.interval_seconds):
                raise SourceReleaseError("sample is outside the original window or minimum interval")
            previous_time = sampled
            for field in ("nonce", "worker_request_id", "supervisor_request_id", "observation_time"):
                _required_string(sample.get(field), f"sample {field}")
            nonce = str(sample["nonce"])
            if nonce in nonces:
                raise SourceReleaseError("worker samples reused a nonce")
            nonces.add(nonce)
            for field in ("supervisor_generation", "process_run_id", "deployment_generation", "runtime_id"):
                _same(worker[field], _mapping(sample.get("observed_identity"), "sample observed incarnation").get(field), f"sample {field}")
            supervisor = _mapping(sample.get("supervisor"), "sample Supervisor")
            for field, expected in (("supervisor_incarnation", worker["supervisor_generation"]), ("status", "RUNNING"), ("alive", True), ("selected_runtime_id", worker["installed_runtime_id"])):
                _same(expected, supervisor.get(field), f"sample Supervisor {field}")
            runtime = _mapping(sample.get("runtime_binding"), "sample installed runtime binding")
            for field, name in (
                ("runtime_id", "installed_runtime_id"),
                ("content_identity", "ready_content_identity"),
                ("lock_sha256", "lock_sha256"),
                ("source_sha", "source_revision"),
                ("distribution_version", "distribution_version"),
                ("process_run_id", "process_run_id"),
                ("deployment_generation", "deployment_generation"),
            ):
                expected = _mapping(worker[name], f"captured {name}").get("value") if name in {"source_revision", "distribution_version"} else worker[name]
                _same(expected, runtime.get(field), f"sample runtime {field}")
            artifact = _mapping(sample.get("artifact"), "sample installed artifact")
            for field in ("source_revision", "distribution_version"):
                _same(worker[field], artifact.get(field), f"sample installed {field}")
            lifecycle = _mapping(sample.get("lifecycle"), "sample worker lifecycle")
            for field, expected in (("state", "running"), ("startup_reconciled", True), ("startup_reconciliation_error", None), ("daemon_serving", True)):
                if field not in lifecycle:
                    raise SourceReleaseError("original lifecycle predicate is absent")
                _same(expected, lifecycle[field], f"sample lifecycle {field}")
            _same("running", _mapping(sample.get("invocations"), "sample invocation gate").get("state"), "sample invocation gate")
            heartbeat = _mapping(sample.get("heartbeat"), "sample worker heartbeat")
            sequence = heartbeat.get("sequence")
            stage = heartbeat.get("stage")
            if heartbeat.get("available") is not True or type(sequence) is not int or sequence <= 0 or not isinstance(stage, str) or not stage.startswith("producer:"):
                raise SourceReleaseError("original sample has no authentic producer heartbeat")
            dispositions = sample.get("checks")
            if not isinstance(dispositions, list) or len(dispositions) != len(REQUIRED_PREDICATES) + 1:
                raise SourceReleaseError("original sample required-check dispositions are missing")
            expected_checks = [{"check": name, "disposition": "PASS"} for name in REQUIRED_PREDICATES]
            if dispositions[: len(REQUIRED_PREDICATES)] != expected_checks:
                raise SourceReleaseError("original sample failed a required startup/readiness check")
            qualifying = sample.get("qualifying")
            advancing = previous_qualified_seq is None or sequence > previous_qualified_seq
            if type(qualifying) is not bool or qualifying != advancing or dispositions[-1] != {"check": "heartbeat_progress", "disposition": "PASS" if advancing else "WAITING"}:
                raise SourceReleaseError("original sample claimed unearned heartbeat progress")
            if qualifying:
                previous_qualified_seq = sequence
                qualifying_count += 1
        if qualifying_count != profile.minimum_samples or samples[-1].get("qualifying") is not True or not previous_time <= completed < deadline:
            raise SourceReleaseError("local PASS was not earned within the original fixed deadline")


@dataclass(frozen=True)
class SourceReleaseOutcome:
    record: SourceReleaseRecord
    status: str
    catalog_oid: str
    release_url: str


class SourceReleasePromotion:
    """One catalog-first Git ref effect, then independently confirmed Release.

    No distributed CAS is claimed: the configured Actions concurrency group is
    the governed-writer boundary. External administrators can still race it;
    unexpected state and unreadable readback are not successful outcomes.
    """

    def __init__(self, client: GitHubGitDataClient, policy: SourceReleasePolicy, *, clock: Callable[[], float] = time.time) -> None:
        policy.validate()
        self.client, self.policy = client, policy
        self.catalog = SourceReleaseCatalog(client, policy.source_repository)
        self.validator = AcceptedSourceValidator(client, policy, clock=clock)

    def _dispatch(self, request: SourceReleaseRequest) -> None:
        run = _object(self.client.request("GET", f"actions/runs/{request.promotion_run_id}/attempts/{request.promotion_run_attempt}"), "promotion Actions attempt")
        expected = {
            "id": request.promotion_run_id,
            "run_attempt": request.promotion_run_attempt,
            "path": self.policy.promotion_workflow_path,
            "head_branch": self.policy.release_branch,
            "event": "workflow_dispatch",
        }
        if any(run.get(key) != value for key, value in expected.items()) or run.get("status") not in {"queued", "in_progress", "completed"}:
            raise SourceReleaseError("promotion is not the configured Actions workflow dispatch/attempt")
        repository = _mapping(run.get("repository"), "promotion Actions repository")
        _same(self.policy.source_repository, repository.get("full_name"), "promotion Actions repository")
        _sha(run.get("head_sha"), "promotion workflow checkout SHA")

    def _pointer(self) -> str | None:
        short = self.policy.release_ref.removeprefix("refs/")
        response = self.client.request("GET", f"git/ref/{short}")
        if response.status == 404:
            return None
        document = _object(response, "current release source pointer")
        target = _mapping(document.get("object"), "current release pointer target")
        if document.get("ref") != self.policy.release_ref or target.get("type") != "commit":
            raise SourceReleaseError("current release pointer is not a direct Git commit")
        return _sha(target.get("sha"), "current release commit")

    def _selection_oid(self) -> str | None:
        """Identify the selected promotion, including a later same-SHA release."""
        response = self.client.request("GET", f"git/ref/{RELEASE_SELECTION_REF.removeprefix('refs/')}")
        if response.status == 404:
            return None
        data = _object(response, "current release operation selection")
        target = _mapping(data.get("object"), "current release operation target")
        if data.get("ref") != RELEASE_SELECTION_REF or target.get("type") != "tag":
            raise SourceReleaseError("release operation selection is not an annotated catalog tag")
        return _sha(target.get("sha"), "current release catalog object")

    def _record(self, request: SourceReleaseRequest, acceptance: ValidatedAcceptance, previous: str | None) -> SourceReleaseRecord:
        return SourceReleaseRecord(
            2,
            "git-source-release",
            request.operation_id,
            request.source_repository,
            request.source_sha,
            request.ci_run_id,
            request.ci_run_attempt,
            request.tested_source_ref,
            request.tested_source_oid,
            request.tested_source_record_sha256,
            request.acceptance_ref,
            request.acceptance_oid,
            acceptance.local_evidence_id,
            acceptance.local_evidence_content_sha256,
            acceptance.beta_instance_id,
            acceptance.profile_id,
            acceptance.profile_revision,
            self.policy.revision,
            request.release_target,
            self.policy.release_ref,
            previous,
            request.promotion_run_id,
            request.promotion_run_attempt,
            RELEASE_WORKFLOW,
        )

    def _verify_original(self, record: SourceReleaseRecord, request: SourceReleaseRequest, acceptance: ValidatedAcceptance) -> None:
        # The original catalog, not workflow inputs, selects every retry value.
        expected = self._record(request, acceptance, record.expected_previous_sha)
        expected = SourceReleaseRecord(**{**asdict(expected), "original_promotion_run_attempt": record.original_promotion_run_attempt})
        if record != expected or record.original_promotion_run_attempt > request.promotion_run_attempt:
            raise SourceReleaseConflict("operation ID already binds a different candidate, evidence, policy or Actions dispatch")

    def _read_release(self, record: SourceReleaseRecord) -> str | None:
        response = self.client.request("GET", f"releases/tags/{record.tag}")
        if response.status == 404:
            return None
        release = _object(response, "exact GitHub Release")
        release_id = release.get("id")
        if release.get("tag_name") != record.tag or release.get("draft") is not False or release.get("prerelease") is not False or not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
            raise SourceReleaseConflict("GitHub Release does not match the immutable source catalog")
        url = release.get("html_url")
        if url != f"https://github.com/{self.policy.source_repository}/releases/tag/{record.tag}":
            raise SourceReleaseConflict("GitHub Release URL does not match the exact immutable tag")
        return url

    def _confirmed(self, record: SourceReleaseRecord, oid: str, status: str) -> SourceReleaseOutcome:
        observed = self.catalog.read(record.operation_id)
        if observed is None or observed.record != record or observed.tag_object_oid != oid:
            raise SourceReleaseError("NEEDS_ATTENTION: immutable release catalog is unreadable or changed")
        current = self._pointer()
        if current != record.source_sha or self._selection_oid() != oid:
            raise SourceReleaseError("SUPERSEDED: current release source or operation selection differs")
        release = self._read_release(record)
        if release is None:
            raise SourceReleaseError("NEEDS_ATTENTION: GitHub Release publication is unconfirmed")
        # Read each authority again after observing the release. An API success
        # response is neither a publication receipt nor a multi-resource CAS.
        if self._pointer() != record.source_sha or self._selection_oid() != oid or self.catalog.read(record.operation_id) != observed or self._read_release(record) != release:
            raise SourceReleaseError("NEEDS_ATTENTION: release state changed during final readback")
        return SourceReleaseOutcome(record, status, oid, release)

    def _publish(self, record: SourceReleaseRecord) -> str:
        existing = self._read_release(record)
        if existing is not None:
            return existing
        # The immutable tag already exists. A Release records its historical
        # association with the tag; it never starts or updates the worker.
        payload: dict[str, object] = {
            "tag_name": record.tag,
            "target_commitish": record.source_sha,
            "name": f"Accepted source release {record.operation_id}",
            "body": "\n".join(
                [
                    "## Immutable accepted Git source",
                    f"Source: `{record.source_sha}`",
                    f"Tested-source ref/OID: `{record.tested_source_ref}` / `{record.tested_source_oid}`",
                    f"Original CI run/attempt: `{record.ci_run_id}` / `{record.ci_run_attempt}`",
                    f"Beta-runtime acceptance ref/OID: `{record.acceptance_ref}` / `{record.acceptance_oid}`",
                    f"Local PASS evidence: `{record.local_evidence_id}` / `{record.local_evidence_content_sha256}`",
                    f"Source release catalog: `{record.tag}`; current source ref: `{record.release_ref}`",
                    "This publishes Git source selection/history only. The release worker is not deployed or declared healthy.",
                ]
            ),
            "draft": False,
            "prerelease": False,
        }
        try:
            self.client.request("POST", "releases", payload)
        except RuntimeError:
            pass
        found = self._read_release(record)
        if found is None:
            raise SourceReleaseError("NEEDS_ATTENTION: GitHub Release response/readback is uncertain")
        return found

    def _advance(self, record: SourceReleaseRecord) -> None:
        observed = self._pointer()
        if observed != record.expected_previous_sha:
            raise SourceReleaseError("SUPERSEDED: release pointer changed before first promotion effect")
        if observed == record.source_sha:
            return
        suffix = self.policy.release_ref.removeprefix("refs/")
        try:
            if observed is None:
                self.client.request("POST", "git/refs", {"ref": self.policy.release_ref, "sha": record.source_sha})
            else:
                # A ref PATCH has no compare-and-swap field. Non-fast-forward
                # selection is allowed only within the governed writer lane.
                self.client.request("PATCH", f"git/refs/{suffix}", {"sha": record.source_sha, "force": True})
        except RuntimeError:
            pass
        if self._pointer() != record.source_sha:
            raise SourceReleaseError("NEEDS_ATTENTION: release pointer write is unconfirmed or superseded")

    def _advance_selection(self, catalog_oid: str, previously_selected_oid: str | None) -> None:
        """Commit the operation identity after the source pointer is confirmed.

        The governed release-channel writer serializes updates. GitHub provides
        no cross-ref CAS: a surprising prestate or poststate fails closed.
        """
        if self._selection_oid() != previously_selected_oid:
            raise SourceReleaseError("SUPERSEDED: release operation selection changed before update")
        try:
            if previously_selected_oid is None:
                self.client.request("POST", "git/refs", {"ref": RELEASE_SELECTION_REF, "sha": catalog_oid})
            else:
                self.client.request("PATCH", f"git/refs/{RELEASE_SELECTION_REF.removeprefix('refs/')}", {"sha": catalog_oid, "force": True})
        except RuntimeError:
            pass
        if self._selection_oid() != catalog_oid:
            raise SourceReleaseError("NEEDS_ATTENTION: release operation selection is unconfirmed")

    def execute(self, request: SourceReleaseRequest) -> SourceReleaseOutcome:
        request.validate(self.policy)
        # Always inspect the original immutable operation before sampling any
        # mutable ref. A re-run never creates a fresh candidate/selection.
        existing = self.catalog.read(request.operation_id)
        self._dispatch(request)
        try:
            accepted = self.validator.validate(request)
        except (SourceAcceptanceError, TestedSourceError, BetaSourceError, SourceEvidenceError, ValueError, KeyError, TypeError) as exc:
            raise SourceReleaseError("original source/CI/acceptance evidence is invalid or unavailable") from exc
        if existing is not None:
            self._verify_original(existing.record, request, accepted)
            current = self._pointer()
            if current != existing.record.source_sha or self._selection_oid() != existing.tag_object_oid:
                raise SourceReleaseError("NEEDS_ATTENTION: existing catalog is not the selected promotion; no replayed ref write")
            self._publish(existing.record)
            return self._confirmed(existing.record, existing.tag_object_oid, "ALREADY_COMPLETED")

        if request.promotion_run_attempt != 1:
            raise SourceReleaseError("NEEDS_ATTENTION: retried Actions attempt has no original immutable operation record")
        previous = self._pointer()
        previous_operation = self._selection_oid()
        record = self._record(request, accepted, previous)
        prepared = self.catalog.prepare(record, require_new=True)
        # A different writer may have changed the current ref while the
        # catalog was created. No update is sent in that case.
        self._advance(record)
        self._advance_selection(prepared.tag_object_oid, previous_operation)
        if self.catalog.read(request.operation_id) != prepared:
            raise SourceReleaseError("NEEDS_ATTENTION: prepared immutable source catalog changed")
        self._publish(record)
        return self._confirmed(record, prepared.tag_object_oid, "CONFIRMED")
