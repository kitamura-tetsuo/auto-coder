# PR validation contract boundaries

Adversarial PR validation strictly separates demonstrated implementation violations,
missing regression protection (test-oracle gaps), and specification defects, ensuring
merge-blocking implementation obligations derive solely from the target Issue's explicit
`## Requirements` manifest.

## Normative contract boundary

The target Issue's explicit `## Requirements` section is the sole authoritative source
of merge-blocking implementation obligations. Each valid requirement is identified by a
stable requirement ID (such as `REQ-001`). Objectives, Context, and Acceptance Scenarios
provide specification-scope evidence, review context, and concrete test scenarios, but
they never create independent merge-blocking implementation obligations outside the explicit
Requirements manifest.

## Implementation findings vs test-oracle gaps

A defect is classified according to the nature of the requirement:
- **Explicit test deliverables**: When an Issue requirement explicitly mandates a regression
  test deliverable in its requirement text (e.g. requiring a negative-control regression test
  or committed test suite), absence of that test deliverable is classified as an
  implementation finding (`NEEDS_FIX`) under that requirement. Any duplicate test-oracle
  gap for the same missing deliverable is deduplicated.
- **Runtime behavior**: When an Issue requirement specifies runtime behavior without an
  explicit test deliverable, missing regression protection is classified as a material
  test-oracle gap (`NEEDS_TESTS`), never as a production implementation finding.
- **Category deduplication**: The system never tracks the same missing test obligation
  under both `findings` and `test_oracle_gaps`. If a requirement specifies runtime behavior,
  missing test protection is tracked only as a test-oracle gap; any duplicate finding is
  removed. Distinct independently reproducible defects (such as a concrete production crash
  and an untested invariant) on the same requirement remain separate.
- **Valid test techniques**: In rereview, equivalent regression tests that assert the required
  behavioral invariant across the component boundary are accepted even if they use a different
  valid technique than illustrated in an Acceptance Scenario. Superficial tests that merely
  assert source text (e.g. AST substring inspection) or use empty inputs without exercising the
  production path are rejected.

## Documentation defect independence

A false claim in documentation that tests or coverage exist is treated as an implementation
defect under the documentation requirement, completely independent of any runtime test-oracle
gap. Correcting or removing the false claim in documentation satisfies the documentation
defect when the requirement permits, but does not satisfy the runtime test-oracle gap. The
runtime test-oracle gap remains open until a focused regression test asserting the runtime
invariant is committed.

## Retired artifact conventions and specification gaps

A pull request that removes or omits retired configuration artifacts or legacy tracking
files (such as `docs/client-features.yaml` when migrating to standalone documentation fragments)
is not rejected under obsolete conventions. Any reviewer objection demanding the retention or
restoration of retired file conventions is converted into a structured `SpecificationGap` rather
than an implementation violation.

Similarly, incomplete implementation or missing coverage of an outcome mentioned only in the
Objective or an Acceptance Scenario whose behavior is not explicitly specified in the
Requirements manifest is converted into a structured `SpecificationGap`.

A `SpecificationGap` records the question, why the existing issue is insufficient, the
observed case, affected scope, and candidate options. Unresolved specification gaps disable
automatic merge (`allows_auto_merge = False`) while preserving the primary verdict (`PASS`,
`NEEDS_TESTS`, or `NEEDS_FIX`). Specification gaps never create code-repair instructions or
mutate the Issue body.

## Response normalization and repair dispatch

Response normalization reconciles model outputs against the authoritative requirement manifest:
1. Replaces misquoted requirement text in valid findings with authoritative text from the
   manifest.
2. Isolates findings citing unknown or sibling-only requirement IDs without failing validation
   when unrelated valid findings survive.
3. Reclassifies retired-artifact and objective/scenario demands as `SpecificationGap`s.
4. Deduplicates overlapping categories between findings and test-oracle gaps.
5. Feedback delivery and repair prompt assembly dispatch only surviving valid findings and
   material test-oracle gaps to repair agents, never converting specification gaps into repair
   instructions or letting older summaries reintroduce rejected obligations.
