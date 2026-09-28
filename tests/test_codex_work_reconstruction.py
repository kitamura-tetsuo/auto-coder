import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_cloud_client import CodexCloudClient
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_accounting import CodexWorkOperation, CodexWorkPhase
from auto_coder.codex_work_reconstruction import REQUIRED_CODEX_SOURCES, CodexSourceSnapshot, CodexWorkReconstructor, production_codex_reconstructor
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository


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


def test_production_followup_is_registered_before_wham_post(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    owner = ImplementationOwner("issue", 2334)
    slots = ImplementationSlotRepository(repository, 1)
    assert slots.reserve(owner)
    task = "task_e_6a26c19ac8a88326af83ebfb44b89fe2"
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
