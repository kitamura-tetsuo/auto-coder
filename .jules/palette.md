## 2025-05-20 - CLI Color Accessibility
**Learning:** The CLI uses hardcoded ANSI color codes in `ProgressFooter`, making it inaccessible for users who need high contrast or no color.
**Action:** Implemented `NO_COLOR` support. In future, ensure all CLI output respects `NO_COLOR` or uses a library that handles it.

## 2025-12-15 - Visual Hierarchy in CLI
**Learning:** Adding visual anchors (icons like 🔀, 🐛) and clear separators (›) significantly improves the scanability of dense CLI progress logs.
**Action:** Always pair icons with text labels and ensure a clean fallback for NO_COLOR environments.

## 2025-05-21 - Feedback during Idle States
**Learning:** Adding a countdown timer during long sleep periods (e.g., waiting for new issues) significantly improves the perception of system liveness compared to a static "Sleeping..." log message.
**Action:** Use the `ProgressFooter` to provide dynamic feedback for all long-running wait operations, ensuring the user knows the system is active.
