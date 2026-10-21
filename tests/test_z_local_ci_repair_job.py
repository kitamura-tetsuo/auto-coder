"""Joined production-admission to durable CI runner regressions for Issue #2557."""

from __future__ import annotations

import json
import subprocess
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from auto_coder import pr_processor
from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import build_pr_local_job_adapters
from auto_coder.ci_observation import (
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from auto_coder.ci_repair_authority import CIRepairAuthority, current_ci_failure_authority, evaluate_ci_repair_snapshot
from auto_coder.ci_repair_verification import FocusedVerification, TargetResult, TargetStatus, verify_targets
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.local_ci_repair_execution import CorrectionPorts, LocalCIRepairRequest
from auto_coder.local_ci_repair_job import DurableCICorrection, LocalCIRepairJobPorts, build_local_ci_repair_job_adapter
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore, PRRepairAdmission
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.local_review_repair import LocalReviewRepairStore
from auto_coder.pr_repair_dispatch import RepairDisposition, RepairKind
from auto_coder.pr_repair_workspace import PRRepairSource, PRRepairWorkspaceProducer
from auto_coder.util.github_action import DetailedChecksResult
from tests.ci_observation_fakes import authoritative_ci_status

pytestmark = pytest.mark.pr_repair_contract


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path, str, str]:
    controller = tmp_path / "controller"
    controller.mkdir()
    git(controller, "init", "-b", "main")
    git(controller, "config", "user.email", "test@example.com")
    git(controller, "config", "user.name", "Test")
    (controller / "base.txt").write_text("base\n", encoding="utf-8")
    git(controller, "add", ".")
    git(controller, "commit", "-m", "base")
    base = git(controller, "rev-parse", "HEAD")
    git(controller, "switch", "-c", "feature")
    (controller / "feature.txt").write_text("broken\n", encoding="utf-8")
    scripts = controller / "scripts"
    scripts.mkdir()
    script = scripts / "test.sh"
    script.write_text('#!/bin/bash\nset -e\ntest "$1" = "feature.txt"\ngrep -q repaired "$1"\n', encoding="utf-8")
    script.chmod(0o755)
    git(controller, "add", ".")
    git(controller, "commit", "-m", "feature")
    head = git(controller, "rev-parse", "HEAD")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(controller, "remote", "add", "origin", str(remote))
    git(controller, "push", "origin", "main", "feature")
    return controller, remote, head, base


def snapshot(head: str, conclusion: CIConclusion = CIConclusion.FAILURE) -> CIObservationSnapshot:
    return CIObservationSnapshot(
        ObservationSubject("https://api.github.com", "owner/repo", 17, head),
        ObservationRequest("github-actions", "complete"),
        "cycle",
        0,
        ObservationAvailability.KNOWN,
        (WorkflowObservation(WorkflowExecutionIdentity("101", "202", 1), conclusion),),
    )


def metadata(head: str, base: str, *, number: int = 17) -> dict[str, object]:
    return {
        "number": number,
        "state": "open",
        "merged_at": None,
        "head": {"ref": "feature", "sha": head, "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main", "sha": base, "repo": {"full_name": "owner/repo"}},
        "body": "<!-- auto-coder:local-llm -->",
    }


def wait_until(predicate, timeout: float = 15) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


