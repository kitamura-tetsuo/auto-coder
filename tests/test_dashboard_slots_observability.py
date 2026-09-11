"""Production-to-mounted-main-page regressions for the Implementation
Slots panel (Issue #1993).

These drive the real `ImplementationSlotRepository` API (the durable
store from Issue #1992) and the real
`AutomationEngine.get_implementation_slot_snapshot` adapter, then execute
the dashboard's actual async slot-refresh callback registered via
`ui.timer`, mirroring the production-to-mounted-view pattern
`test_dashboard_observability.py` uses for the detail page (`_mounted_detail`).
A test that hand-builds a snapshot object or only checks that
`ui.page`/`ui.timer` were registered would not establish that production
writes actually reach the mounted page (see Issue #1993's AS-001).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from auto_coder.automation_config import AutomationConfig, Candidate
from auto_coder.automation_engine import AutomationEngine
from auto_coder.dashboard import init_dashboard
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

REPO = "owner/repo"


def _mount_main(mock_ui, engine: AutomationEngine, repo_name: str = REPO):
    pages = {}

    def page(path):
        def register(function):
            pages[path] = function
            return function

        return register

    mock_ui.page.side_effect = page
    init_dashboard(FastAPI(), engine, repo_name)
    pages["/"]()
    return pages


def _slots_refresh_callback(mock_ui):
    """The main page registers two independent `ui.timer` callbacks: one for
    Workers/Queue/Open Items, one for the Implementation Slots panel. Select
    the slots one by its function name, proving the panel does not share a
    callback (and therefore not a failure/blocking mode) with the rest of
    the page (REQ-005, REQ-006)."""
    for call in mock_ui.timer.call_args_list:
        callback = call.args[1]
        if callback.__name__ == "refresh_slots":
            return callback
    raise AssertionError("refresh_slots was never registered via ui.timer")


def _link_calls(mock_ui):
    return [call.args for call in mock_ui.link.call_args_list]


def _label_texts(mock_ui):
    return [call.args[0] for call in mock_ui.label.call_args_list if call.args]


def _banner_texts(mock_ui):
    """The status banner is one persistent `ui.label(...).classes(...)`
    element updated via `.set_text(...)` on every refresh (REQ-005: cheap
    per-tick update, no rebuild). `ui.label(...)` and the chained
    `.classes(...)` are both mocked calls, so the live element handle is
    `ui.label.return_value.classes.return_value`, and its `set_text`
    history is where each refresh's banner text actually lands."""
    return [call.args[0] for call in mock_ui.label.return_value.classes.return_value.set_text.call_args_list if call.args]


@patch("auto_coder.dashboard.ui")
def test_production_ownership_reaches_mounted_main_page(mock_ui, tmp_path):
    """AS-001: an Issue admitted through the real slot API, with a recorded
    implementation PR and provider session, finishing its execution while
    retaining ownership, and an independent standalone PR owner, both reach
    the mounted main page's Implementation Slots panel with correct links.
    Multiple executions of the same owner still consume only one slot row."""
    slots = ImplementationSlotRepository(REPO, 5, tmp_path / "slots.json")
    issue_owner = ImplementationOwner("issue", 5010)
    execution_id = slots.start_execution(issue_owner)
    assert execution_id is not None
    assert slots.record_implementation_pr(issue_owner, 5011)
    assert slots.record_provider_session(issue_owner, "session-xyz")
    # A second execution for the same owner (bypassing the duplicate-active
    # guard, as a resumed/handoff continuation would) still counts as one
    # normal slot, not two.
    second_execution_id = slots.start_execution(issue_owner, bypass_active_execution=True)
    assert second_execution_id is not None
    # No active local worker: the execution finishes, but ownership itself
    # is retained (a remote handoff or a retained PR can still occupy it).
    slots.finish_execution(issue_owner, execution_id)

    standalone_pr_owner = ImplementationOwner("pr", 5020)
    assert slots.reserve_new(standalone_pr_owner)

    engine = AutomationEngine(MagicMock())
    engine.implementation_slots = slots

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())

    links = _link_calls(mock_ui)
    assert ("Issue #5010", "/detail/issue/5010") in links
    assert ("#5011", "/detail/pr/5011") in links
    assert ("Pr #5020", "/detail/pr/5020") in links

    labels = _label_texts(mock_ui)
    assert any("session-xyz" in text for text in labels)
    assert any(second_execution_id in text for text in labels)
    # Two normal owners (the Issue and the standalone PR), 5 available minus
    # 2 used = 3, no emergency usage.
    assert any("Normal: 2/5 used, 3 available" in text for text in labels)
    assert any("Emergency: 0" in text for text in labels)


