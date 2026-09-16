# Adversarial implementation-readiness analysis

Auto-Coder exposes a provider-independent semantic analysis operation for an
Issue's authoritative normative manifest. It treats title, body, examples, and
optional parent context only as non-normative evidence, never reparses Markdown
to redefine Requirements, and does not inspect repository code. The operation
returns a strictly validated `READY`, `BLOCKED`, or fail-closed `ERROR` result.
Blocked findings use stable material-defect categories, exact affected
Requirement IDs, and (for false-success gaps) a concrete incorrect outcome and
the missing normative boundary.

Backend selection for this analysis (`create_adversarial_validation_backend_manager(validation_kind="issue")`
in src/auto_coder/cli_helpers.py) prefers `[backend_issue_adversarial_validation]`
in ~/.auto-coder/llm_config.toml when present, falling back to
`[backend_adversarial_validation]` otherwise; it never uses
`[backend_pr_adversarial_validation]`.