@dataclass
class Harness:
    root: Path
    controller: Path
    remote: Path
    head: str
    base: str
    store: LocalJobStore
    github: MagicMock
    editor_calls: int = 0
    authority_head: str = ""
    authority_conclusion: CIConclusion = CIConclusion.FAILURE
    barrier_entered: Event | None = None
    barrier_release: Event | None = None
    mode: str = "positive"

    @classmethod
    def create(cls, tmp_path: Path) -> "Harness":
        controller, remote, head, base = repository(tmp_path)
        github = MagicMock()
        github.token = "token"
        github.get_pull_request_metadata_strict.side_effect = lambda *_args: metadata(head, base)
        return cls(tmp_path, controller, remote, head, base, LocalJobStore(tmp_path / "jobs.sqlite3"), github, authority_head=head)

    def offer_from_production_selection(self) -> str:
        authority = CIRepairAuthority(True, "current exact-head CI failure", self.head, snapshot(self.head))
        with (
            pr_processor.local_pr_repair_context(pr_processor.LocalPRRepairContext(self.store, "execution-17")),
            patch.object(pr_processor, "_selected_local_backend_name", return_value="codex"),
            patch.object(pr_processor, "check_pr_repair_exhaustion", return_value=None),
        ):
            result = pr_processor._offer_selected_local_pr_repair(
                "owner/repo",
                metadata(self.head, self.base),
                RepairKind.CI_FAILURE,
                self.github,
                ci_authority=authority,
                store=self.store,
                repair_store=LocalReviewRepairStore(self.root / "reviews.sqlite3"),
            )
        assert result is not None and result.disposition is RepairDisposition.DEFERRED
        return result.receipt

    def offer_from_pr_handler(self) -> str:
        """Drive the supported PR-processing origin through durable admission."""
        from auto_coder import local_pr_repair_job

        pr = metadata(self.head, self.base)
        authority = CIRepairAuthority(True, "current exact-head CI failure", self.head, snapshot(self.head))
        self.github.get_pr_review_threads_strict.return_value = []
        with (
            pr_processor.local_pr_repair_context(pr_processor.LocalPRRepairContext(self.store, "execution-17")),
            patch.object(pr_processor, "current_ci_failure_authority", return_value=nullcontext(authority)),
            patch.object(pr_processor, "check_github_actions_and_exit_if_in_progress", return_value=True),
            patch.object(pr_processor, "_get_mergeable_state", return_value={"mergeable": True}),
            patch.object(pr_processor, "_is_jules_pr", return_value=False),
            patch.object(pr_processor, "_is_local_llm_pr", return_value=True),
            patch.object(pr_processor, "_check_github_actions_status", side_effect=authoritative_ci_status(success=False, ids=[1])),
            patch.object(
                pr_processor,
                "get_detailed_checks_from_history",
                return_value=MagicMock(spec=DetailedChecksResult, success=False, failed_checks=[{"id": 1, "name": "tests", "conclusion": "failure"}]),
            ),
            patch.object(pr_processor, "_selected_local_backend_name", return_value="codex"),
            patch.object(pr_processor, "check_pr_repair_exhaustion", return_value=None),
            patch.object(local_pr_repair_job, "LocalJobStore", return_value=self.store),
        ):
            actions = pr_processor._handle_pr_merge(self.github, "owner/repo", pr, AutomationConfig(), {})
        retained = tuple(job for job in self.store.discover_unsettled() if job.kind is LocalJobKind.PR_CI_FAILURE)
        assert len(retained) == 1
        assert any(retained[0].job_id in action for action in actions)
        return retained[0].job_id

    @contextmanager
    def authority(self, _github: object, _repository: str, _number: int, expected_head: str):  # type: ignore[no-untyped-def]
        if self.barrier_entered is not None:
            self.barrier_entered.set()
            assert self.barrier_release is not None and self.barrier_release.wait(5)
        observed = snapshot(self.authority_head, self.authority_conclusion)
        allowed, reason = evaluate_ci_repair_snapshot(observed)
        if self.authority_head != expected_head:
            allowed, reason = False, "PR head changed before repair initiation"
        yield CIRepairAuthority(allowed, reason, expected_head, observed)

    def correction(self, job, prepared):  # type: ignore[no-untyped-def]
        if self.mode == "missing_backend":
            return None
        config = AutomationConfig()
        if self.mode == "exhausted":
            config.MAX_FIX_ATTEMPTS = 0

        def initial(_diagnostics, _backend, _targets, started):  # type: ignore[no-untyped-def]
            self.editor_calls += 1
            started()
            if self.mode == "deletion":
                (prepared.workspace / "feature.txt").unlink()
            else:
                (prepared.workspace / "feature.txt").write_text("repaired\n", encoding="utf-8")
            return ("edited private workspace",)

        def run_selected(_config, *, test_file):  # type: ignore[no-untyped-def]
            completed = subprocess.run(["bash", "scripts/test.sh", test_file], cwd=prepared.workspace, capture_output=True, text=True)
            return {
                "success": completed.returncode == 0,
                "output": completed.stdout,
                "errors": completed.stderr,
                "return_code": completed.returncode,
                "command": f"bash scripts/test.sh {test_file}",
                "test_file": test_file,
            }

        def focused(targets: list[str]) -> FocusedVerification:
            if self.mode == "exhausted":
                return FocusedVerification([TargetResult(targets[0], TargetStatus.FAILED, raw={"test_file": targets[0]})])
            return verify_targets(targets, run_selected, config, workspace_root=prepared.workspace)

        targets = None if self.mode == "no_targets" else ["feature.txt"]
        request = LocalCIRepairRequest("owner/repo", 17, self.head, "feature", True, (), config, self.github)
        ports = CorrectionPorts(
            automatic_fix_enabled=lambda: True,
            current_backend=lambda: "codex",
            high_score_backend=lambda: "codex",
            initial_correction=initial,
            verify=focused,
            follow_up=lambda *_args: (_ for _ in ()).throw(AssertionError("exhausted budget must prevent follow-up")),
            new_work_allowed=lambda: True,
            pr_closed=lambda: False,
            submit=lambda: (_ for _ in ()).throw(AssertionError("durable runner must not publish")),
            defer_verification=lambda: False,
            trace_initial=lambda: None,
        )
        return DurableCICorrection(request, "failed CI", targets, ports, lambda _logs: [])

    def adapter(self):  # type: ignore[no-untyped-def]
        ports = LocalCIRepairJobPorts(
            source=lambda _job: PRRepairSource(self.controller),
            github_client=lambda _job: self.github,
            authority=self.authority,
            local_route=lambda _job, observed: observed.get("body") == "<!-- auto-coder:local-llm -->",
            correction=self.correction,
        )
        return build_local_ci_repair_job_adapter(self.store, ports, workspace_root=self.root / "workspaces")

    def runner(self, adapter=None) -> LocalJobRunner:  # type: ignore[no-untyped-def]
        return LocalJobRunner(
            self.store,
            InvocationAdmissionGate(),
            repository="owner/repo",
            capacity=1,
            adapters={LocalJobKind.PR_CI_FAILURE: adapter or self.adapter()},
        )


