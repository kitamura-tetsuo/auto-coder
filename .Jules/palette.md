## 2026-01-29 - [CLI Spinner Step Pattern]
**Learning:** CLI Spinners often leave debris when updating messages to shorter strings.
**Action:** Use `Spinner.step(message)` which handles padding/clearing automatically instead of direct message assignment.

## 2026-01-30 - [CLI List Formatting]
**Learning:** Hardcoded list formatting (-) in recursive functions misses context-aware coloring opportunities for nested values.
**Action:** Centralize value formatting in a helper function (`_colorize_value`) that handles both color and symbols based on context.

## 2026-02-05 - [CLI Terminal Hyperlinks]
**Learning:** Modern terminals support OSC 8 hyperlinks, allowing clickable text in CLI output which significantly improves navigation for Issues/PRs.
**Action:** Use a helper function `create_terminal_link` with `NO_COLOR` and `isatty` checks to safely add clickable links to summaries.

## 2026-02-19 - [CLI Visual Polish]
**Learning:** Standard Braille spinners are functional but lack the "modern" feel of newer CLI tools; semantic coloring for states like "Completed" or "Pending" improves scanability.
**Action:** Updated spinner frames to an 8-bit "dots" style and expanded semantic color mapping for status keywords.
