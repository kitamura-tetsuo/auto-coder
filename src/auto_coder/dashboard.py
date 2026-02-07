"""
Dashboard module for Auto-Coder using NiceGUI.
"""

import os
from datetime import datetime

from fastapi import FastAPI
from nicegui import ui

from .automation_engine import AutomationEngine
from .trace_logger import get_trace_logger


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
            ui.button("Go", on_click=lambda: ui.open(f"/detail/{search_type.value}/{int(search_number.value)}"))

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
                            ui.link(f"#{item_number}", f"/detail/{item_type}/{item_number}").classes(
                                "w-20 text-blue-500"
                            )
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
                            ui.link(f"#{item_number}", f"/detail/{item_type}/{item_number}").classes(
                                "w-20 text-blue-500"
                            )

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

        # Metrics container
        metrics_container = ui.row().classes("w-full gap-4 mb-6")

        # Logs container
        logs_container = ui.column().classes("w-full")

        def refresh_details():
            metrics_container.clear()
            logs_container.clear()

            logs = get_trace_logger().get_logs(item_type=item_type, item_number=item_number)

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
                    rows = []
                    for log in logs:
                        dt = datetime.fromtimestamp(log["timestamp"]).strftime("%H:%M:%S")
                        rows.append({"time": dt, "category": log["category"], "message": log["message"], "details": str(log.get("details", ""))})

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
