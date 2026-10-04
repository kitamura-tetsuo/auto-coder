"""Controller-designated CI repair skips the automatic unscoped baseline (Issue #2422)."""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

import pytest

from src.auto_coder.execution_trace import EventKind, Outcome, get_trace_collector
from src.auto_coder.invocation_admission import bind_ci_repair_designation, ci_repair_designated
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from tests.test_muse_msp import _host, _manager, _repository
from tests.utils.workspace import write_target_test_script

_CASES = itertools.count(242200)
_MARKER_SCRIPT = '#!/bin/bash\nprintf run >> "$BASELINE_MARKER"\nexit 0\n'


def _config() -> LLMBackendConfiguration:
    return LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})


def _setup(tmp_path, monkeypatch, script: str = _MARKER_SCRIPT):
    repo = _repository(tmp_path)
    write_target_test_script(repo, script)
    marker = tmp_path / "baseline-marker"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("BASELINE_MARKER", str(marker))
    return repo, marker


def _baseline_events(case: int):
    snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type="issue", item_number=case)
    return [e for e in snapshot.events if e.kind == EventKind.STAGE_RESULT.value and e.stage_id == "local.workspace-tests"]


def _run(manager, case: int, prompt: str = "first", designated: bool = False):
    with get_trace_collector().start_execution("owner/repo", "issue", case, origin="worker") as execution:
        if designated:
            with bind_ci_repair_designation():
                out = manager._run_llm_cli(prompt)
        else:
            out = manager._run_llm_cli(prompt)
        execution.finish(Outcome.COMPLETED)
    return out


def test_designated_invocation_skips_baseline_but_hands_off_edit(tmp_path, monkeypatch, _use_real_commands):
    repo, marker = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MSP_MUTATE", "1")
    case = next(_CASES)
    assert _run(_manager(_config()), case, designated=True) == "answer:first"
    assert not marker.exists()
    assert (repo / "tracked.txt").read_text() == "mutated\n"
    events = _baseline_events(case)
    assert len(events) == 1
    assert events[0].outcome == Outcome.SKIPPED.value
    assert events[0].facts["baseline"] == "not_run"
    assert events[0].facts["reason"] == "ci_repair_policy"
    assert events[0].facts["invocation_id"]
    assert "exit_code" not in events[0].facts and "log_path" not in events[0].facts


def test_undesignated_invocation_runs_baseline_and_designation_does_not_leak(tmp_path, monkeypatch, _use_real_commands):
    _, marker = _setup(tmp_path, monkeypatch)
    manager = _manager(_config())
    first, second, third = next(_CASES), next(_CASES), next(_CASES)
    _run(manager, first, designated=True)
    assert not marker.exists()
    _run(manager, second)
    assert marker.read_text() == "run"
    assert _baseline_events(second)[0].outcome == Outcome.COMPLETED.value
    # A designated operation that raises must not leave the designation behind.
    with pytest.raises(RuntimeError):
        with bind_ci_repair_designation():
            raise RuntimeError("boom")
    assert ci_repair_designated() is False
    _run(manager, third)
    assert marker.read_text() == "runrun"


def test_failing_baseline_still_allows_provider_for_ordinary_invocation(tmp_path, monkeypatch, _use_real_commands):
    _, marker = _setup(tmp_path, monkeypatch, '#!/bin/bash\nprintf run >> "$BASELINE_MARKER"\nexit 1\n')
    assert _run(_manager(_config()), next(_CASES)) == "answer:first"
    assert marker.read_text() == "run"


def test_designated_invocation_still_refuses_on_workspace_preparation_failure(tmp_path, monkeypatch, _use_real_commands):
    from src.auto_coder.worktree_utils import WorkspacePreparationError

    repo, marker = _setup(tmp_path, monkeypatch)
    (repo / "sub").mkdir()
    (repo / ".gitmodules").write_text("")
    import subprocess

    subprocess.run(["git", "update-index", "--add", "--cacheinfo", "160000,0123456789012345678901234567890123456789,sub"], cwd=repo, check=True)  # unsupported submodule entry refuses preparation
    with pytest.raises(WorkspacePreparationError):
        _run(_manager(_config()), next(_CASES), designated=True)
    assert not marker.exists()
    assert not (tmp_path / "msp.jsonl").exists()


def test_designation_is_per_context_under_real_overlap(tmp_path, monkeypatch, _use_real_commands):
    _, marker = _setup(tmp_path, monkeypatch)
    manager = _manager(_config())
    barrier = threading.Barrier(2, timeout=10)
    seen: dict[str, bool] = {}

    def designated():
        with bind_ci_repair_designation():
            barrier.wait()
            seen["designated"] = ci_repair_designated()
            barrier.wait()

    def ordinary():
        barrier.wait()
        seen["ordinary"] = ci_repair_designated()
        barrier.wait()

    threads = [threading.Thread(target=designated), threading.Thread(target=ordinary)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert seen == {"designated": True, "ordinary": False}
    assert not marker.exists()
    _run(manager, next(_CASES))
    assert marker.read_text() == "run"


def test_designation_survives_automatic_backend_fallback_into_replacement_clone(tmp_path, monkeypatch, _use_real_commands):
    from unittest.mock import patch

    from src.auto_coder.cli_helpers import build_backend_manager

    repo, marker = _setup(tmp_path, monkeypatch, '#!/bin/bash\nprintf run >> "$BASELINE_MARKER"\nexit 127\n')
    host = tmp_path / "muse"
    # The first provider turn reports quota exhaustion; the replacement attempt succeeds and edits.
    host.write_text(
        host.read_text().replace(
            'if os.environ.get("MSP_QUOTA_RESPONSE"):',
            'if os.environ.get("MSP_QUOTA_ONCE") and not Path(os.environ["MSP_QUOTA_ONCE"]).exists() and (Path(os.environ["MSP_QUOTA_ONCE"]).write_text("x") or True):',
            1,
        )
    )
    monkeypatch.setenv("MSP_QUOTA_ONCE", str(tmp_path / "quota-spent"))
    monkeypatch.setenv("MSP_MUTATE", "1")
    names = ["muse", "muse2"]
    config = LLMBackendConfiguration(backends={n: BackendConfig(name=n, backend_type="muse", model="muse-spark-1.3") for n in names})
    with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.muse_client.get_llm_config", return_value=config), patch("src.auto_coder.backend_manager.get_llm_config", return_value=config):
        manager = build_backend_manager(names, "muse", {n: "muse-spark-1.3" for n in names})
        case = next(_CASES)
        assert _run(manager, case, designated=True) == "answer:first"
        assert not marker.exists()
        assert (repo / "tracked.txt").read_text() == "mutated\n"
        events = _baseline_events(case)
        assert len(events) == 2
        assert {e.outcome for e in events} == {Outcome.SKIPPED.value}
        assert len({e.facts["invocation_id"] for e in events}) == 2
        # A later non-designated invocation through the same manager runs its baseline.
        later = next(_CASES)
        from src.auto_coder.worktree_utils import WorkspacePreparationError

        with pytest.raises(WorkspacePreparationError, match="could not complete"):
            _run(manager, later)
        assert marker.read_text() == "run"
