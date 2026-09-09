"""
Dashboard module for Auto-Coder using NiceGUI.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from nicegui import ui

from .automation_engine import AutomationEngine
from .dashboard_detail import (
    DetailSelection,
    SelectionMode,
    build_observed_path_diagram,
    events_for_execution,
    evidence_rows,
    execution_finished_event,
    execution_start_event_present,
    executions_for_item,
    format_fact_value,
    is_resolvable_item_number,
    is_supported_item_type,
    resolve_selected_execution,
    unscoped_events_for_item,
)
from .execution_trace import get_trace_collector
from .trace_logger import get_trace_logger


def prepare_log_rows(logs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Format logs for display in the dashboard table, reversed (newest first)."""
    rows = []
    # Reverse logs to show newest first
    for log in reversed(logs):
        dt = datetime.fromtimestamp(log["timestamp"]).strftime("%H:%M:%S")
        rows.append(
            {
                "time": dt,
                "category": log["category"],
                "message": log["message"],
                "details": str(log.get("details", "")),
            }
        )
    return rows


def init_dashboard(app: FastAPI, engine: AutomationEngine, repo_name: str) -> None:
    """Initialize the dashboard and mount it to the FastAPI app.

    ``repo_name`` is the single repository this daemon process (and hence
    this dashboard) is scoped to; detail views select diagnostic evidence
    for exactly that repository (REQ-001) rather than any repository an
    incoming request happens to name.
    """

    @ui.page("/")
    def main_page() -> None:
        ui.label("Auto-Coder Dashboard").classes("text-2xl font-bold mb-4")

        # Search Section
        ui.label("Search").classes("text-xl font-bold mt-4")
        with ui.row().classes("gap-2 items-center"):
            search_type = ui.select(["pr", "issue"], value="pr", label="Type").classes("w-32")
            search_number = ui.number(label="Number", value=1, format="%.0f").classes("w-32")
            ui.button("Go", on_click=lambda: ui.navigate.to(f"/detail/{search_type.value}/{int(search_number.value)}"))

        # Active Workers Section
        ui.label("Active Workers").classes("text-xl font-bold mt-4")
        workers_container = ui.row().classes("w-full gap-4")

        # Queue Section
        ui.label("Queue").classes("text-xl font-bold mt-4")
        queue_container = ui.column().classes("w-full gap-2")

        # Open Issues/PRs Section
        ui.label("Open Issues/PRs").classes("text-xl font-bold mt-4")
        open_items_container = ui.column().classes("w-full gap-2")

        def refresh_status() -> None:
            status = engine.get_status()

            # Update Workers
            workers_container.clear()
            with workers_container:
                active_workers = status.get("active_workers", {})
                if not active_workers:
                    ui.label("No active workers")
                else:
                    for wid, worker_data in active_workers.items():
                        with ui.card().classes("w-64"):
                            ui.label(f"Worker {wid}").classes("font-bold")
                            if worker_data:
                                item_type = worker_data.get("type", "")
                                item_number = worker_data.get("number")
                                ui.link(
                                    f"{item_type.capitalize()} #{item_number}",
                                    f"/detail/{item_type}/{item_number}",
                                ).classes("text-blue-500 font-bold")
                                ui.label(worker_data.get("title", "No Title")).classes("text-sm text-gray-500 truncate")
                            else:
                                ui.label("Idle").classes("text-gray-400")

            # Update Queue
            queue_container.clear()
            with queue_container:
                queue_items = status.get("queue_items", [])
                if not queue_items:
                    ui.label("Queue is empty")
                else:
                    # Header for queue table
                    with ui.row().classes("w-full font-bold border-b"):
                        ui.label("Type").classes("w-20")
                        ui.label("Number").classes("w-20")
                        ui.label("Priority").classes("w-20")
                        ui.label("Title").classes("flex-grow")

                    for item in queue_items:
                        with ui.row().classes("w-full border-b py-2 items-center"):
                            item_type = item.get("type", "")
                            item_number = item.get("number")
                            ui.label(item_type.capitalize()).classes("w-20")
                            ui.link(f"#{item_number}", f"/detail/{item_type}/{item_number}").classes("w-20 text-blue-500")
                            ui.label(str(item.get("priority"))).classes("w-20")
                            ui.label(item.get("title", "")).classes("flex-grow truncate")

            # Update Open Issues/PRs
            open_items_container.clear()
            with open_items_container:
                open_items = status.get("open_items", [])
                if not open_items:
                    ui.label("No open issues or PRs found")
                else:
                    # Header for table
                    with ui.row().classes("w-full font-bold border-b"):
                        ui.label("Type").classes("w-20")
                        ui.label("Number").classes("w-20")
                        ui.label("Status").classes("w-48")
                        ui.label("Created At").classes("w-48")
                        ui.label("Title").classes("flex-grow")

                    for item in open_items:
                        with ui.row().classes("w-full border-b py-2 items-center"):
                            item_type = item.get("type", "")
                            item_number = item.get("number")
                            status_str = item.get("status", "Unknown")
                            created_at = item.get("created_at", "")
                            # Format created_at slightly nicer if it's ISO
                            if created_at and "T" in created_at:
                                try:
                                    created_at = created_at.split("T")[0]  # Just date
                                except Exception:
                                    pass

                            ui.label(item_type.capitalize()).classes("w-20")
                            ui.link(f"#{item_number}", f"/detail/{item_type}/{item_number}").classes("w-20 text-blue-500")

                            # Colorize status
                            status_color = "text-black"
                            if "Processing" in status_str:
                                status_color = "text-green-600 font-bold"
                            elif "Queued" in status_str:
                                status_color = "text-blue-600"

                            ui.label(status_str).classes(f"w-48 {status_color}")
                            ui.label(created_at).classes("w-48 text-sm text-gray-500")
                            ui.label(item.get("title", "")).classes("flex-grow truncate")

        # Initial load
        refresh_status()

        # Auto-refresh every 1 second
        ui.timer(1.0, refresh_status)

    @ui.page("/detail/{item_type}/{item_number}")
    def detail_page(item_type: str, item_number: int) -> None:
        ui.label(f"Detail View: {item_type.capitalize()} #{item_number}").classes("text-2xl font-bold mb-4")

        # Back button
        ui.link("Back to Dashboard", "/").classes("text-blue-500 mb-4 inline-block")

        if not is_supported_item_type(item_type) or not is_resolvable_item_number(item_number):
            ui.label(f"Invalid or unresolved target: {item_type!r} #{item_number}. " "This repository's diagnostic evidence cannot be selected for it.").classes("text-red-600 font-bold")
            return

        ui.label(f"Repository: {repo_name}").classes("text-sm text-gray-500 mb-2")

        status_banner = ui.label("Loading...").classes("text-sm text-gray-500 mb-2")

        # Navigation container (Follow latest / older / newer execution)
        navigation_container = ui.row().classes("w-full items-center mb-2 gap-2")

        # Execution identity/origin header
        identity_container = ui.column().classes("w-full mb-4 gap-0")

        # Activity Diagram container
        diagram_container = ui.row().classes("w-full mb-6")

        # Observed-evidence panel container
        evidence_container = ui.column().classes("w-full mb-6")

        # Decision log (all events for the selected execution)
        logs_container = ui.column().classes("w-full mb-6")

        # Unscoped/legacy diagnostic evidence, kept visually separate and
        # never attributed to any execution (REQ-007).
        legacy_container = ui.column().classes("w-full")

        state: Dict[str, Any] = {"mode": SelectionMode.FOLLOW_LATEST, "pinned_execution_id": None, "refreshing": False, "last_refresh_ok_at": None}
        mermaid_holder: Dict[str, str] = {"code": ""}
        # Rendered-projection cache and persistent element handles (issue #1978).
        # A periodic refresh that observes the same projection as last time
        # must not touch the DOM at all (REQ-002/REQ-004): every section below
        # is only cleared/rebuilt when its own signature changes, and rows/
        # content for unchanged-shape tables and the Mermaid diagram are
        # patched in place via NiceGUI's reactive props rather than recreated,
        # which is what keeps scroll position, pagination, and Follow/pinned
        # selection stable across ticks (REQ-003).
        render_cache: Dict[str, Any] = {}
        elements: Dict[str, Any] = {}

        def pin(execution_id: str) -> None:
            state["mode"] = SelectionMode.PINNED
            state["pinned_execution_id"] = execution_id
            refresh_details()

        def follow_latest() -> None:
            state["mode"] = SelectionMode.FOLLOW_LATEST
            state["pinned_execution_id"] = None
            refresh_details()

        def refresh_details() -> None:
            # A plain synchronous callback cannot itself be re-entered by
            # this single-threaded client event loop, but the guard makes
            # that guarantee explicit rather than implicit (REQ-008): a
            # refresh already in flight is never superseded by a nested one.
            if state["refreshing"]:
                return
            state["refreshing"] = True
            try:
                try:
                    snapshot = get_trace_collector().get_snapshot(item_type=item_type, item_number=item_number, repository=repo_name)
                except Exception:
                    stale_at = state["last_refresh_ok_at"] or "never"
                    status_banner.set_text(f"Snapshot read failed; showing data as of last successful local refresh ({stale_at}).")
                    status_banner.classes(replace="text-sm text-red-600 font-bold mb-2")
                    return

                state["last_refresh_ok_at"] = datetime.now().strftime("%H:%M:%S")
                status_banner.set_text(f"Local snapshot as of {state['last_refresh_ok_at']} (not live GitHub/provider state).")
                status_banner.classes(replace="text-sm text-gray-500 mb-2")

                executions = executions_for_item(snapshot, repo_name, item_type, item_number)
                selection: DetailSelection = resolve_selected_execution(executions, state["mode"], state["pinned_execution_id"])
                selected_events: List[Any] = []
                if selection.execution is not None:
                    selected_events = events_for_execution(snapshot, selection.execution.execution_id)

                following = state["mode"] is SelectionMode.FOLLOW_LATEST
                current_index: Optional[int] = None
                if selection.execution is not None:
                    for i, summary in enumerate(executions):
                        if summary.execution_id == selection.execution.execution_id:
                            current_index = i
                            break

                # --- Navigation (Follow latest / older / newer execution) ---
                nav_sig = (following, selection.pinned_evicted, current_index, tuple(e.execution_id for e in executions))
                if nav_sig != render_cache.get("nav_sig"):
                    navigation_container.clear()
                    with navigation_container:
                        ui.button("Follow latest", on_click=follow_latest).props(f"dense {'flat' if not following else 'unelevated'}")

                        def pin_relative(offset: int, index: Optional[int] = current_index) -> None:
                            if index is None:
                                return
                            target = index + offset
                            if 0 <= target < len(executions):
                                pin(executions[target].execution_id)

                        btn_older = ui.button(icon="arrow_downward", on_click=lambda: pin_relative(-1)).props("dense flat").tooltip("Older execution")
                        if current_index is None or current_index <= 0:
                            btn_older.disable()

                        if selection.pinned_evicted:
                            ui.label("Pinned execution no longer retained").classes("font-bold text-red-600")
                        elif current_index is not None:
                            ui.label(f"Execution {current_index + 1} of {len(executions)} ({'following latest' if following else 'pinned'})").classes("font-bold")
                        else:
                            ui.label("No retained executions").classes("font-bold text-gray-500")

                        btn_newer = ui.button(icon="arrow_upward", on_click=lambda: pin_relative(1)).props("dense flat").tooltip("Newer execution")
                        if current_index is None or current_index >= len(executions) - 1:
                            btn_newer.disable()
                    render_cache["nav_sig"] = nav_sig

                # --- Execution identity/origin header ---
                started_present = execution_start_event_present(selected_events) if selection.execution is not None else None
                finished_event = execution_finished_event(selected_events) if selection.execution is not None else None
                identity_sig = (
                    snapshot.events_truncated,
                    selection.pinned_evicted,
                    selection.pinned_execution_id if selection.pinned_evicted else None,
                    selection.execution.execution_id if selection.execution is not None else None,
                    snapshot.process_run_id if selection.execution is not None else None,
                    started_present,
                    (finished_event.outcome, finished_event.timestamp) if finished_event is not None else None,
                )
                if identity_sig != render_cache.get("identity_sig"):
                    identity_container.clear()
                    with identity_container:
                        if snapshot.events_truncated:
                            ui.label("Note: retained diagnostic history has been truncated by bounded retention; earlier events (for this and other items) may be missing.").classes("text-sm text-amber-600")

                        if selection.pinned_evicted:
                            ui.label(f"The pinned execution {selection.pinned_execution_id!r} is no longer retained locally. " "This does not mean it never existed or that it failed; its evidence has simply been evicted.").classes("text-red-600")
                        elif selection.execution is None:
                            ui.label("No execution has been observed locally for this item in this process run. " "This does not indicate there was never earlier work, nor that any remote work has finished.").classes("text-gray-500")
                        else:
                            summary = selection.execution
                            ui.label(f"Execution: {summary.execution_id}").classes("font-mono text-sm")
                            ui.label(f"Process run: {snapshot.process_run_id}").classes("font-mono text-sm text-gray-500")
                            if not started_present:
                                ui.label("Start evidence for this execution has been evicted; this history is partially retained (incomplete).").classes("text-sm text-amber-600")
                            if finished_event is not None:
                                ui.label(f"Completion: {finished_event.outcome or 'unknown'} (observed {datetime.fromtimestamp(finished_event.timestamp).strftime('%Y-%m-%d %H:%M:%S')})").classes("text-sm")
                            else:
                                ui.label("Completion: not yet recorded for this execution.").classes("text-sm text-gray-500")
                    render_cache["identity_sig"] = identity_sig

                # --- Activity diagram: patched in place via Mermaid's reactive
                # `content` prop (no clear/rebuild) whenever only the diagram
                # text changes, so the diagram never flickers on an ordinary
                # tick; a full rebuild only happens the first time or when it
                # toggles between "no events" and "has events" (REQ-004).
                mermaid_code = build_observed_path_diagram(selected_events)
                mermaid_holder["code"] = mermaid_code
                diagram_kind = "code" if mermaid_code else "empty"
                if diagram_kind != render_cache.get("diagram_kind"):

                    def copy_mermaid() -> None:
                        # Reads the holder at click time, never a value captured
                        # by an earlier refresh, so this always copies the
                        # diagram currently displayed for the selected execution.
                        ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(mermaid_holder['code'])})")
                        ui.notify("Copied!")

                    diagram_container.clear()
                    with diagram_container:
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.label("Processing Path (observed order, not asserted control flow)").classes("text-xl font-bold")
                            if mermaid_code:
                                ui.button(icon="content_copy", on_click=copy_mermaid).props("flat round dense").tooltip("Copy Mermaid Code")

                        if mermaid_code:
                            elements["mermaid"] = ui.mermaid(mermaid_code).classes("w-full bg-white p-4 rounded shadow")
                        else:
                            ui.label("No observed events for this execution.")
                    render_cache["diagram_kind"] = diagram_kind
                    render_cache["mermaid_code"] = mermaid_code
                elif mermaid_code and mermaid_code != render_cache.get("mermaid_code"):
                    elements["mermaid"].set_content(mermaid_code)
                    render_cache["mermaid_code"] = mermaid_code

                # --- Observed evidence table: rows patched in place (no
                # clear/rebuild) whenever the section already shows a table,
                # so the QTable's own client-side pagination state is never
                # reset by an update that merely appends/changes rows
                # (REQ-002, REQ-003, AS-005).
                evidence = evidence_rows(selected_events)
                evidence_has_rows = bool(evidence)
                if evidence_has_rows != render_cache.get("evidence_has_rows"):
                    evidence_container.clear()
                    with evidence_container:
                        ui.label("Observed Evidence (local diagnostics only, not live GitHub/provider state)").classes("text-xl font-bold mb-2")
                        if evidence:
                            columns = [
                                {"name": "time", "label": "Observed At", "field": "time", "align": "left"},
                                {"name": "stage", "label": "Stage", "field": "stage", "align": "left"},
                                {"name": "outcome", "label": "Outcome", "field": "outcome", "align": "left"},
                                {"name": "facts", "label": "Facts", "field": "facts", "align": "left"},
                            ]
                            elements["evidence_table"] = ui.table(columns=columns, rows=evidence, pagination=10).classes("w-full")
                        else:
                            ui.label("No evidence facts recorded for this execution.")
                    render_cache["evidence_has_rows"] = evidence_has_rows
                    render_cache["evidence_rows"] = evidence
                elif evidence != render_cache.get("evidence_rows"):
                    elements["evidence_table"].rows = evidence
                    render_cache["evidence_rows"] = evidence

                # --- Decision log table: same in-place row patching as evidence. ---
                log_rows = []
                for event in reversed(selected_events):  # newest-first (REQ-009)
                    fact_lines = [f"{k}: {format_fact_value(v)}" for k, v in sorted((event.facts or {}).items())]
                    log_rows.append(
                        {
                            "time": datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S"),
                            "stage": event.label or event.stage_id,
                            "kind": event.kind,
                            "outcome": event.outcome or "unknown",
                            "facts": "\n".join(fact_lines) if fact_lines else "",
                        }
                    )
                logs_has_rows = bool(log_rows)
                if logs_has_rows != render_cache.get("logs_has_rows"):
                    logs_container.clear()
                    with logs_container:
                        ui.label("Decision Log").classes("text-xl font-bold mb-2")
                        if log_rows:
                            columns = [
                                {"name": "time", "label": "Time", "field": "time", "align": "left"},
                                {"name": "stage", "label": "Stage", "field": "stage", "align": "left"},
                                {"name": "kind", "label": "Kind", "field": "kind", "align": "left"},
                                {"name": "outcome", "label": "Outcome", "field": "outcome", "align": "left"},
                                {"name": "facts", "label": "Facts", "field": "facts", "align": "left"},
                            ]
                            elements["logs_table"] = ui.table(columns=columns, rows=log_rows, pagination=10).classes("w-full")
                        else:
                            ui.label("No events recorded for this execution.")
                    render_cache["logs_has_rows"] = logs_has_rows
                    render_cache["log_rows"] = log_rows
                elif log_rows != render_cache.get("log_rows"):
                    elements["logs_table"].rows = log_rows
                    render_cache["log_rows"] = log_rows

                # --- Unscoped/legacy diagnostic evidence: rebuilt only when a
                # table appears/disappears; rows patched in place otherwise. ---
                unscoped = unscoped_events_for_item(snapshot, repo_name, item_type, item_number)
                legacy_raw = get_trace_logger().get_logs(item_type=item_type, item_number=item_number, limit=500)
                unscoped_rows = [
                    {
                        "time": datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S"),
                        "stage": e.label or e.stage_id,
                        "outcome": e.outcome or "unknown",
                        "supported": "supported" if e.supported else "unsupported schema",
                    }
                    for e in reversed(unscoped)
                ]
                legacy_raw_rows = prepare_log_rows(legacy_raw)
                legacy_shape = (bool(unscoped_rows), bool(legacy_raw_rows))
                if legacy_shape != render_cache.get("legacy_shape"):
                    legacy_container.clear()
                    with legacy_container:
                        if unscoped_rows or legacy_raw_rows:
                            ui.label("Unscoped / legacy diagnostic evidence (not attributed to any execution)").classes("text-lg font-bold mb-2")
                        if unscoped_rows:
                            columns = [
                                {"name": "time", "label": "Time", "field": "time", "align": "left"},
                                {"name": "stage", "label": "Stage", "field": "stage", "align": "left"},
                                {"name": "outcome", "label": "Outcome", "field": "outcome", "align": "left"},
                                {"name": "supported", "label": "Schema", "field": "supported", "align": "left"},
                            ]
                            elements["unscoped_table"] = ui.table(columns=columns, rows=unscoped_rows, pagination=10).classes("w-full mb-4")
                        if legacy_raw_rows:
                            ui.label("Legacy raw diagnostic text (pre-migration; inert text only)").classes("text-md font-bold mb-2")
                            columns = [
                                {"name": "time", "label": "Time", "field": "time", "align": "left"},
                                {"name": "category", "label": "Category", "field": "category", "align": "left"},
                                {"name": "message", "label": "Message", "field": "message", "align": "left"},
                                {"name": "details", "label": "Details", "field": "details", "align": "left"},
                            ]
                            elements["legacy_table"] = ui.table(columns=columns, rows=legacy_raw_rows, pagination=10).classes("w-full")
                    render_cache["legacy_shape"] = legacy_shape
                    render_cache["unscoped_rows"] = unscoped_rows
                    render_cache["legacy_raw_rows"] = legacy_raw_rows
                else:
                    if unscoped_rows and unscoped_rows != render_cache.get("unscoped_rows"):
                        elements["unscoped_table"].rows = unscoped_rows
                        render_cache["unscoped_rows"] = unscoped_rows
                    if legacy_raw_rows and legacy_raw_rows != render_cache.get("legacy_raw_rows"):
                        elements["legacy_table"].rows = legacy_raw_rows
                        render_cache["legacy_raw_rows"] = legacy_raw_rows
            finally:
                state["refreshing"] = False

        refresh_details()

        # Auto-refresh every 1 second while connected; NiceGUI cancels
        # page-scoped timers automatically on client disconnect, and this
        # explicit hook makes that stop deterministic (REQ-008).
        detail_timer = ui.timer(1.0, refresh_details)
        ui.context.client.on_disconnect(detail_timer.deactivate)

    # Mount NiceGUI at /dashboard
    # Note: When using mount_path, pages defined with '/' will be available at mount_path + '/'
    ui.run_with(
        app,
        mount_path="/dashboard",
        storage_secret=os.getenv("DASHBOARD_SECRET", "auto-coder-dashboard-secret"),
        title="Auto-Coder Dashboard",
    )
