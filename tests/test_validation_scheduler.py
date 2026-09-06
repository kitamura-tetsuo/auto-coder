"""Production-path regressions for bounded eager specification validation."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate
from auto_coder.automation_engine import AutomationEngine
from auto_coder.decomposition_analyzer import DecompositionAnalysisResult
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.specification_analyzer import SpecificationAnalysisResult
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.validation_scheduler import ValidationScheduler


class Activity:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active >= 2:
                self.started.set()
        assert self.release.wait(3)
        with self.lock:
            self.active -= 1


def issue(number: int, state: str = "open") -> dict[str, object]:
    labels = [{"name": "implementation-ready"}] if number == 10 else []
    return {"id": 1000 + number, "number": number, "title": f"Issue {number}", "body": f"## Requirements\n- REQ-001: Validate {number}.", "state": state, "labels": labels}


def configured_engine(tmp_path: Path, activity: Activity) -> AutomationEngine:
    github = Mock()
    snapshots = {number: issue(number, "closed" if number == 20 else "open") for number in (10, 20, 30, 40)}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    # Retrieval deliberately differs from semantic ordering and contains a
    # closed member, proving the supported GitHub origin preserves both.
    github.get_direct_sub_issues_strict.return_value = [{"number": 40}, {"number": 20}, {"number": 30}]
    config = AutomationConfig(env_override=False)
    object.__setattr__(config, "validation_concurrency", 2)
    engine = AutomationEngine(github, config)

    def individual(*_args: object) -> SpecificationAnalysisResult:
        activity.run()
        return SpecificationAnalysisResult("READY")

    def decomposition(*_args: object) -> DecompositionAnalysisResult:
        activity.run()
        return DecompositionAnalysisResult("READY")

    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "individual.json", individual)
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", decomposition)
    return engine


def test_authoritative_parent_schedules_set_and_all_children_under_one_bound(tmp_path: Path) -> None:
    activity = Activity()
    engine = configured_engine(tmp_path, activity)
    authoritative = engine._fetch_authoritative_decomposition_set("owner/repo", 10)
    assert authoritative is not None
    set_job, children = engine._schedule_parent_validations("owner/repo", authoritative)

    assert activity.started.wait(2), "decomposition and child validation should overlap"
    assert activity.maximum == 2
    assert set(children) == {20, 30, 40}
    assert tuple(member.issue_number for member in engine._get_decomposition_validator("owner/repo").identity(*authoritative).children) == (20, 30, 40)
    assert engine.implementation_slots is None

    activity.release.set()
    assert set_job.result().verdict == "READY"
    assert [children[number].result().verdict for number in sorted(children)] == ["READY", "READY", "READY"]
    assert activity.maximum == 2
    engine.validation_scheduler.shutdown()


def test_duplicate_identity_shares_execution_and_error_is_retryable() -> None:
    scheduler = ValidationScheduler(1)
    release = threading.Event()
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        assert release.wait(2)
        return "ERROR"

    first = scheduler.submit("individual:same", operation)
    duplicate = scheduler.submit("individual:same", operation)
    assert first.future is duplicate.future
    release.set()
    assert first.result() == "ERROR"
    assert calls == 1
    for _ in range(50):
        retry = scheduler.submit("individual:same", lambda: "READY")
        if retry.future is not first.future:
            break
        time.sleep(0.01)
    assert retry.result() == "READY"
    scheduler.shutdown()


@pytest.mark.parametrize("value", [0, -1, None, True])
def test_validation_scheduler_rejects_non_positive_or_absent_bounds(value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ValidationScheduler(value)  # type: ignore[arg-type]


def test_configured_validation_bound_is_positive_and_defaults_bounded(tmp_path: Path) -> None:
    from auto_coder.llm_backend_config import get_validation_concurrency_from_config

    absent = tmp_path / "absent.toml"
    absent.write_text("[process_issues]\n", encoding="utf-8")
    assert get_validation_concurrency_from_config(str(absent)) == 2

    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[process_issues]\nvalidation_concurrency = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="validation_concurrency must be a positive integer"):
        get_validation_concurrency_from_config(str(invalid))


def test_executor_preserves_repository_context_through_real_analyzer_factories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Policy creation and provider execution observe the same repository route."""
    from auto_coder.llm_backend_config import LLMBackendConfiguration, active_repo_context, get_active_repo_name

    def effective_config() -> LLMBackendConfiguration:
        model = "repo-model" if get_active_repo_name() == "owner/repo" else "base-model"
        return LLMBackendConfiguration.load_from_dict(
            {
                "backend_adversarial_validation": {"order": ["codex"]},
                "backends": {"codex": {"backend_type": "codex", "model": model}},
            }
        )

    observed_models: list[dict[str, str]] = []
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", effective_config)

    def create_manager() -> Mock:
        config = effective_config()
        observed_models.append({"codex": config.get_model_for_backend("codex")})
        return Mock()

    monkeypatch.setattr(
        "auto_coder.cli_helpers.create_adversarial_validation_backend_manager",
        create_manager,
    )
    ready = '{"verdict":"READY","findings":[]}'
    monkeypatch.setattr("auto_coder.specification_analyzer.run_llm_prompt", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr("auto_coder.decomposition_analyzer.run_llm_prompt", lambda *_args, **_kwargs: ready)
    from auto_coder.specification_analyzer import analyze_issue_specification

    monkeypatch.setattr(
        "auto_coder.specification_validation_lifecycle.analyze_issue_specification",
        analyze_issue_specification,
    )
    github = Mock()
    parent, children = issue(10), [issue(20), issue(30)]
    snapshots = {10: parent, 20: children[0], 30: children[1]}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_direct_sub_issues_strict.return_value = [dict(child) for child in children]
    engine = AutomationEngine(github, AutomationConfig(env_override=False))

    with active_repo_context("owner/repo"):
        from auto_coder.specification_validation_lifecycle import configured_provider_identity

        policy = configured_provider_identity()
        engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", policy, tmp_path / "sets.json")
        engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", policy, tmp_path / "individual.json")
        authoritative = engine._fetch_authoritative_decomposition_set("owner/repo", 10)
        assert authoritative is not None
        set_job, child_jobs = engine._schedule_parent_validations("owner/repo", authoritative)
        assert set_job.result().verdict == "READY"
        assert child_jobs[20].result().verdict == "READY"
        assert child_jobs[30].result().verdict == "READY"

    assert observed_models == [{"codex": "repo-model"}] * 3
    engine.validation_scheduler.shutdown()


def test_all_closed_parent_processing_uses_shared_validation_capacity(tmp_path: Path) -> None:
    """Candidate routing cannot bypass a capacity occupied by another category."""
    release = threading.Event()
    occupied = threading.Event()
    decomposition_started = threading.Event()
    parent, child = issue(10), issue(20, state="closed")
    github = Mock()
    snapshots = {10: parent, 20: child}
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child)] if number == 10 else []
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    engine = AutomationEngine(github, AutomationConfig(env_override=False))
    engine.validation_scheduler.shutdown()
    engine.validation_scheduler = ValidationScheduler(1)
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle(
        "owner/repo",
        "provider/model",
        tmp_path / "sets.json",
        lambda *_args: decomposition_started.set() or DecompositionAnalysisResult("READY"),
    )
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle(
        "owner/repo",
        "provider/model",
        tmp_path / "individual.json",
        lambda *_args: SpecificationAnalysisResult("READY"),
    )

    blocker = engine.validation_scheduler.submit(
        "individual:occupied",
        lambda: occupied.set() or release.wait(5),
    )
    assert occupied.wait(2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        processing = pool.submit(engine._process_single_candidate_unified, "owner/repo", Candidate("issue", parent, 0, issue_number=10), engine.config)
        assert not decomposition_started.wait(0.2)
        release.set()
        assert blocker.result() is True
        result = processing.result(timeout=5)

    assert decomposition_started.is_set()
    assert result.actions == ["Skipped - submitted parent has no open child eligible for sequential implementation"]
    engine.validation_scheduler.shutdown()
