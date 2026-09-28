from pathlib import Path

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_pr_attribution import CodexPrAttributionRepository
from auto_coder.codex_retirement_observer import CodexEvidenceState, collect_codex_retirement_observation
from auto_coder.codex_wham_client import WhamTask, WhamTurn
from auto_coder.codex_work_accounting import CodexWorkAccounting, CodexWorkPhase
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

OWNER = ImplementationOwner("issue", 2335)
REPOSITORY = "owner/repo"
TASK = "task_e_1234567890abcdef"


class WhamReader:
    def __init__(self, task: WhamTask, turns: list[WhamTurn]) -> None:
        self.task = task
        self.turns = turns

    def get_task(self, task_id: str) -> WhamTask:
        assert task_id == TASK
        return self.task

    def get_task_turns(self, task_id: str) -> list[WhamTurn]:
        assert task_id == TASK
        return self.turns


class GitHubReader:
    def __init__(self, prs: dict[int, dict[str, object]]) -> None:
        self.prs = prs

    def get_connected_prs(self, repository: str, issue: int, strict: bool = False) -> list[int]:
        assert (repository, issue, strict) == (REPOSITORY, 2335, True)
        return [11]

    def get_open_pull_requests_strict(self, repository: str) -> list[dict[str, object]]:
        assert repository == REPOSITORY
        return [self.prs[12]]

    def get_pull_request_metadata_strict(self, repository: str, number: int) -> dict[str, object]:
        assert repository == REPOSITORY
        return self.prs[number]


def _stores(tmp_path: Path) -> tuple[ImplementationSlotRepository, CloudRunRepository, str]:
    slots = ImplementationSlotRepository(REPOSITORY, 2, storage_path=tmp_path / "slots.json")
    assert slots.reserve(OWNER)
    incarnation = slots.owner_incarnation(OWNER)
    assert incarnation is not None
    runs = CloudRunRepository(REPOSITORY, storage_path=tmp_path / "runs.json")
    runs.save(
        CloudRun(
            REPOSITORY,
            2335,
            0,
            "codex-cloud",
            TASK,
            "codex-cloud",
            "environment-1",
            "main",
            pull_request_numbers=[10],
            publication_head_repository=REPOSITORY,
            publication_head_ref="issue-2335-attempt-0-codex-cloud",
        )
    )
    return slots, runs, incarnation


def _terminal_task() -> tuple[WhamTask, list[WhamTurn]]:
    baseline = WhamTurn(f"{TASK}~assttrn_baseline", "assistant", "completed")
    request = WhamTurn("user-turn", "user", "completed", raw_data={"request_id": "repair-request"})
    terminal = WhamTurn(f"{TASK}~assttrn_terminal", "assistant", "completed")
    task = WhamTask(
        TASK,
        turns=[baseline, request, terminal],
        current_user_turn=WhamTurn("user-turn", "user", "completed"),
        current_assistant_turn=terminal,
        latest_turn_status="completed",
        environment_id="environment-1",
    )
    return task, [baseline, request, terminal]


def test_collects_all_durable_and_discovered_prs_without_writes(tmp_path: Path) -> None:
    slots, runs, incarnation = _stores(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    assert slots.reserve(OWNER, implementation_pr=10)
    registration = accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="repair",
        kind="repair",
        source_request_id="repair-request",
        causal_baseline=f"{TASK}~assttrn_baseline",
        task_id=TASK,
    )
    snapshot = accounting.transition(
        OWNER,
        incarnation,
        "repair",
        CodexWorkPhase.SETTLED,
        evidence_id="settlement",
        evidence_causal_baseline=f"{TASK}~assttrn_baseline",
        evidence_source_request_id="repair-request",
        task_id=TASK,
        execution_complete=True,
        publication_complete=True,
        tracking_complete=True,
    )
    assert registration.created
    prs = {
        10: {"number": 10, "state": "closed"},
        11: {"number": 11, "state": "closed"},
        12: {
            "number": 12,
            "state": "open",
            "body": f"Closes #2335 https://chatgpt.com/codex/tasks/{TASK}",
            "head": {"ref": "issue-2335-attempt-0-codex-cloud"},
        },
    }
    task, turns = _terminal_task()
    attribution_path = tmp_path / "attributions.json"
    before_slots = (tmp_path / "slots.json").read_bytes()
    before_runs = (tmp_path / "runs.json").read_bytes()

    result = collect_codex_retirement_observation(
        REPOSITORY,
        OWNER,
        incarnation,
        slots,
        snapshot,
        runs,
        GitHubReader(prs),  # type: ignore[arg-type]
        wham=WhamReader(task, turns),  # type: ignore[arg-type]
        attributions=CodexPrAttributionRepository(REPOSITORY, attribution_path),
    )

    assert [item.number for item in result.implementation_prs] == [10, 11, 12]
    assert result.task_evidence[0].state is CodexEvidenceState.TERMINAL
    assert result.settlement_certificates[0].logical_operation_id == "repair"
    assert result.conclusive is False
    assert before_slots == (tmp_path / "slots.json").read_bytes()
    assert before_runs == (tmp_path / "runs.json").read_bytes()
    assert not attribution_path.exists()


def test_old_completion_cannot_settle_unmatched_new_admission(tmp_path: Path) -> None:
    slots, runs, incarnation = _stores(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    assert slots.reserve(OWNER, implementation_pr=10)
    snapshot = accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="new-repair",
        kind="repair",
        source_request_id="missing-request",
        causal_baseline=f"{TASK}~assttrn_baseline",
        task_id=TASK,
    ).snapshot
    task, turns = _terminal_task()
    prs = {
        10: {"number": 10, "state": "closed"},
        11: {"number": 11, "state": "closed"},
        12: {"number": 12, "state": "closed", "body": "unrelated"},
    }

    result = collect_codex_retirement_observation(
        REPOSITORY,
        OWNER,
        incarnation,
        slots,
        snapshot,
        runs,
        GitHubReader(prs),  # type: ignore[arg-type]
        wham=WhamReader(task, turns),  # type: ignore[arg-type]
        attributions=CodexPrAttributionRepository(REPOSITORY, tmp_path / "attributions.json"),
    )

    assert result.settlement_certificates == ()
    assert "operation new-repair is unresolved" in result.incomplete_reasons
    assert result.conclusive is False
