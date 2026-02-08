"""
Dashboard module for Auto-Coder using NiceGUI.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from fastapi import FastAPI
from nicegui import ui

from .automation_engine import AutomationEngine
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


def generate_activity_diagram(logs: List[Dict[str, Any]], item_type: str) -> str:
    """Generate Mermaid activity diagram based on logs."""

    # Define base diagrams
    if item_type == "pr":
        graph = """
        graph TD
            Start[Start PR Processing]
            CheckLabel{Check @auto-coder}
            CheckWaitJules{Waiting for Jules?}
            LinkJules[Link Jules PR]
            CheckCI{Check CI Status}

            %% CI Status Branches
            CheckCI -- In Progress --> End[End Processing]
            CheckCI -- No Runs --> TriggerCI[Trigger Workflow]
            TriggerCI --> MonitorCI[Start Monitor] --> End
            CheckCI -- Success --> CheckMerge{Check Mergeability}
            CheckCI -- Failure --> CheckJules{Is Jules PR?}

            %% Merge Path
            CheckMerge -- Mergeable --> Merge[Merge PR]
            CheckMerge -- Not Mergeable --> Remediate{Remediation}

            Merge -- Success --> Cleanup[Cleanup & Archive] --> End
            Merge -- Failure --> End

            %% Remediation Path
            Remediate -- Update Base --> CheckConflict{Conflict?}
            CheckConflict -- Resolved --> PushUpdate[Push Update] --> End
            CheckConflict -- Degrading --> ClosePR[Close PR] --> End
            CheckConflict -- Failed --> End

            %% Failure Path
            CheckJules -- Yes --> Feedback[Send Jules Feedback] --> End
            CheckJules -- No --> UpdateBase{Update Base Branch}

            UpdateBase -- Pushed --> End
            UpdateBase -- UpToDate/Skipped --> FixIssues[Fix Issues]
            FixIssues -- GHA Logs --> CommitFix[Commit Fix]
            FixIssues -- Local Tests --> CommitFix
            CommitFix --> End

            %% Early Exits
            Start --> CheckLabel
            CheckLabel -- Yes --> End
            CheckLabel -- No --> CheckWaitJules
            CheckWaitJules -- Yes --> End
            CheckWaitJules -- No --> LinkJules
            LinkJules --> CheckCI
        """
    elif item_type == "issue":
        graph = """
        graph TD
            Start[Start Issue Processing]
            CheckLabel{Check @auto-coder}

            CheckLabel -- Yes --> End[End Processing]
            CheckLabel -- No --> CheckType{Issue Type}

            %% Parent Issue Path
            CheckType -- Parent Issue --> CreateParentPR[Create Parent PR] --> End

            %% Regular Issue Path
            CheckType -- Regular --> CheckMode{Processing Mode}

            %% Jules Mode
            CheckMode -- Jules --> StartSession[Start Jules Session]
            StartSession --> Comment[Comment & Label] --> End

            %% Direct Mode
            CheckMode -- Direct --> BranchSetup[Branch Setup]
            BranchSetup --> Analyze[Analyze Issue]
            Analyze -- Success --> Apply[Apply Changes]
            Analyze -- Fail --> End
            Apply --> CreatePR[Create PR] --> End

            Start --> CheckLabel
        """
    else:
        return ""

    # Map logs to nodes to highlight
    visited_nodes = set()
    visited_nodes.add("Start")

    # Track state for context-dependent nodes
    is_jules_pr = False
    is_remediating = False

    for log in logs:
        cat = log["category"]
        details = log.get("details", {})

        # Ensure details is a dict (sometimes it might be a string in older logs, though code ensures dict)
        if isinstance(details, str):
            try:
                details = json.loads(details.replace("'", '"'))
            except:
                details = {}

        if item_type == "pr":
            if cat == "PR Processing":
                visited_nodes.add("CheckLabel")
                visited_nodes.add("CheckWaitJules")
                if details.get("skip_reason") == "already_processed":
                    visited_nodes.add("End")
                elif details.get("skip_reason") == "waiting_for_jules":
                    visited_nodes.add("End")

            elif cat == "Jules Link":
                visited_nodes.add("LinkJules")
                is_jules_pr = details.get("success", False)

            elif cat == "CI Status":
                visited_nodes.add("CheckCI")
                if details.get("in_progress"):
                    visited_nodes.add("End")
                elif details.get("success"):
                    pass  # Path goes to CheckMerge
                else:
                    visited_nodes.add("CheckJules")  # Path goes to CheckJules

            elif cat == "CI Trigger":
                if details.get("monitor"):
                    visited_nodes.add("MonitorCI")
                    visited_nodes.add("End")
                else:
                    visited_nodes.add("TriggerCI")

            elif cat == "Merge Check":
                visited_nodes.add("CheckMerge")

            elif cat == "Remediation":
                is_remediating = True
                visited_nodes.add("Remediate")
                state = details.get("state")
                result = details.get("result")
                step = details.get("step")

                if step == "update_base":
                    pass  # Arrow logic handles this visually in static graph

                if result == "success":
                    visited_nodes.add("CheckConflict")
                    visited_nodes.add("PushUpdate")
                    visited_nodes.add("End")
                elif result == "degrading":
                    visited_nodes.add("CheckConflict")
                    visited_nodes.add("ClosePR")
                    visited_nodes.add("End")
                elif result == "failed":
                    visited_nodes.add("CheckConflict")
                    visited_nodes.add("End")

            elif cat == "Merging":
                visited_nodes.add("Merge")
                visited_nodes.add("Cleanup")
                visited_nodes.add("End")

            elif cat == "Jules Feedback":
                visited_nodes.add("Feedback")
                visited_nodes.add("End")

            elif cat == "Update Base":
                if not is_remediating:
                    visited_nodes.add("UpdateBase")
                    result = details.get("result")
                    if result == "pushed":
                        visited_nodes.add("End")
                    elif result in ["skipped", "up_to_date"]:
                        visited_nodes.add("FixIssues")

            elif cat == "Fixing Issues":
                visited_nodes.add("FixIssues")
                visited_nodes.add("CommitFix")  # Assumption based on flow
                visited_nodes.add("End")

            elif cat == "Decision":
                visited_nodes.add("End")

        elif item_type == "issue":
            if cat == "Skip":
                visited_nodes.add("CheckLabel")
                visited_nodes.add("End")

            elif cat == "Issue Type":
                visited_nodes.add("CheckLabel")
                visited_nodes.add("CheckType")
                if details.get("is_parent"):
                    visited_nodes.add("CreateParentPR")
                    visited_nodes.add("End")

            elif cat == "Dispatch":
                visited_nodes.add("CheckLabel")
                visited_nodes.add("CheckType")
                visited_nodes.add("CheckMode")
                mode = details.get("mode")
                if mode == "jules":
                    visited_nodes.add("StartSession")
                elif mode == "local":
                    visited_nodes.add("BranchSetup")

            elif cat == "Jules Session":
                visited_nodes.add("StartSession")
                visited_nodes.add("Comment")
                visited_nodes.add("End")

            elif cat == "Branch Setup":
                visited_nodes.add("BranchSetup")

            elif cat == "Analysis Start":
                visited_nodes.add("Analyze")

            elif cat == "Apply Changes":
                visited_nodes.add("Apply")

            elif cat == "Create PR":
                visited_nodes.add("CreatePR")
                visited_nodes.add("End")

    # Add styles
    style_def = "\n    classDef visited fill:#4ade80,stroke:#16a34a,stroke-width:2px;\n"
    for node in visited_nodes:
        style_def += f"    class {node} visited;\n"

    return graph + style_def


def group_logs_by_session(logs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group logs into sessions based on Queue and Worker events."""
    sessions = []
    current_session = []

    for log in logs:
        is_worker_start = log.get("category") == "Worker" and "started processing" in log.get("message", "")
        is_queue = log.get("category") == "Queue"

        should_split = False
        if is_queue:
            should_split = True
        elif is_worker_start:
            # Split if current session already has a worker start marker
            if any(l.get("category") == "Worker" and "started processing" in l.get("message", "") for l in current_session):
                should_split = True

        if should_split and current_session:
            sessions.append(current_session)
            current_session = []

        current_session.append(log)

    if current_session:
        sessions.append(current_session)

    return sessions


