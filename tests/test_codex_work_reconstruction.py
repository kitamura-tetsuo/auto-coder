import pytest

from auto_coder.codex_work_accounting import CodexWorkOperation, CodexWorkPhase
from auto_coder.codex_work_reconstruction import REQUIRED_CODEX_SOURCES, CodexSourceSnapshot, CodexWorkReconstructor


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