@patch("auto_coder.dashboard.ui")
def test_capacity_override_and_emergency_ownership_are_honest(mock_ui, tmp_path):
    """AS-002: normal limit 1, two normal owners admitted via the explicit
    capacity bypass plus one emergency owner shows normal 2/1 used, normal
    available 0, and emergency usage 1, with all three rows -- and several
    memberships under one owner do not inflate usage."""
    slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    owner_a = ImplementationOwner("issue", 6001)
    owner_b = ImplementationOwner("issue", 6002)
    emergency_owner = ImplementationOwner("issue", 6003)

    assert slots.start_execution(owner_a, bypass_capacity=True) is not None
    assert slots.start_execution(owner_b, bypass_capacity=True) is not None
    assert slots.record_implementation_pr(owner_a, 6011)
    assert slots.record_implementation_pr(owner_a, 6012)
    assert slots.record_provider_session(owner_a, "session-a1")
    assert slots.record_provider_session(owner_a, "session-a2")
    # Fill normal capacity with a distinct owner sequence, then admit the
    # emergency owner through the urgent-emergency path.
    slots2 = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    assert slots2.start_execution(emergency_owner, allow_urgent_emergency=True) is not None

    engine = AutomationEngine(MagicMock())
    engine.implementation_slots = slots

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())

    labels = _label_texts(mock_ui)
    assert any("Normal: 2/1 used, 0 available" in text for text in labels)
    assert any("Emergency: 1" in text for text in labels)
    links = _link_calls(mock_ui)
    assert ("Issue #6001", "/detail/issue/6001") in links
    assert ("Issue #6002", "/detail/issue/6002") in links
    assert ("Issue #6003", "/detail/issue/6003") in links
    # owner_a's two PRs and two sessions do not inflate normal usage above 2.
    assert ("#6011", "/detail/pr/6011") in links
    assert ("#6012", "/detail/pr/6012") in links


@patch("auto_coder.dashboard.ui")
def test_startup_observes_persisted_owner_from_correct_store(mock_ui, tmp_path, monkeypatch):
    """AS-003: a process writes an owner and exits; a brand-new engine with
    an initially uninitialized (`engine.implementation_slots is None`) slot
    repository must still observe it on the first successful refresh, using
    the REAL `AUTO_CODER_RUNTIME_ROOT`-based store-selection boundary
    (`ImplementationSlotRepository.__init__`'s own default-path resolution,
    exercised via `_get_implementation_slots`) -- not a patched stand-in,
    and not a differently populated default (HOME-based) or
    other-repository store."""
    runtime_root = tmp_path / "runtime-root"
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("HOME", str(fake_home))

    # No explicit storage_path override: this resolves via the same
    # AUTO_CODER_RUNTIME_ROOT-based default path production uses.
    runtime_selected_writer = ImplementationSlotRepository(REPO, 3)
    assert runtime_selected_writer.storage_path == runtime_root / "state" / REPO / "implementation_slots.json"
    owner = ImplementationOwner("issue", 7001)
    assert runtime_selected_writer.start_execution(owner) is not None
    del runtime_selected_writer  # the writing process has exited

    # A differently populated store for a different repository, under the
    # SAME runtime root, must never be substituted for this one.
    other_repo_writer = ImplementationSlotRepository("owner/other-repo", 3)
    assert other_repo_writer.start_execution(ImplementationOwner("issue", 9999)) is not None
    del other_repo_writer

    # A differently populated default (HOME-based, no AUTO_CODER_RUNTIME_ROOT)
    # store for the SAME repo name must also never be substituted -- this
    # proves selection genuinely used the runtime root, not silently fell
    # back to the unset-env-var default.
    monkeypatch.delenv("AUTO_CODER_RUNTIME_ROOT", raising=False)
    default_store_writer = ImplementationSlotRepository(REPO, 3)
    assert default_store_writer.storage_path == fake_home / ".auto-coder" / REPO / "implementation_slots.json"
    assert default_store_writer.start_execution(ImplementationOwner("issue", 8888)) is not None
    del default_store_writer
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime_root))  # restore for the engine under test

    engine = AutomationEngine(MagicMock())
    assert engine.implementation_slots is None

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())

    links = _link_calls(mock_ui)
    assert ("Issue #7001", "/detail/issue/7001") in links
    assert ("Issue #9999", "/detail/issue/9999") not in links
    assert ("Issue #8888", "/detail/issue/8888") not in links
    # The now-bound repository is genuinely the runtime-root-selected one,
    # not a patched stand-in.
    assert engine.implementation_slots is not None
    assert engine.implementation_slots.storage_path == runtime_root / "state" / REPO / "implementation_slots.json"