@pytest.mark.parametrize("mode", ["positive", "deletion", "no_targets", "exhausted"])
def test_production_selection_runs_focused_nonpublishing_job(mode: str, tmp_path: Path, _use_real_commands) -> None:
    """REQ-001/004-007: selection, offer, workspace, runner and artifact stay joined."""
    harness = Harness.create(tmp_path)
    harness.mode = mode
    job_id = harness.offer_from_pr_handler()
    remote_before = git(harness.remote, "rev-parse", "refs/heads/feature")
    runner = harness.runner()

    assert runner.poll() == 1
    wait_until(lambda: runner.active_count() == 0)
    completed = harness.store.get(job_id)
    assert completed is not None and completed.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert completed.provider_entered and completed.invocation_outcome is InvocationOutcome.COMPLETED
    artifact = harness.store.get_result_artifact(completed.result_reference)
    assert artifact is not None
    verified = PRRepairWorkspaceProducer(harness.store, harness.root / "workspaces").revalidate_result(completed, artifact)
    changed_feature = dict(verified.manifest.changed_files)["feature.txt"]
    if mode == "deletion":
        assert changed_feature is None
        assert not Path(verified.manifest.workspace, "feature.txt").exists()
    else:
        assert changed_feature is not None
        assert Path(verified.manifest.workspace, "feature.txt").read_text(encoding="utf-8") == "repaired\n"
    assert git(harness.remote, "rev-parse", "refs/heads/feature") == remote_before == harness.head
    assert harness.editor_calls == 1
    observed = json.loads(verified.manifest.observed_output)
    if mode == "no_targets":
        assert any("identified no test file" in action for action in observed["actions"])
    if mode == "exhausted":
        assert any("Max fix attempts (0) reached" in action for action in observed["actions"])
    runner.close()


