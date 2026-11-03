"""Issue2725 production sender/journal/reclamation over native-shaped HTTP."""

import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_cloud_client import CodexCloudClient
from auto_coder.codex_pr_recovery import CodexPRRecoveryStore, RecoveryOutcome
from auto_coder.codex_retirement_observer import collect_codex_retirement_observation
from auto_coder.codex_wham_client import CodexWhamClient
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.codex_work_reconstruction import production_codex_reconstructor, reconstruct_active_codex_work
from auto_coder.implementation_reclamation_scheduler import ReclamationObligationStore, run_due_reclamation_checks, schedule_reevaluation
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.pr_processor import _send_followup_with_quota_admission
from auto_coder.pr_repair import ExistingPrRepairTarget, build_existing_pr_repair_prompt
from auto_coder.public_api import project_slots

REPOSITORY = "owner/repo"
TASK = "task_e_1234567890abcdef1234567890abcdef"
BASELINE = TASK + "~assttrn_baseline"
REQUEST = TASK + "~usertrn_followup"
RESPONSE = TASK + "~assttrn_followup"


def native_turn(identity, role, parent, *, text=None):
    inner = {"id": identity, "role": role, "turn_status": "completed", "previous_turn_id": parent}
    if text is not None:
        inner["input_items"] = [{"type": "message", "role": "user", "content": [{"content_type": "text", "text": text}]}]
    return {"id": identity, "turn": inner, "parent": parent}


class NativeProvider:
    def __init__(self):
        self.turns = [native_turn(TASK + "~usertrn_initial", "user", None, text="Initial implementation"), native_turn(BASELINE, "assistant", TASK + "~usertrn_initial")]
        self.posts = []
        self.incomplete = False
        self.on_read = None

    def get(self, url, **_kwargs):
        if self.on_read is not None:
            callback, self.on_read = self.on_read, None
            callback()
        if str(url).endswith("/turns"):
            payload = {"turn_mapping": {turn["id"]: turn for turn in self.turns}}
            if self.incomplete:
                payload["has_more"] = True
        else:
            payload = {
                "task": {"id": TASK, "environment_id": "environment-1"},
                "task_status_display": {"latest_turn_status_display": {"turn_status": "completed"}},
                "current_user_turn": self.turns[-2],
                "current_assistant_turn": self.turns[-1],
            }
        return httpx.Response(200, json=deepcopy(payload), request=httpx.Request("GET", str(url)))

    def post(self, url, *, json, **_kwargs):
        assert json["follow_up"]["task_id"] == TASK
        assert json["follow_up"]["turn_id"] == BASELINE
        assert "logical-followup-2725" not in repr(json)
        self.posts.append(deepcopy(json))
        text = json["input_items"][0]["content"][0]["text"]
        self.turns.extend([native_turn(REQUEST, "user", BASELINE, text=text), native_turn(RESPONSE, "assistant", REQUEST)])
        return httpx.Response(202, json={}, request=httpx.Request("POST", str(url)))


class TerminalGitHub:
    def __init__(self, *, open_pr=False):
        self.open_pr = open_pr

    def get_connected_prs(self, repository, number, strict=False):
        assert (repository, number, strict) == (REPOSITORY, 2725, True)
        return [91]

    def get_open_pull_requests_strict(self, repository):
        assert repository == REPOSITORY
        return []

    def get_pull_request_metadata_strict(self, repository, number):
        assert (repository, number) == (REPOSITORY, 91)
        return {"number": 91, "state": "open" if self.open_pr else "closed", "merged": not self.open_pr}