@patch("auto_coder.dashboard.ui")
def test_unknown_is_never_free_and_recovery_returns_same_state(mock_ui, tmp_path):
    """AS-004: a readable occupied store, then a failing read, must keep
    showing the prior rows/counters with a stale indication and an
    unchanged successful-observation time; a later successful read (even of
    the same unchanged state) clears the stale indication."""
    slots = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 8001)
    assert slots.start_execution(owner) is not None

    engine = AutomationEngine(MagicMock())
    engine.implementation_slots = slots

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())
    assert ("Issue #8001", "/detail/issue/8001") in _link_calls(mock_ui)
    assert not any("STALE" in text for text in _banner_texts(mock_ui))
    success_banner = _banner_texts(mock_ui)[-1]
    success_timestamp = success_banner.split("as of ")[1].split(" (local")[0]

    real_snapshot = slots.snapshot
    with patch.object(slots, "snapshot", side_effect=RuntimeError("simulated storage boundary failure")):
        asyncio.run(refresh_slots())
    banners_during_failure = _banner_texts(mock_ui)
    assert "STALE" in banners_during_failure[-1]
    # The successful-observation time embedded in the banner must not
    # advance on a failed refresh: it is still the timestamp from the
    # earlier successful read.
    assert f"observation {success_timestamp};" in banners_during_failure[-1]
    # The prior owner row must remain -- not replaced with a zero/free
    # display -- while unavailable.
    links_during_failure = _link_calls(mock_ui)
    assert ("Issue #8001", "/detail/issue/8001") in links_during_failure
    assert not any("Normal: 0/2" in text for text in _label_texts(mock_ui))

    slots.snapshot = real_snapshot
    asyncio.run(refresh_slots())
    banners_after_recovery = _banner_texts(mock_ui)
    assert "STALE" not in banners_after_recovery[-1]
    assert ("Issue #8001", "/detail/issue/8001") in _link_calls(mock_ui)


@patch("auto_coder.dashboard.ui")
def test_never_observed_shows_unavailable_without_free_capacity(mock_ui, tmp_path):
    """AS-004: before any successful observation, an unreadable store must
    show an explicit unavailable reason, never a fabricated zero/free
    capacity or an empty-success message."""
    unreadable_dir = tmp_path / "state"
    unreadable_dir.mkdir()
    state_file = unreadable_dir / "slots.json"
    state_file.write_text("{not valid json")

    slots = ImplementationSlotRepository(REPO, 4, state_file)
    engine = AutomationEngine(MagicMock())
    engine.implementation_slots = slots

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())

    banners = _banner_texts(mock_ui)
    assert any("unavailable" in text.lower() for text in banners)
    labels = _label_texts(mock_ui)
    assert not any("0/4" in text for text in labels)
    assert not any("4 available" in text for text in labels)


@patch("auto_coder.dashboard.ui")
def test_empty_store_is_known_zero_not_unavailable(mock_ui, tmp_path):
    """Contrast case for AS-004: a valid, confirmed-absent state file is a
    successful empty observation with zero usage and full capacity
    available -- not treated as unavailable."""
    slots = ImplementationSlotRepository(REPO, 4, tmp_path / "does-not-exist" / "slots.json")
    engine = AutomationEngine(MagicMock())
    engine.implementation_slots = slots

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())

    labels = _label_texts(mock_ui)
    assert any("Normal: 0/4 used, 4 available" in text for text in labels)
    assert not any("unavailable" in text.lower() for text in labels)