def test_production_runner_composition_discovers_admitted_ci_job(tmp_path: Path, _use_real_commands) -> None:
    """REQ-001: daemon composition registers and executes the captured CI adapter."""
    harness = Harness.create(tmp_path)
    job_id = harness.offer_from_pr_handler()
    with (
        patch("auto_coder.ci_repair_authority.current_ci_failure_authority", harness.authority),
        patch("auto_coder.pr_processor.build_durable_ci_correction", side_effect=lambda job, prepared, *_args: harness.correction(job, prepared)),
    ):
        adapters = build_pr_local_job_adapters(harness.github, AutomationConfig(), harness.store, harness.controller)
        runner = LocalJobRunner(harness.store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters=adapters)
        assert runner.poll() == 1
        wait_until(lambda: runner.active_count() == 0)
    completed = harness.store.get(job_id)
    assert completed is not None and completed.invocation_outcome is InvocationOutcome.COMPLETED
    assert harness.editor_calls == 1
    assert harness.store.get_result_artifact(completed.result_reference) is not None
    runner.close()


def test_explicit_checked_out_jules_authority_survives_into_production_runner(tmp_path: Path, _use_real_commands) -> None:
    harness = Harness.create(tmp_path)
    jules = metadata(harness.head, harness.base)
    jules["user"] = {"login": "google-labs-jules[bot]"}
    jules["body"] = "Jules session: session-17"
    harness.github.get_pull_request_metadata_strict.side_effect = lambda *_args: jules
    authority = CIRepairAuthority(True, "current exact-head CI failure", harness.head, snapshot(harness.head))
    with (
        pr_processor.local_pr_repair_context(pr_processor.LocalPRRepairContext(harness.store, "execution-17")),
        patch.object(pr_processor, "_selected_local_backend_name", return_value="codex"),
        patch.object(pr_processor, "check_pr_repair_exhaustion", return_value=None),
    ):
        admitted = pr_processor._offer_selected_local_pr_repair(
            "owner/repo",
            jules,
            RepairKind.CI_FAILURE,
            harness.github,
            ci_authority=authority,
            explicit_local_checkout=True,
            store=harness.store,
            repair_store=LocalReviewRepairStore(tmp_path / "reviews.sqlite3"),
        )
    assert admitted is not None and admitted.disposition is RepairDisposition.DEFERRED

    with (
        patch("auto_coder.ci_repair_authority.current_ci_failure_authority", harness.authority),
        patch(
            "auto_coder.pr_processor.build_durable_ci_correction",
            side_effect=lambda job, prepared, *_args: harness.correction(job, prepared),
        ),
    ):
        adapters = build_pr_local_job_adapters(harness.github, AutomationConfig(), harness.store, harness.controller)
        runner = LocalJobRunner(
            harness.store,
            InvocationAdmissionGate(),
            repository="owner/repo",
            capacity=1,
            adapters=adapters,
        )
        assert runner.poll() == 1
        wait_until(lambda: runner.active_count() == 0)

    completed = harness.store.get(admitted.receipt)
    assert completed is not None and completed.invocation_outcome is InvocationOutcome.COMPLETED
    assert harness.editor_calls == 1
    runner.close()


def test_changed_head_at_pre_entry_barrier_records_no_start(tmp_path: Path, _use_real_commands) -> None:
    """REQ-002/003/006: superseding H after preparation produces zero provider entry."""
    harness = Harness.create(tmp_path)
    job_id = harness.offer_from_pr_handler()
    harness.barrier_entered, harness.barrier_release = Event(), Event()
    runner = harness.runner()

    assert runner.poll() == 1
    assert harness.barrier_entered.wait(5)
    harness.authority_head = "H2"
    harness.barrier_release.set()
    wait_until(lambda: runner.active_count() == 0)

    refused = harness.store.get(job_id)
    assert refused is not None and refused.state is LocalJobState.PENDING
    assert refused.provider_entered is False and refused.execution_incarnation == ""
    assert "head changed" in refused.diagnostic
    assert harness.editor_calls == 0
    runner.close()


