"""Original accepted-release evidence for the configured worker lifecycle.

The source commit ref and independent operation-selection tag must both match.
These historical facts alone never authorize a worker mutation.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from auto_coder.source_release_catalog import CatalogReadback, SourceReleaseError, SourceReleasePolicy, SourceReleaseRequest, _sha, canonical, release_tag
from auto_coder.source_release_promotion import AcceptedSourceValidator, SourceReleasePromotion, ValidatedAcceptance
from auto_coder.tested_source_refs import TestedSourcePublisher
from auto_coder.util.gh_cache import GitHubGitDataClient

from .operation_store import ManagementOperationStore
from .operations import EffectState, ManagedTarget, OperationPhase, ReadStatus
from .source_release_dispatch import ACTION, SourceReleaseDispatch


@dataclass(frozen=True)
class ReleaseWorkerEvidenceIdentity:
    """Exact caller intent, with no repository, endpoint or runtime arguments."""

    promotion_operation_id: str
    source_sha: str
    catalog_ref: str
    catalog_oid: str
    acceptance_ref: str
    acceptance_oid: str

    def validate(self) -> None:
        expected_ref = f"refs/tags/{release_tag(self.promotion_operation_id)}"
        if self.catalog_ref != expected_ref:
            raise SourceReleaseError("release catalog ref does not name the original promotion")
        for value in (self.source_sha, self.catalog_oid, self.acceptance_oid):
            _sha(value, "release deployment evidence identity")


@dataclass(frozen=True)
class ReleaseWorkerPromotionFacts:
    """Historical provenance and source readback, never deployment authority."""

    identity: ReleaseWorkerEvidenceIdentity
    catalog: CatalogReadback
    acceptance: ValidatedAcceptance
    policy_revision: str
    promotion_intent_fingerprint: str
    current_source_sha: str
    lock_sha256: str
    distribution_version: str
    observed_at: float
    release_operation_selection_proven: bool = field(default=True, init=False)
    deployment_authorized: bool = field(default=False, init=False)
    worker_deployed: bool = field(default=False, init=False)


class ReleaseWorkerEvidenceReader:
    """Join exact existing producer records without staging or journal mutation.

    All dependencies and target/workflow identities come from trusted controller
    configuration. The authenticated lifecycle consumer independently enforces
    its original grant, full policy, instance lane and pre-effect revalidation.
    This reader deliberately supplies no deployment-authorization Boolean.
    """

    def __init__(self, journal: ManagementOperationStore, client: GitHubGitDataClient, policy: SourceReleasePolicy, *, promotion_target_id: str, workflow_id: int, clock: Callable[[], float] = time.time) -> None:
        policy.validate()
        if not promotion_target_id or type(workflow_id) is not int or workflow_id <= 0:
            raise SourceReleaseError("trusted promotion target/workflow is unavailable")
        self.journal, self.client, self.policy = journal, client, policy
        self.promotion_target_id, self.workflow_id, self.clock = promotion_target_id, workflow_id, clock

    def read(self, identity: ReleaseWorkerEvidenceIdentity) -> ReleaseWorkerPromotionFacts:
        """Refuse missing, stale, unpublished or mismatched original evidence."""
        try:
            return self._read(identity)
        except SourceReleaseError:
            raise
        except (RuntimeError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            raise SourceReleaseError("exact release promotion evidence is unavailable") from exc

    def _read(self, identity: ReleaseWorkerEvidenceIdentity) -> ReleaseWorkerPromotionFacts:
        identity.validate()
        observed_at = self.clock()
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at) or observed_at <= 0:
            raise SourceReleaseError("release evidence observation time is invalid")
        lookup = self.journal.get(identity.promotion_operation_id)
        record = lookup.record
        if lookup.status is not ReadStatus.FOUND or record is None or record.phase is not OperationPhase.SUCCEEDED or record.effect.state is not EffectState.CONFIRMED:
            raise SourceReleaseError("original source promotion is not confirmed")
        if record.intent.action != ACTION or record.intent.target != ManagedTarget("accepted-source-release", "release", self.policy.source_repository) or record.intent.fingerprint() != record.intent_fingerprint:
            raise SourceReleaseError("original source promotion intent does not match trusted configuration")
        params = {item.name: item.value for item in record.intent.normalized_parameters()}
        if params.get("target_id") != self.promotion_target_id or params.get("workflow_id") != self.workflow_id or params.get("release_policy_revision") != self.policy.revision or record.intent.expected_incarnation != self.policy.release_ref or record.intent.expected_artifact != identity.source_sha:
            raise SourceReleaseError("original promotion target, policy or source differs")
        saved = self.journal.lifecycle_snapshot(record.operation_id)
        bound = self.journal.source_release_run(record.operation_id)
        if saved is None or bound is None:
            raise SourceReleaseError("original promotion snapshot/run is unavailable")
        snapshot = saved[1]
        if snapshot.get("schema_version") != 1 or snapshot.get("kind") != ACTION or snapshot.get("workflow_id") != self.workflow_id or SourceReleasePolicy.from_document(snapshot.get("policy")) != self.policy:
            raise SourceReleaseError("original promotion snapshot differs from trusted configuration")
        run_id, run_attempt = bound.get("id"), bound.get("run_attempt")
        if type(run_id) is not int or type(run_attempt) is not int:
            raise SourceReleaseError("original promotion run/attempt is malformed")
        candidate = SourceReleaseDispatch.request(self.policy, params, record.operation_id)
        request = SourceReleaseRequest(**asdict(candidate), promotion_run_id=run_id, promotion_run_attempt=run_attempt)
        request.validate(self.policy)
        if (request.source_sha, request.acceptance_ref, request.acceptance_oid) != (identity.source_sha, identity.acceptance_ref, identity.acceptance_oid):
            raise SourceReleaseError("exact accepted release identities differ from original intent")
        receipt = record.effect.receipt
        if receipt is None or receipt.observation_source != "github-source-release-readback" or receipt.correlation_identity != record.operation_id or record.effect.correlation_identity != record.operation_id:
            raise SourceReleaseError("original promotion receipt is unavailable or uncorrelated")
        expected_receipt = {"outcome": "SOURCE_PROMOTION_CONFIRMED", "run_id": request.promotion_run_id, "run_attempt": request.promotion_run_attempt, "catalog_oid": identity.catalog_oid, "source_sha": identity.source_sha, "worker_deployed": False}
        if receipt.safe_result != canonical(expected_receipt):
            raise SourceReleaseError("original promotion receipt does not confirm the exact release")
        response = self.client.request("GET", f"actions/runs/{request.promotion_run_id}/attempts/{request.promotion_run_attempt}")
        run = response.data
        if response.status != 200 or not isinstance(run, dict) or SourceReleaseDispatch._run(run, self.policy, self.workflow_id, record.operation_id) != bound or run.get("status") != "completed" or run.get("conclusion") != "success":
            raise SourceReleaseError("original promotion Actions attempt is not successful")
        # New worker admission must not reuse the historical dispatch clock to
        # make expired acceptance eligible for a new runtime mutation.
        accepted = AcceptedSourceValidator(self.client, self.policy, clock=lambda: observed_at).validate(request)
        if asdict(accepted) != snapshot.get("accepted"):
            raise SourceReleaseError("original immutable acceptance linkage differs")
        promotion = SourceReleasePromotion(self.client, self.policy)
        catalog = promotion.catalog.read(record.operation_id)
        if catalog is None or catalog.tag_object_oid != identity.catalog_oid:
            raise SourceReleaseError("exact immutable release catalog is unavailable")
        promotion._verify_original(catalog.record, request, accepted)
        # Use the existing publication producer's repeated independent readback,
        # never execute(), which can publish or change the release source ref.
        promotion._confirmed(catalog.record, identity.catalog_oid, "CONFIRMED")
        tested = TestedSourcePublisher(self.client, self.policy.source_repository).read(identity.source_sha)
        if tested is None or tested.tag_ref != request.tested_source_ref or tested.tag_object_oid != request.tested_source_oid or (tested.record.run_id, tested.record.run_attempt) != (request.ci_run_id, request.ci_run_attempt):
            raise SourceReleaseError("exact tested source runtime metadata is unavailable")
        if self.journal.get(record.operation_id).record != record or self.journal.lifecycle_snapshot(record.operation_id) != saved or self.journal.source_release_run(record.operation_id) != bound:
            raise SourceReleaseError("original promotion evidence changed during observation")
        return ReleaseWorkerPromotionFacts(identity, catalog, accepted, self.policy.revision, record.intent_fingerprint, identity.source_sha, tested.record.lock_sha256, tested.record.distribution_version, observed_at)