@patch("auto_coder.dashboard.ui")
def test_legacy_and_falsy_recorded_fields_are_observed_not_repaired(mock_ui, tmp_path):
    """AS-006: an owner with empty membership lists and admission flags
    absent from a legacy reservation must still be displayed and counted,
    never marked free; `false` remains distinguishable from `not recorded`
    in the rendered admission fields."""
    slots = ImplementationSlotRepository(REPO, 3, tmp_path / "slots.json")
    legacy_owner = ImplementationOwner("pr", 8801)
    assert slots.reserve_new(legacy_owner)  # legacy shape: no executions/admission fields

    engine = AutomationEngine(MagicMock())
    engine.implementation_slots = slots

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())

    labels = _label_texts(mock_ui)
    assert any("admission_pending: not recorded; admission_established: not recorded" in text for text in labels)
    assert any("No recorded executions" in text for text in labels)
    assert any("Normal: 1/3 used, 2 available" in text for text in labels)


@patch("auto_coder.dashboard.ui")
def test_render_failure_after_partial_update_remains_a_slot_panel_diagnostic(mock_ui, tmp_path):
    """REQ-006: a rendering/projection failure that happens AFTER part of
    the render path has already run (not merely a storage read failure)
    must not escape the refresh timer callback, must not promote a
    partially-rendered snapshot to "last known", and the panel must show a
    diagnostic while the previously successful (coherent) snapshot stays
    displayed rather than a mix of old and new state."""
    slots = ImplementationSlotRepository(REPO, 3, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 8501)
    assert slots.start_execution(owner) is not None

    engine = AutomationEngine(MagicMock())
    engine.implementation_slots = slots

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())
    assert ("Issue #8501", "/detail/issue/8501") in _link_calls(mock_ui)
    assert not any("STALE" in text for text in _banner_texts(mock_ui))

    # Change the underlying state (a second owner) so the next observation
    # genuinely differs, then make the summary/banner half of rendering
    # fail -- after `sync_owners` has already applied the new owner rows.
    other_owner = ImplementationOwner("issue", 8502)
    assert slots.start_execution(other_owner) is not None
    with patch("auto_coder.dashboard_slots.summarize", side_effect=RuntimeError("simulated mid-render failure")):
        asyncio.run(refresh_slots())  # must not raise past the timer callback

    banners = _banner_texts(mock_ui)
    assert "rendering failed" in banners[-1].lower()
    # The prior (coherent) owner's link is (re-)created as part of the
    # compensating re-render back to the last fully-rendered snapshot.
    # (Whether the transient owner-8502 row is actually removed from the
    # live DOM -- not just absent from a *fresh* render -- is a real-browser
    # concern verified in test_dashboard_slots_scroll_stability.py: a mocked
    # `ui.link` only accumulates a call history, it does not model
    # `.clear()` actually removing prior children.)
    assert ("Issue #8501", "/detail/issue/8501") in _link_calls(mock_ui)

    # Recovery: a later successful refresh (rendering restored) reaches the
    # STALE indicator's clearing path and shows the real current state.
    asyncio.run(refresh_slots())
    banners_after_recovery = _banner_texts(mock_ui)
    assert "STALE" not in banners_after_recovery[-1]
    assert "rendering failed" not in banners_after_recovery[-1].lower()
    links_after_recovery = _link_calls(mock_ui)
    assert ("Issue #8501", "/detail/issue/8501") in links_after_recovery
    assert ("Issue #8502", "/detail/issue/8502") in links_after_recovery