def actual_followup(tmp_path, monkeypatch, *, handoff=True, bounded_repair=True):
    monkeypatch.setenv("HOME", str(tmp_path))
    slots = ImplementationSlotRepository(REPOSITORY, 1)
    owner = ImplementationOwner("issue", 2725)
    execution = slots.start_execution(owner)
    assert execution is not None
    assert slots.record_implementation_pr(owner, 91)
    assert slots.record_provider_session(owner, TASK)
    slots.finish_execution(owner, execution)
    assert slots.snapshot().owners[0].executions == ()
    incarnation = slots.owner_incarnation(owner)
    run = CloudRun(REPOSITORY, owner.number, 0, "codex-cloud", TASK, "codex-cloud", "environment-1", "main", launch_identity="initial-attempt", pull_request_numbers=[91])
    runs = CloudRunRepository(REPOSITORY)
    runs.save(run)
    recovery = CodexPRRecoveryStore()
    assert recovery.save_observation(run, RecoveryOutcome.PR_OBSERVED, pr_number=91)
    if handoff:
        assert recovery.mark_handoff(run)
    reconstruct_active_codex_work(REPOSITORY, slots)
    provider = NativeProvider()
    monkeypatch.setattr(httpx, "get", provider.get)
    monkeypatch.setattr(httpx, "post", provider.post)
    wham = CodexWhamClient(base_url="https://wham.example")
    monkeypatch.setattr(wham, "_get_headers", lambda: {"Authorization": "Bearer fixture-only"})
    client = CodexCloudClient.__new__(CodexCloudClient)
    client.repo_name = REPOSITORY
    client.wham_client = wham
    client.active_tasks = {}
    if bounded_repair:
        target = ExistingPrRepairTarget(REPOSITORY, 91, "fix/2725", "main", "a" * 40)
        prompt = build_existing_pr_repair_prompt(target, "Apply the bounded correction to this PR.")
        assert _send_followup_with_quota_admission(client, REPOSITORY, TASK, prompt, ("logical-followup-2725",), repair_target=target)
    else:
        assert client.send_followup(TASK, "Publish a replacement PR for the previous output.", ("logical-followup-2725",))
    assert len(provider.posts) == 1
    return slots, owner, incarnation, runs, recovery, run, provider, wham


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("late_handoff", [False, True])
def test_real_followup_reconstructs_and_reclaims_on_first_due_check(tmp_path, monkeypatch, restart, late_handoff):
    slots, owner, incarnation, runs, recovery, run, provider, wham = actual_followup(tmp_path, monkeypatch, handoff=not late_handoff)
    if late_handoff:
        reconstruct_active_codex_work(REPOSITORY, slots)
        assert recovery.mark_handoff(run)
    if restart:
        slots = ImplementationSlotRepository(REPOSITORY, 1)
        runs = CloudRunRepository(REPOSITORY)
    store = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(owner, slots, store, due_at=100)
    freed = []

    released = run_due_reclamation_checks(slots, store, github_client=TerminalGitHub(), cloud_run_store=runs, codex_wham_client=wham, on_capacity_freed=lambda: freed.append(True), now=100)

    assert released == 1
    assert freed == [True]
    assert len(provider.posts) == 1
    assert slots.owner_incarnation(owner) is None
    retired = slots._read_retired()[incarnation]
    assert retired["implementation_prs"] == [91]
    assert retired["provider_sessions"] == [TASK]
    followups = [operation for operation in retired["codex_work_accounting"]["operations"].values() if operation["kind"] == "follow-up"]
    assert len(followups) == 1
    assert followups[0]["phase"] == "settled"
    assert followups[0]["settlement_evidence_id"] == "codex-retirement:" + RESPONSE + "|user:" + REQUEST
    projection = project_slots(slots.snapshot(), 500)
    assert projection.availability == "available"
    assert (projection.normal_usage, projection.normal_available, projection.owners) == (0, 1, [])
    assert slots.start_execution(ImplementationOwner("issue", 2726)) is not None


