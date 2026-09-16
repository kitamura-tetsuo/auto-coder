# Antigravity prompt stdin transport

Local Antigravity tasks use its explicit finite `--input-format text` mode and
deliver the existing stripped, `@`-escaped task through UTF-8 stdin followed by
EOF. This applies to backend aliases, edit/no-edit options, and explicit
continuations while preserving output formatting and result assembly. Configured
positional/print prompt sources and non-text input formats fail before launch.
