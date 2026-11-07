import hashlib
import json
from types import SimpleNamespace

import pytest

from auto_coder.management.beta_runtime_smoke import LocalResult, LocalSmokeEvidence, SmokeObservation, SmokeState
from auto_coder.management.mcp_runtime_tools import ManagementRuntime
from auto_coder.management.operation_executor import ManagementOperationDispatcher, OperationHandler
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, EffectState, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.management.source_acceptance import BetaAcceptanceHandler, SourceAcceptanceError, SourceAcceptancePublisher
from auto_coder.util.gh_cache import GitDataResponse
from tests.test_beta_runtime_smoke import _fixture

SHA = "a" * 40
OID = "b" * 40


class Git:
    def __init__(self, *, lose_ref_response: bool = False) -> None:
        self.tag = None
        self.ref = None
        self.lose = lose_ref_response

    def request(self, method, path, payload=None):
        if method == "POST" and path == "git/tags":
            self.tag = {**payload, "sha": OID}
            return GitDataResponse(201, {"sha": OID})
        if method == "POST" and path == "git/refs":
            self.ref = payload
            if self.lose:
                raise RuntimeError("lost response")
            return GitDataResponse(201, payload)
        if path.startswith("git/ref/"):
            if self.ref is None:
                return GitDataResponse(404)
            return GitDataResponse(200, {"ref": self.ref["ref"], "object": {"type": "tag", "sha": self.ref["sha"]}})
        if path == f"git/tags/{OID}" and self.tag is not None:
            return GitDataResponse(
                200,
                {
                    **self.tag,
                    "object": {"type": self.tag["type"], "sha": self.tag["object"]},
                },
            )
        return GitDataResponse(404)