@pytest.mark.parametrize(
    "gap", ["unmatched", "duplicate", "later-duplicate", "interleaved", "missing-baseline", "missing-history", "wrong-parent", "unrelated-response", "publication", "unbound-publication", "open-pr", "competing-ambiguous", "cycle", "missing-ancestor", "wrong-turn-identity", "source-race"]
)
def test_real_followup_uncertainty_retains_capacity_without_replay(tmp_path, monkeypatch, gap):
    slots, owner, incarnation, runs, _recovery, run, provider, wham = actual_followup(tmp_path, monkeypatch, handoff=gap != "publication", bounded_repair=gap != "unbound-publication")
    if gap == "unmatched":
        provider.turns[-2]["turn"]["input_items"][0]["content"][0]["text"] += "Different request"
    elif gap in {"duplicate", "interleaved"}:
        text = provider.turns[-2]["turn"]["input_items"][0]["content"][0]["text"] if gap == "duplicate" else "An unrelated manual request"
        provider.turns.insert(-1, native_turn(TASK + "~usertrn_competing", "user", BASELINE, text=text))
    elif gap == "later-duplicate":
        provider.turns.extend([native_turn(TASK + "~usertrn_later", "user", RESPONSE, text=provider.turns[-2]["turn"]["input_items"][0]["content"][0]["text"]), native_turn(TASK + "~assttrn_later", "assistant", TASK + "~usertrn_later")])
    elif gap == "missing-baseline":
        provider.turns = [turn for turn in provider.turns if turn["id"] != BASELINE]
    elif gap == "missing-history":
        provider.incomplete = True
    elif gap == "wrong-parent":
        provider.turns[-2]["parent"] = TASK + "~assttrn_other"
    elif gap == "unrelated-response":
        provider.turns[-1]["parent"] = TASK + "~usertrn_initial"
    elif gap == "competing-ambiguous":
        competing = native_turn(TASK + "~usertrn_competing", "user", BASELINE, text="Competing ambiguous input")
        competing["turn"]["previous_turn_id"] = TASK + "~assttrn_other"
        provider.turns.insert(-1, competing)
    elif gap == "cycle":
        provider.turns[1]["parent"] = RESPONSE
        provider.turns[1]["turn"]["previous_turn_id"] = RESPONSE
    elif gap == "missing-ancestor":
        provider.turns.pop(0)
    elif gap == "wrong-turn-identity":
        provider.turns[-2]["id"] = TASK + "~usertrn_wrong_outer_identity"
    elif gap == "source-race":

        def change_source_after_capture():
            run.pull_request_numbers.append(92)
            runs.update_claim(run)

        provider.on_read = change_source_after_capture
    store = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(owner, slots, store, due_at=100)

    released = run_due_reclamation_checks(slots, store, github_client=TerminalGitHub(open_pr=gap == "open-pr"), cloud_run_store=runs, codex_wham_client=wham, now=100)

    assert released == 0
    assert slots.owner_incarnation(owner) == incarnation
    assert slots.available_normal_slots() == 0
    assert len(provider.posts) == 1
    assert store.all()[0].next_due_at == 160
    assert not slots._read_retired()
    projection = project_slots(slots.snapshot(), 500)
    assert (projection.normal_usage, projection.normal_available) == (1, 0)
    assert [entry.owner_number for entry in projection.owners] == [2725]


def test_actual_followup_observation_does_not_settle_or_mutate_sources(tmp_path, monkeypatch):
    slots, owner, incarnation, runs, recovery, _run, provider, wham = actual_followup(tmp_path, monkeypatch)
    reconstructor = production_codex_reconstructor(REPOSITORY, owner.number)
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    accounting.reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())
    snapshot = accounting.snapshot(owner, incarnation)

    def retained_files():
        result = {}
        for path in Path(tmp_path).rglob("*"):
            if not path.is_file() or ".sqlite3" in path.name:
                continue
            result[str(path.relative_to(tmp_path))] = path.read_bytes()
        for path in Path(tmp_path).rglob("*.sqlite3"):
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
                result[str(path.relative_to(tmp_path))] = "\n".join(connection.iterdump()).encode()
        return result

    before = retained_files()
    observation = collect_codex_retirement_observation(REPOSITORY, owner, incarnation, slots, snapshot, runs, TerminalGitHub(), wham=wham, recovery=recovery)

    assert observation.conclusive
    assert observation.settlement_certificates
    assert retained_files() == before
    assert accounting.snapshot(owner, incarnation) == snapshot
    assert slots.owner_incarnation(owner) == incarnation
    assert len(provider.posts) == 1
