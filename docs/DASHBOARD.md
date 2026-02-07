# Auto-Coder Dashboard

The Auto-Coder Dashboard provides a real-time visualization of the automation engine's activities, including the queue status, active workers, and detailed logs for processed items (Issues and Pull Requests).

## Enabling the Dashboard

The dashboard is integrated into the Auto-Coder daemon, which can be started in two ways:

### 1. Using `process-issues` with Webhook

By default, the `process-issues` command enables the webhook server, which also hosts the dashboard.

```bash
auto-coder process-issues --repo owner/repo --enable-webhook
```

To disable the dashboard (and webhook server), use:

```bash
auto-coder process-issues --repo owner/repo --disable-webhook
```

### 2. Using `serve` Command

You can run the Auto-Coder daemon explicitly using the `serve` command:

```bash
auto-coder serve --repo owner/repo
```

This command starts the FastAPI server hosting both the webhook endpoints and the dashboard.

## Accessing the Dashboard

Once the daemon is running, the dashboard is accessible at:

**[http://localhost:8000/dashboard/](http://localhost:8000/dashboard/)**

(Default port is 8000, but can be configured via `--port` option)

## Dashboard Features

### Main View

The main dashboard view provides an overview of the current system state:

*   **Search Section**: Allows quick navigation to the detail view of a specific Issue or PR. Select the type (PR/Issue) and enter the number, then click "Go".
*   **Active Workers**: Displays the currently active worker tasks. Each card shows the worker ID, the item being processed (with a link to details), and the current task description.
*   **Queue**: Lists pending items in the processing queue. The table shows the item type, number, priority, and title.

### Detail View

Clicking on an item in the Active Workers or Queue list (or using the Search) takes you to the Detail View for that specific Issue or PR.

The Detail View provides:

*   **Metrics**:
    *   **Mergeability**: Indicates if the PR is mergeable (True/False/Unknown).
    *   **CI Status**: Shows the status of CI checks (Success/Failure/In Progress).
*   **Decision Log**: A chronological log of decisions and actions taken by the automation engine for this item. This includes:
    *   Time of the event.
    *   Category (e.g., Merge Check, CI Status, Decision).
    *   Message description.
    *   Detailed JSON data associated with the event.

## Configuration

The dashboard uses NiceGUI and FastAPI. You can configure the host and port via CLI arguments:

*   `--host`: Host to bind the server to (default: 0.0.0.0).
*   `--port`: Port to bind the server to (default: 8000).

Example:

```bash
auto-coder serve --port 8080
```

Then access at `http://localhost:8080/dashboard/`.
