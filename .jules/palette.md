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

## 2025-12-24 - Visual Anchors for Metadata
**Learning:** Pure text timers (e.g., `[1h 23m]`) blend into other bracketed metadata in the logs. Adding a semantic icon (⏱) creates an instant visual anchor, allowing users to scan for "how long has this been running?" without reading the text.
**Action:** Use distinct icons for different types of bracketed metadata (status vs. time vs. ID) to improve scanability.
