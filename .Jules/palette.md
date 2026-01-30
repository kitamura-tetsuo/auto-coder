## 2026-01-29 - [CLI Spinner Step Pattern]
**Learning:** CLI Spinners often leave debris when updating messages to shorter strings.
**Action:** Use `Spinner.step(message)` which handles padding/clearing automatically instead of direct message assignment.

## 2026-01-30 - [CLI List Formatting]
**Learning:** Hardcoded list formatting (-) in recursive functions misses context-aware coloring opportunities for nested values.
**Action:** Centralize value formatting in a helper function (`_colorize_value`) that handles both color and symbols based on context.