@patch("auto_coder.dashboard.ui")
def test_no_external_calls_or_state_mutation_from_mount_refresh_and_navigation(mock_ui, tmp_path):
    """AS-006: loading, refreshing, and navigating away from the slot panel
    must never issue a GitHub/provider request, a liveness probe, or a
    slot reservation/release/reconciliation call, and must never change
    the durable implementation-state file's bytes or permissions."""
    state_path = tmp_path / "slots.json"
    slots = ImplementationSlotRepository(REPO, 3, state_path)
    owner = ImplementationOwner("issue", 8601)
    assert slots.start_execution(owner) is not None
    assert slots.record_implementation_pr(owner, 8602)

    bytes_before = state_path.read_bytes()
    mode_before = state_path.stat().st_mode

    github_client = MagicMock(name="github_client")
    engine = AutomationEngine(github_client)
    engine.implementation_slots = slots

    mutating_methods = ["reserve", "reserve_new", "start_execution", "finish_execution", "reconcile"]
    with patch.multiple(
        ImplementationSlotRepository,
        **{name: MagicMock(side_effect=AssertionError(f"{name} must not be called by the slot panel")) for name in mutating_methods},
    ):
        pages = _mount_main(mock_ui, engine, REPO)
        refresh_slots = _slots_refresh_callback(mock_ui)
        asyncio.run(refresh_slots())
        asyncio.run(refresh_slots())
        # Navigating to the detail page the owner/PR links point at must
        # not itself trigger any slot mutation or external call either.
        pages["/detail/{item_type}/{item_number}"](item_type="issue", item_number=8601)

    # No GitHub/provider call of any kind was made.
    assert github_client.method_calls == []
    # The durable implementation-state file is byte-for-byte and
    # permission-bit identical to before mounting/refreshing/navigating.
    assert state_path.read_bytes() == bytes_before
    assert state_path.stat().st_mode == mode_before


@patch("auto_coder.dashboard.ui")
def test_real_controller_admission_origin_reaches_mounted_page(mock_ui, tmp_path):
    """AS-001: at least one joined regression must originate from a
    supported controller processing origin (not a direct repository-API
    bypass), preserving production slot writes, snapshot acquisition,
    controller adaptation, and mounted refresh -- mirroring
    `test_standalone_dependency_gate_reaches_mounted_detail_view`'s proven
    pattern in `test_dashboard_observability.py` for driving real
    admission through `AutomationEngine._process_single_candidate_unified`."""
    from auto_coder.automation_config import CandidateProcessingResult, ExplicitTargetOutcome
    from auto_coder.specification_analyzer import SpecificationAnalysisResult
    from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
    from auto_coder.util.gh_cache import GitHubClient

    issue = {
        "number": 9001,
        "id": 900100,
        "title": "Real admission origin",
        "body": "Blocked-By:\n\n## Objective\n\nResolve eligible work.\n\n## Requirements\nREQ-001: Resolve eligible work.",
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
        "user": {"id": 1},
        "created_at": "2020-01-01T00:00:00Z",
    }
    github = MagicMock(spec=GitHubClient)
    github.token = "test-token"
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda *_: dict(issue)
    github.get_parent_issue_details_strict.return_value = None
    github.get_direct_sub_issues_strict.return_value = []
    github.get_open_sub_issues_strict.return_value = []
    from types import SimpleNamespace

    github.get_open_entities_strict.return_value = SimpleNamespace(issues=[SimpleNamespace(number=9001)])
    github.get_issue_comments_strict.return_value = []
    github.get_connected_prs.return_value = []
    github.get_parent_issue_number_strict.return_value = None
    github.get_issue_hierarchy_generation_strict.return_value = "standalone-generation"

    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = [1]
    engine = AutomationEngine(github, config)
    slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    analyzer = MagicMock(return_value=SpecificationAnalysisResult("READY"))
    engine._specification_validators[REPO] = SpecificationValidationLifecycle(REPO, "test/model", tmp_path / "spec.json", analyzer)

    with patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult("issue", 9001, issue["title"], True, ["implementation reached"])):
        result = engine._process_single_candidate_unified(REPO, Candidate("issue", dict(issue), 0), config)

    assert result.success is True, result.error
    assert result.target_outcome is not ExplicitTargetOutcome.SKIPPED
    # The real controller path -- not a test calling the repository API
    # directly -- is what durably admitted this owner.
    owner = ImplementationOwner("issue", 9001)
    assert slots.active_owners() == (owner,)

    _mount_main(mock_ui, engine, REPO)
    refresh_slots = _slots_refresh_callback(mock_ui)
    asyncio.run(refresh_slots())

    links = _link_calls(mock_ui)
    assert ("Issue #9001", "/detail/issue/9001") in links
    labels = _label_texts(mock_ui)
    assert any("Normal: 1/1 used, 0 available" in text for text in labels)
