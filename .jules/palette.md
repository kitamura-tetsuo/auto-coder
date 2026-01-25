## 2025-05-20 - CLI Color Accessibility
**Learning:** The CLI uses hardcoded ANSI color codes in `ProgressFooter`, making it inaccessible for users who need high contrast or no color.
**Action:** Implemented `NO_COLOR` support. In future, ensure all CLI output respects `NO_COLOR` or uses a library that handles it.

## 2025-12-15 - Visual Hierarchy in CLI
**Learning:** Adding visual anchors (icons like 🔀, 🐛) and clear separators (›) significantly improves the scanability of dense CLI progress logs.
**Action:** Always pair icons with text labels and ensure a clean fallback for NO_COLOR environments.

## 2025-12-17 - Activity Indicators in Sync Operations
**Learning:** Even in a synchronous CLI environment, users need immediate feedback that the process hasn't hung. A simple tick-based spinner in the main loop of blocking operations (like command execution) provides this "heartbeat" without complex threading.
**Action:** When designing CLI tools, identify the main blocking loops and insert a lightweight "tick" mechanism to update UI indicators.

## 2025-12-18 - Time Perception in CLI
**Learning:** For long-running CLI processes, users lose track of time and may suspect a hang. Adding an explicit elapsed time counter provides reassurance and context without requiring user interaction.
**Action:** Include elapsed time indicators for any CLI operation expected to take more than a few seconds.

## 2025-12-19 - Information Density in CLI Startup
**Learning:** Displaying configuration as a dense "wall of text" makes it hard for users to verify their settings at a glance. Structured, aligned output with visual separation (colors/icons) drastically improves readability and confidence before a long-running process starts.
**Action:** When printing startup configuration, use a key-value alignment strategy and grouped summaries instead of sequential log lines.

## 2026-01-20 - Perceived Responsiveness in Countdowns
**Learning:** Countdowns that only update once per second can feel sluggish or unresponsive. Increasing the update frequency (e.g., to 10Hz) and adding a spinner animation maintains a sense of activity, making the wait feel shorter.
**Action:** For CLI countdowns, use a high-frequency loop to drive animations, even if the numeric countdown only updates every second.

## 2026-05-21 - Completion Summaries
**Learning:** After long-running CLI operations (like processing an issue or analyzing a repo), users benefit from a distinct, structured summary that separates the final result from the stream of logs. This provides closure and easy access to key information (e.g., status, errors, actions taken).
**Action:** Use `print_completion_message` (or similar) to standardize success/failure reporting, ensuring it stands out visually (using colors/emojis if supported) and handles list data cleanly.

## 2026-05-22 - Smart Progress Indicators
**Learning:** For variable-duration operations, showing a timer immediately adds visual noise, while showing nothing causes anxiety for slow operations. A "smart timer" that appears only after a threshold (e.g., 1s) balances cleanliness with reassurance.
**Action:** Implement progress indicators with a delay threshold to handle both fast and slow paths gracefully without UI flickering or clutter.

## 2026-10-25 - Context-Aware Completion Messages
**Learning:** A generic "Done" or "OK" after a CLI spinner is less informative than a specific success message (e.g. "✅ Credentials verified" vs "✅ Checking credentials..."). Updating the spinner to support dedicated success/error messages provides immediate, context-rich feedback.
**Action:** When using spinners, allow callers to specify distinct completion messages for success and failure states to replace the ongoing process description.
