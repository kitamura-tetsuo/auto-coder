"""
Dashboard module for Auto-Coder using NiceGUI.
"""

import os

from fastapi import FastAPI
from nicegui import ui


def init_dashboard(app: FastAPI) -> None:
    """Initialize the dashboard and mount it to the FastAPI app."""

    @ui.page("/")
    def main_page() -> None:
        ui.label("Hello World").classes("text-2xl font-bold")

    # Mount NiceGUI at /dashboard
    # Note: When using mount_path, pages defined with '/' will be available at mount_path + '/'
    ui.run_with(
        app,
        mount_path="/dashboard",
        storage_secret=os.getenv("DASHBOARD_SECRET", "auto-coder-dashboard-secret"),
        title="Auto-Coder Dashboard",
    )