def init_dashboard(app: FastAPI, engine: AutomationEngine) -> None:
    """Initialize the dashboard and mount it to the FastAPI app."""

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

        # Navigation container
        navigation_container = ui.row().classes("w-full items-center mb-4 gap-2")

        # Activity Diagram container
        diagram_container = ui.row().classes("w-full mb-6")

        # Metrics container
        metrics_container = ui.row().classes("w-full gap-4 mb-6")

        # Logs container
        logs_container = ui.column().classes("w-full")

        # State to track current session index
        session_state = {"index": -1}

        def refresh_details():
            metrics_container.clear()
            logs_container.clear()
            diagram_container.clear()
            navigation_container.clear()

            all_logs = get_trace_logger().get_logs(item_type=item_type, item_number=item_number, limit=5000)
            sessions = group_logs_by_session(all_logs)

            if not sessions:
                with logs_container:
                    ui.label("No logs found.")
                return

            # Initialize or clamp index
            if session_state["index"] == -1:
                session_state["index"] = len(sessions) - 1

            if session_state["index"] >= len(sessions):
                session_state["index"] = len(sessions) - 1
            if session_state["index"] < 0:
                session_state["index"] = 0

            logs = sessions[session_state["index"]]

            # Extract metrics
            mergeability = "Unknown"
            ci_status = "Unknown"

            for log in logs:
                if log["category"] == "Merge Check":
                    details = log.get("details", {})
                    mergeability = str(details.get("mergeable", "Unknown"))
                if log["category"] == "CI Status":
                    details = log.get("details", {})
                    success = details.get("success")
                    in_progress = details.get("in_progress")
                    if in_progress:
                        ci_status = "In Progress"
                    else:
                        ci_status = "Success" if success else "Failure"

            # Render Navigation
            with navigation_container:

                def go_older():
                    session_state["index"] -= 1
                    refresh_details()

                def go_newer():
                    session_state["index"] += 1
                    refresh_details()

                btn_older = ui.button(icon="arrow_downward", on_click=go_older).props("dense flat").tooltip("Older Session")
                if session_state["index"] <= 0:
                    btn_older.disable()

                ui.label(f"Session {session_state['index'] + 1} of {len(sessions)}").classes("font-bold")

                btn_newer = ui.button(icon="arrow_upward", on_click=go_newer).props("dense flat").tooltip("Newer Session")
                if session_state["index"] >= len(sessions) - 1:
                    btn_newer.disable()

                # Show timestamp range if available
                if logs:
                    start_ts = logs[0].get("timestamp")
                    if start_ts:
                        start_dt = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M:%S")
                        ui.label(f"Start: {start_dt}").classes("text-sm text-gray-500 ml-4")

            with diagram_container:
                mermaid_code = generate_activity_diagram(logs, item_type)
                with ui.row().classes("items-center gap-2 mb-2"):
                    ui.label("Processing Path").classes("text-xl font-bold")
                    if mermaid_code:
                        ui.button(
                            icon="content_copy",
                            on_click=lambda: (
                                ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(mermaid_code)})"),
                                ui.notify("Copied!"),
                            ),
                        ).props(
                            "flat round dense"
                        ).tooltip("Copy Mermaid Code")

                if mermaid_code:
                    ui.mermaid(mermaid_code).classes("w-full bg-white p-4 rounded shadow")
                else:
                    ui.label("No diagram available for this item type.")

            with metrics_container:
                with ui.card():
                    ui.label("Mergeability").classes("text-sm text-gray-500")
                    color = "text-green-500" if mergeability == "True" else "text-red-500" if mergeability == "False" else "text-gray-500"
                    ui.label(mergeability).classes(f"text-xl font-bold {color}")

                with ui.card():
                    ui.label("CI Status").classes("text-sm text-gray-500")
                    color = "text-green-500" if ci_status == "Success" else "text-red-500" if ci_status == "Failure" else "text-yellow-500"
                    ui.label(ci_status).classes(f"text-xl font-bold {color}")

            with logs_container:
                ui.label("Decision Log").classes("text-xl font-bold mb-2")
                if not logs:
                    ui.label("No logs found.")
                else:
                    # Table
                    columns = [
                        {"name": "time", "label": "Time", "field": "time", "align": "left"},
                        {"name": "category", "label": "Category", "field": "category", "align": "left"},
                        {"name": "message", "label": "Message", "field": "message", "align": "left"},
                        {"name": "details", "label": "Details", "field": "details", "align": "left"},
                    ]
                    rows = prepare_log_rows(logs)

                    ui.table(columns=columns, rows=rows, pagination=10).classes("w-full")

        refresh_details()

    # Mount NiceGUI at /dashboard
    # Note: When using mount_path, pages defined with '/' will be available at mount_path + '/'
    ui.run_with(
        app,
        mount_path="/dashboard",
        storage_secret=os.getenv("DASHBOARD_SECRET", "auto-coder-dashboard-secret"),
        title="Auto-Coder Dashboard",
    )
