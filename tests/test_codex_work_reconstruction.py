import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_cloud_client import CodexCloudClient
from auto_coder.codex_pr_recovery import CodexPRRecoveryStore, RecoveryOutcome
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_accounting import CodexWorkAccounting, CodexWorkOperation, CodexWorkPhase, WorkAccountingStatus
from auto_coder.codex_work_reconstruction import REQUIRED_CODEX_SOURCES, CodexSourceSnapshot, CodexWorkReconstructor, production_codex_reconstructor, reconstruct_active_codex_work
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_stage_routing import ImplementationRetryRequest
from auto_coder.retry_dispatch import RetryDispatchRepository


def _operation(operation_id: str) -> CodexWorkOperation:
    return CodexWorkOperation(operation_id, "submission", "attempt-0", None, None, CodexWorkPhase.RESERVED, False, False, False, None)


def test_reconstruction_requires_every_source_and_preserves_reserved_work() -> None:
    readers = {source: (lambda source=source: CodexSourceSnapshot(source, f"{source}:v1")) for source in REQUIRED_CODEX_SOURCES}
    readers["cloud-runs"] = lambda: CodexSourceSnapshot("cloud-runs", "cloud-runs:v1", (_operation("pre-send-reservation"),))

    receipt = CodexWorkReconstructor(readers).reconstruct()

    assert receipt.complete is True
    assert receipt.operations == (_operation("pre-send-reservation"),)
    assert dict(receipt.source_consistency_ids)["cloud-runs"] == "cloud-runs:v1"


def test_reconstruction_refuses_source_race_and_duplicate_operation() -> None:
    calls = 0

    def racing() -> CodexSourceSnapshot:
        nonlocal calls
        calls += 1
        return CodexSourceSnapshot("cloud-runs", f"revision-{calls}")

    readers = {source: (lambda source=source: CodexSourceSnapshot(source, "v1")) for source in REQUIRED_CODEX_SOURCES}
    readers["cloud-runs"] = racing
    with pytest.raises(RuntimeError, match="changed"):
        CodexWorkReconstructor(readers).reconstruct()

    readers = {source: (lambda source=source: CodexSourceSnapshot(source, "v1", (_operation("same"),))) for source in REQUIRED_CODEX_SOURCES}
    with pytest.raises(ValueError, match="conflicting"):
        CodexWorkReconstructor(readers).reconstruct()


def test_production_reconstruction_preserves_pre_send_run_and_detects_mutation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    run = CloudRun(repository, 2334, 0, "codex-cloud", submission_outcome="indeterminate", launch_identity="attempt-0")
    runs = CloudRunRepository(repository)
    runs.save(run)

    reconstructor = production_codex_reconstructor(repository, 2334)
    receipt = reconstructor.reconstruct()

    assert receipt.complete is True
    submission = next(operation for operation in receipt.operations if operation.kind == "submission")
    assert submission.task_id is None
    assert submission.phase is CodexWorkPhase.DELIVERY_UNKNOWN
    previous = dict(receipt.source_consistency_ids)
    run.task_id = "task_e_production"
    run.submission_outcome = "accepted"
    runs.update_claim(run)
    assert dict(reconstructor.consistency_ids()) != previous


def test_production_reconstruction_recovers_publication_and_tracking_completion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    run = CloudRun(repository, 2334, 0, "codex-cloud", "task_e_terminal", "codex-cloud", "env", "main", launch_identity="attempt-0")
    CloudRunRepository(repository).save(run)
    recovery = CodexPRRecoveryStore()
    assert recovery.save_observation(run, RecoveryOutcome.PR_OBSERVED, completion_turn="terminal-turn", pr_number=92)
    assert recovery.mark_handoff(run)

    receipt = production_codex_reconstructor(repository, 2334).reconstruct()

    submission = next(operation for operation in receipt.operations if operation.kind == "submission")
    assert submission.publication_complete is True
    assert submission.tracking_complete is True
    publication = next(operation for operation in receipt.operations if operation.kind == "publication")
    assert publication.publication_complete is True
    assert publication.tracking_complete is True


@pytest.mark.parametrize("provider_session", (None, "jules-session"))
def test_startup_reconstruction_does_not_add_codex_accounting_to_non_codex_owner(tmp_path, monkeypatch, provider_session) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    owner = ImplementationOwner("issue", 2334)
    slots = ImplementationSlotRepository(repository, 1)
    assert slots.reserve(owner, implementation_pr=91)
    if provider_session is not None:
        assert slots.record_provider_session(owner, provider_session)

    reconstruct_active_codex_work(repository, slots)

    with slots._state_lock():
        record = slots._read()[owner.key]
    assert "codex_work_accounting" not in record


@pytest.mark.parametrize("retry", (False, True))
def test_production_followup_is_registered_before_wham_post(tmp_path, monkeypatch, retry) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    owner = ImplementationOwner("issue", 2334)
    slots = ImplementationSlotRepository(repository, 1)
    assert slots.reserve(owner)
    task = "task_e_6a26c19ac8a88326af83ebfb44b89fe2"
    if retry:
        dispatch = RetryDispatchRepository(repository)
        authority = ImplementationRetryRequest(request_id="retry-request", repository=repository, target_number=2334, generation="generation-1", attempt_id="attempt-0", status="owned", ownership_reference="execution-1")
        _, may_create = dispatch.claim(authority, "codex-cloud", "codex-cloud", {})
        assert may_create is True
        reconstruct_active_codex_work(repository, slots)
        incarnation = slots.owner_incarnation(owner)
        assert incarnation is not None
        before = CodexWorkAccounting(slots).snapshot(owner, incarnation)
        assert len(before.operations) == 1
        assert before.operations[0].task_id is None
        assert before.operations[0].phase is CodexWorkPhase.RESERVED
        dispatch.record_outcome("retry-request", "accepted", external_id=task)
    CloudRunRepository(repository).save(CloudRun(repository, 2334, 0, "codex-cloud", task, "codex-cloud", "env", "main", launch_identity="attempt-0"))

    class Wham:
        def resolve_latest_assistant_turn(self, task_id):
            assert task_id == task
            return f"{task}~asst_1"

        def send_follow_up(self, task_id, turn_id, prompt):
            incarnation = slots.owner_incarnation(owner)
            assert incarnation is not None
            snapshot = production_codex_reconstructor(repository, 2334).reconstruct()
            assert any(operation.kind == "follow-up" and operation.causal_baseline == turn_id for operation in snapshot.operations)
            with slots._state_lock():
                accounting = slots._read()[owner.key]["codex_work_accounting"]
            assert any(value["kind"] == "follow-up" for value in accounting["operations"].values())
            return FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 202)

    client = CodexCloudClient.__new__(CodexCloudClient)
    client.repo_name = repository
    client.wham_client = Wham()
    client.active_tasks = {}

    assert client.send_followup(task, "Repair the pull request", ("repair-generation-1",)) is True
    if retry:
        # A fresh process sees the accepted binding without losing obligations.
        restarted_slots = ImplementationSlotRepository(repository, 1)
        reconstruct_active_codex_work(repository, restarted_slots)
        reconstructor = production_codex_reconstructor(repository, 2334)
        snapshot = CodexWorkAccounting(restarted_slots, reconstructor.consistency_ids).snapshot(owner, incarnation)
        assert snapshot.accounting_status is WorkAccountingStatus.COMPLETE
        retry_operation = next(operation for operation in snapshot.operations if operation.kind == "retry")
        assert retry_operation.task_id == task
        assert retry_operation.phase is CodexWorkPhase.ACCEPTED
        assert retry_operation.accepted is True
        assert snapshot.releasable is False
