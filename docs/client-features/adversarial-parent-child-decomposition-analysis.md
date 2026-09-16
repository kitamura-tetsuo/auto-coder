# Adversarial parent/child decomposition analysis

Auto-Coder exposes a provider-independent set analysis for exactly one supplied
parent and its authoritative direct-child membership. The parent is a
contract-free tracking coordinator: before any model use, a shared role-aware
structural assessment (Issue #1951) deterministically rejects a forbidden parent
Requirements heading/declaration, a missing/duplicate/empty parent Objective, or
a missing/invalid child Requirements contract as `BLOCKED` with remediation
`EDIT_IN_PLACE` and `invalid_issue_structure` findings identifying the offending
member and source location; a structurally valid parent always contributes an
empty implementation-Requirements manifest and reaches ordinary semantic
analysis. It consumes each Issue's prebuilt normative Requirement manifest and
uses titles and bodies only as non-normative evidence. The strictly validated
result is `READY`, `BLOCKED`, or fail-closed `ERROR`; blockers identify supplied
Issue identities, one stable decomposition category, and the smallest necessary
contract clarification. A `missing_requirement_ownership` or
`decomposition_false_success` finding always references the parent with an
empty Requirement list and quotes the relevant supplied parent Objective text,
rather than citing a parent Requirement ID (parents never have one). The analysis
detects missing child ownership against the parent's fixed Objective, cross-Issue
contradictions, unstated dependencies, incompatible boundary semantics, and cases
where every child can pass while the parent's Objective still fails, without
imposing one-to-one ownership, internal implementation choices, or a final
independent parent implementation pass. Before verdict, it closes each material
ambiguity across directly related graph consumers, distinguishes membership
changes from member specification-generation changes, traces stale and reusable
individual/set evidence through readiness and authorization, and predicts other
defects already derivable after the proposed clarifications. This closure is
strictly bounded to the caller-supplied parent and complete direct-child set.

Backend selection for this analysis (`create_adversarial_validation_backend_manager(validation_kind="issue")`
in src/auto_coder/cli_helpers.py) prefers `[backend_issue_adversarial_validation]`
in ~/.auto-coder/llm_config.toml when present, falling back to
`[backend_adversarial_validation]` otherwise; it never uses
`[backend_pr_adversarial_validation]`.