@pytest.mark.parametrize(
    ("status", "conclusion", "run_id"),
    [
        ("in_progress", None, "202"),
        ("completed", "success", "202"),
        ("completed", "failure", "303"),
    ],
    ids=["pending-same-generation", "green-same-generation", "different-failed-generation"],
)
def test_production_ci_authority_refuses_superseded_facts_before_entry(
    status: str,
    conclusion: str | None,
    run_id: str,
    tmp_path: Path,
    _use_real_commands,
) -> None:
    """REQ-002/006: fresh production CI facts must match the retained failure generation."""
    harness = Harness.create(tmp_path)
    job_id = harness.offer_from_pr_handler()
    entered, release = Event(), Event()
    remote_before = git(harness.remote, "rev-parse", "refs/heads/feature")
    observed_ci: dict[str, str | None] = {"status": "completed", "conclusion": "failure", "run_id": "202"}

    class Checks:
        @staticmethod
        def list_for_ref(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"check_runs": []}

    class Actions:
        @staticmethod
        def list_workflow_runs_for_repo(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            assert release.wait(5)
            return {
                "workflow_runs": [
                    {
                        "id": int(observed_ci["run_id"] or "0"),
                        "workflow_id": 101,
                        "run_attempt": 1,
                        "head_sha": harness.head,
                        "status": observed_ci["status"],
                        "conclusion": observed_ci["conclusion"],
                        "name": "tests",
                        "path": ".github/workflows/tests.yml",
                    }
                ]
            }

    api = type("API", (), {"checks": Checks(), "actions": Actions()})()
    adapter = harness.adapter()
    adapter._ports = LocalCIRepairJobPorts(  # type: ignore[attr-defined]
        source=adapter._ports.source,  # type: ignore[attr-defined]
        github_client=adapter._ports.github_client,  # type: ignore[attr-defined]
        authority=current_ci_failure_authority,
        local_route=adapter._ports.local_route,  # type: ignore[attr-defined]
        correction=adapter._ports.correction,  # type: ignore[attr-defined]
    )
    runner = harness.runner(adapter)

    with patch("auto_coder.ci_repair_authority.get_ghapi_client", return_value=api):
        assert runner.poll() == 1
        assert entered.wait(5)
        observed_ci.update(status=status, conclusion=conclusion, run_id=run_id)
        release.set()
        wait_until(lambda: runner.active_count() == 0)

    refused = harness.store.get(job_id)
    assert refused is not None and refused.state is LocalJobState.PENDING
    assert refused.provider_entered is False and refused.execution_incarnation == ""
    assert refused.diagnostic
    assert harness.editor_calls == 0
    assert git(harness.remote, "rev-parse", "refs/heads/feature") == remote_before
    runner.close()


@pytest.mark.parametrize("new_state", ["changed_head", "closed"])
def test_newer_strict_metadata_refuses_entry(new_state: str, tmp_path: Path, _use_real_commands) -> None:
    """REQ-002: the adapter must honor metadata newer than the authority read."""
    harness = Harness.create(tmp_path)
    job_id = harness.offer_from_pr_handler()
    open_h = metadata(harness.head, harness.base)
    newer = metadata("H2" if new_state == "changed_head" else harness.head, harness.base)
    if new_state == "closed":
        newer["state"] = "closed"
    harness.github.get_pull_request_metadata_strict.side_effect = [open_h, newer]
    adapter = harness.adapter()
    adapter._ports = LocalCIRepairJobPorts(  # type: ignore[attr-defined]
        source=adapter._ports.source,  # type: ignore[attr-defined]
        github_client=adapter._ports.github_client,  # type: ignore[attr-defined]
        authority=current_ci_failure_authority,
        local_route=adapter._ports.local_route,  # type: ignore[attr-defined]
        correction=adapter._ports.correction,  # type: ignore[attr-defined]
    )
    api = type(
        "API",
        (),
        {
            "checks": type("Checks", (), {"list_for_ref": lambda *_a, **_k: {"check_runs": []}})(),
            "actions": type(
                "Actions",
                (),
                {
                    "list_workflow_runs_for_repo": lambda *_a, **_k: {
                        "workflow_runs": [
                            {
                                "id": 202,
                                "workflow_id": 101,
                                "run_attempt": 1,
                                "head_sha": harness.head,
                                "status": "completed",
                                "conclusion": "failure",
                                "name": "tests",
                                "path": ".github/workflows/tests.yml",
                            }
                        ]
                    }
                },
            )(),
        },
    )()
    runner = harness.runner(adapter)
    with patch("auto_coder.ci_repair_authority.get_ghapi_client", return_value=api):
        assert runner.poll() == 1
        wait_until(lambda: runner.active_count() == 0)
    refused = harness.store.get(job_id)
    assert refused is not None and refused.state is LocalJobState.PENDING
    assert not refused.provider_entered and harness.editor_calls == 0
    assert "head, base, open state" in refused.diagnostic
    runner.close()


def test_missing_backend_does_not_touch_unrelated_pending_job(tmp_path: Path, _use_real_commands) -> None:
    """REQ-003/006: definitely-unentered preparation failure is isolated and retryable."""
    harness = Harness.create(tmp_path)
    job_id = harness.offer_from_production_selection()
    harness.mode = "missing_backend"
    unrelated = LocalJobOffer(LocalJobKind.PR_CI_FAILURE, "owner/repo", 18, "other-attempt", "codex", "other repair")
    unrelated_admission = PRRepairAdmission(
        "owner/repo",
        "feature",
        harness.head,
        "owner/repo",
        "main",
        harness.base,
        "ci_failure",
        "other-evidence",
        "other-incarnation",
        ("workflow:101:run:202:attempt:1",),
        True,
        True,
        True,
        True,
        ci_complete_failure=True,
    )
    assert harness.store.offer_pr_repair(unrelated, unrelated_admission, LocalReviewRepairStore(tmp_path / "other-reviews.sqlite3")) is not None
    runner = harness.runner()

    assert runner.poll() == 1
    wait_until(lambda: runner.active_count() == 0)
    refused = harness.store.get(job_id)
    untouched = harness.store.get(unrelated.job_id)
    assert refused is not None and refused.state is LocalJobState.PENDING and not refused.provider_entered
    assert untouched is not None and untouched.state is LocalJobState.PENDING and not untouched.provider_entered
    assert harness.editor_calls == 0
    runner.close()


def test_reopened_runner_never_replays_entered_unknown_invocation(tmp_path: Path, _use_real_commands) -> None:
    """REQ-003/006: a dead entered incarnation remains reconciliation-only."""
    harness = Harness.create(tmp_path)
    job_id = harness.offer_from_production_selection()
    claim = harness.store.claim(job_id, "process:999999999:1")
    assert claim is not None and claim.acquired and harness.store.mark_provider_entered(claim)
    prepared = PRRepairWorkspaceProducer(harness.store, harness.root / "workspaces").prepare(claim, PRRepairSource(harness.controller))
    calls = 0

    class CountingAdapter:
        def authorize_provider_entry(self, _job):  # type: ignore[no-untyped-def]
            return True

        def invoke(self, _job):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise AssertionError("entered unknown work must not replay")

    reopened = LocalJobStore(harness.store.path)
    runner = LocalJobRunner(reopened, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.PR_CI_FAILURE: CountingAdapter()})
    runner.recover_unsettled()
    runner.recover_unsettled()

    retained = reopened.get(job_id)
    assert retained is not None and retained.state is LocalJobState.RUNNING and retained.provider_entered
    assert Path(retained.workspace_path) == prepared.workspace
    assert calls == 0 and runner.poll() == 0
    runner.close()
