"""
Dashboard module for Auto-Coder using NiceGUI.
"""

import os

from fastapi import FastAPI
from nicegui import ui

from .automation_engine import AutomationEngine


def init_dashboard(app: FastAPI, engine: AutomationEngine) -> None:
    """Initialize the dashboard and mount it to the FastAPI app."""

    @ui.page("/")
    def main_page() -> None:
        ui.label("Auto-Coder Dashboard").classes("text-2xl font-bold mb-4")

        # Active Workers Section
        ui.label("Active Workers").classes("text-xl font-bold mt-4")
        workers_container = ui.row().classes("w-full gap-4")

        # Queue Section
        ui.label("Queue").classes("text-xl font-bold mt-4")
        queue_container = ui.column().classes("w-full gap-2")

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
                                ui.label(
                                    f"{worker_data.get('type', '').capitalize()} #{worker_data.get('number')}"
                                )
                                ui.label(worker_data.get("title", "No Title")).classes(
                                    "text-sm text-gray-500 truncate"
                                )
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
                            ui.label(item.get("type", "").capitalize()).classes("w-20")
                            ui.label(f"#{item.get('number')}").classes("w-20")
                            ui.label(str(item.get("priority"))).classes("w-20")
                            ui.label(item.get("title", "")).classes(
                                "flex-grow truncate"
                            )

        # Initial load
        refresh_status()

        # Auto-refresh every 1 second
        ui.timer(1.0, refresh_status)

    # Mount NiceGUI at /dashboard
    # Note: When using mount_path, pages defined with '/' will be available at mount_path + '/'
    ui.run_with(
        app,
        mount_path="/dashboard",
        storage_secret=os.getenv("DASHBOARD_SECRET", "auto-coder-dashboard-secret"),
        title="Auto-Coder Dashboard",
    )
