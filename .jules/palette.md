## 2025-05-20 - CLI Color Accessibility
**Learning:** The CLI uses hardcoded ANSI color codes in `ProgressFooter`, making it inaccessible for users who need high contrast or no color.
**Action:** Implemented `NO_COLOR` support. In future, ensure all CLI output respects `NO_COLOR` or uses a library that handles it.