def evidence(result="PASS", *, operation_id="accept-1", source_sha=SHA, runtime_id="runtime-a"):
    content = json.dumps(
        {
            "schema_version": 1,
            "record_type": "beta-runtime-smoke",
            "operation_id": operation_id,
            "repository": "owner/repo",
            "source_sha": source_sha,
            "local_evidence_id": f"beta-runtime-smoke-v1:{operation_id}",
            "local_result": result,
            "capture": {
                "source": {"repository": "owner/repo", "source_sha": source_sha},
                "installed_runtime_id": runtime_id,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return LocalSmokeEvidence(f"beta-runtime-smoke-v1:{operation_id}", hashlib.sha256(content.encode()).hexdigest(), content)


@pytest.mark.parametrize("lost", [False, True])
def test_exact_create_only_tag_readback_derives_publication(lost):
    git = Git(lose_ref_response=lost)
    publisher = SourceAcceptancePublisher(git, "owner/repo")
    result = publisher.publish(evidence(), "2026-01-01T00:00:00Z")
    assert result.direct_commit_sha == SHA
    assert result.tag_object_oid == OID
    assert result == publisher.publish(evidence(), "2026-01-01T00:00:00Z")
    assert "PASS_PUBLISHED" not in result.annotation


def test_non_pass_local_result_is_never_published():
    git = Git()
    with pytest.raises(SourceAcceptanceError, match="exact PASS"):
        SourceAcceptancePublisher(git, "owner/repo").publish(evidence("BLOCKED"), "2026-01-01T00:00:00Z")
    assert git.tag is None


def test_existing_mismatched_tag_is_a_collision():
    git = Git()
    publisher = SourceAcceptancePublisher(git, "owner/repo")
    publisher.publish(evidence(), "2026-01-01T00:00:00Z")
    git.tag["message"] = git.tag["message"].replace(SHA, "c" * 40)
    with pytest.raises(SourceAcceptanceError, match="differs|conflicts"):
        publisher.publish(evidence(), "2026-01-01T00:00:00Z")


def test_readback_rejects_annotation_with_substituted_local_evidence():
    git = Git()
    publisher = SourceAcceptancePublisher(git, "owner/repo")
    publisher.publish(evidence(), "2026-01-01T00:00:00Z")
    annotation = json.loads(git.tag["message"])
    annotation["local_evidence"]["capture"]["source"]["source_sha"] = "c" * 40
    git.tag["message"] = json.dumps(annotation, sort_keys=True, separators=(",", ":"))
    with pytest.raises(SourceAcceptanceError, match="digest differs"):
        publisher.read("accept-1")


def test_nonpass_local_result_is_durably_rejected_before_publication(tmp_path):
    journal = ManagementOperationStore(tmp_path)
    authority = AcceptedAuthority("issuer", "resource", "project", "operator", "client", "grant", ("auto-coder.read", "auto-coder.deploy"), 1, 2)
    admitted = journal.admit(OperationIntent(ManagedTarget("beta", "beta", "worker/repo"), "run_beta_acceptance", (), "key"), authority)
    assert admitted.record is not None
    blocked = evidence("BLOCKED")

    class Producer:
        def __init__(self):
            self.journal = journal

        def advance(self, operation_id):
            return SmokeObservation(SmokeState.COMPLETE, operation_id, "installed_candidate_mismatch", blocked)

    settled = BetaAcceptanceHandler(object(), Producer(), object()).forward(admitted.record)
    assert settled.phase is OperationPhase.FAILED
    assert settled.effect.state is EffectState.REJECTED
    assert settled.effect.receipt is not None
    assert json.loads(settled.effect.receipt.safe_result) == {
        "local_evidence_id": blocked.local_evidence_id,
        "local_result": LocalResult.BLOCKED.value,
        "reason": "installed_candidate_mismatch",
    }


@pytest.mark.parametrize(
    "published_source,published_runtime,settles",
    [(SHA, "runtime-a", True), ("c" * 40, "runtime-a", False), (SHA, "runtime-b", False)],
)
def test_reconcile_confirms_only_the_original_durable_local_evidence(tmp_path, published_source, published_runtime, settles):
    journal = ManagementOperationStore(tmp_path)
    authority = AcceptedAuthority(
        "issuer",
        "resource",
        "project",
        "operator",
        "client",
        "grant",
        ("auto-coder.read", "auto-coder.deploy"),
        1,
        2,
    )
    admitted = journal.admit(
        OperationIntent(
            ManagedTarget("beta", "beta", "owner/repo"),
            "run_beta_acceptance",
            (),
            "key",
        ),
        authority,
    )
    assert admitted.record is not None
    operation_id = admitted.record.operation_id
    original = evidence(operation_id=operation_id)
    begun = journal.begin_effect(
        operation_id,
        admitted.record.revision,
        owner_attempt_id=f"publish-{operation_id}",
        attempt_id=f"publish-{operation_id}",
        correlation_identity=original.content_sha256,
    )
    assert begun.record is not None
    held = journal.transition(
        operation_id,
        begun.record.revision,
        OperationPhase.NEEDS_ATTENTION,
        safe_error_category="UNKNOWN:source_acceptance_publication",
    )
    assert held.record is not None

    git = Git()
    publisher = SourceAcceptancePublisher(git, "owner/repo")
    publisher.publish(
        evidence(
            operation_id=operation_id,
            source_sha=published_source,
            runtime_id=published_runtime,
        ),
        "2026-01-01T00:00:00Z",
    )

    class Producer:
        def __init__(self):
            self.journal = journal

        def read(self, requested_operation_id):
            assert requested_operation_id == operation_id
            return SmokeObservation(
                SmokeState.COMPLETE,
                operation_id,
                "pass",
                original,
            )

    reconstructed = BetaAcceptanceHandler(object(), Producer(), publisher)
    result = reconstructed.reconcile(held.record)
    assert (result.phase is OperationPhase.SUCCEEDED) is settles
    assert (result.effect.state is EffectState.CONFIRMED) is settles


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", [True, False])
async def test_service_monitor_continues_original_sampling_window_after_reconstruction(tmp_path, monkeypatch, progress):
    """Component regression: real producer/store/dispatcher, explicit worker double."""
    smoke = _fixture(tmp_path)
    publisher = SourceAcceptancePublisher(Git(), "owner/repo")

    def runtime():
        journal = ManagementOperationStore(tmp_path / "journal")
        handler = BetaAcceptanceHandler(None, smoke.producer(journal=journal), publisher)
        monkeypatch.setattr(handler, "_authority_refusal", lambda record: None)
        dispatcher = ManagementOperationDispatcher(journal)
        dispatcher.register("run_beta_acceptance", OperationHandler(handler.forward, handler.reconcile))
        return SimpleNamespace(journal=journal, dispatcher=dispatcher, beta_acceptance=handler)

    first = runtime()
    await first.dispatcher.start()
    await first.dispatcher.drain()
    original = smoke.snapshot()
    assert len(original["samples"]) == 1
    await first.dispatcher.stop()
    rebuilt = runtime()
    await rebuilt.dispatcher.start()
    try:
        await rebuilt.dispatcher.drain()
        # Frequent service sweeps cannot sample before the persisted interval.
        for _ in range(3):
            await ManagementRuntime._poll_lifecycle_once(rebuilt)
        assert smoke.snapshot() == original
        if progress:
            smoke.next_heartbeat()
        else:
            smoke.clock.now = original["window"]["deadline"]
        await ManagementRuntime._poll_lifecycle_once(rebuilt)
        final = smoke.snapshot()
        assert final["window"] == original["window"]
        assert final["capture"] == original["capture"]
        assert final["binding"] == original["binding"]
        record = rebuilt.journal.get(smoke.operation.operation_id).record
        assert record.phase is (OperationPhase.SUCCEEDED if progress else OperationPhase.FAILED)
        evidence = rebuilt.beta_acceptance.producer.read(record.operation_id).evidence
        assert evidence.local_result.value == ("PASS" if progress else "BLOCKED")
        assert (publisher.read(record.operation_id) is not None) is progress
    finally:
        await rebuilt.dispatcher.stop()
