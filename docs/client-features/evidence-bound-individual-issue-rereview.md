# Evidence-bound individual Issue rereview

Individual specification validation retains a versioned evidence envelope for
every accepted decision. The envelope binds the exact repository and Issue,
ordered Requirement manifest and validity, title/body, reconciled role and
parent, related contracts, semantic policy and rerun authority, fixed Objective
and first baseline, findings, dispositions, coverage, and original execution
provenance. Findings receive controller-owned identities independent of their
wording and Requirement references.

An exact current identity reuses its durable decision without a semantic model
call. A changed identity uses the latest durably accepted compatible predecessor
in acceptance order and sends its exact evidence plus a lossless field-level
before/after delta to the production analyzer. Changed role or parent, semantic
policy, rerun authority, or missing/corrupt legacy evidence selects a full
review instead. Provider/model routing is provenance and does not invalidate
compatible semantic evidence.

Incremental output must adjudicate every predecessor finding as `RESOLVED`,
`STILL_VALID`, `REGRESSED`, or `UNVERIFIED`, and account for every current
Requirement plus the contract-wide boundary as freshly reviewed, carried with a
specific no-impact basis, or unresolved. Unknown/duplicate references,
unsupported carry-forward, incomplete coverage, contradictory dispositions,
and unverified evidence fail closed as `ERROR`. A current `READY` decision is
accepted only with complete coverage and no remaining finding. The analyzer may
broaden uncertain impact to fresh or full review and may still report a newly
demonstrated material defect, but does not restart unrelated searches after the
affected boundaries are checked.

Standalone and reconciled-child Review-lane calls use this same lifecycle and
store. The durable audit report exposes the review mode, predecessor key,
assessed delta, stable finding identities and dispositions, and coverage/no-impact
reasons; these fields are evidence, not an alternate readiness authority.

## Self-contained transport interning

Individual standalone and reconciled-child FULL/INCREMENTAL calls now send one
version-1 Individual Evidence Envelope. Current inputs and incremental history
retain separate logical paths. The envelope includes the caller's entire ordered
manifest and validity, title/body, supplied parent context (including null versus
empty), reconciled role/parent/complete related contracts, immutable Objective and
baseline, ordered prior outcomes, predecessor identity/acceptance sequence,
findings/dispositions/coverage/provenance and exact before/after delta. Closed
members remain present. No Markdown is reparsed to supply normative authority.

The envelope's `root` is a typed tree: `m` is an ordered mapping of key-reference
and value-node pairs, `l` an ordered list, `t` a tuple, `v` a scalar and `r` a
string reference. Missing keys, null, empty strings/sequences and bool/int types
remain distinct. A string reference `ie1:<identity>:N` resolves only definition N
of its own envelope/version. Definitions use `s` for literal exact text, `c` for
ordered concatenation without separators, and `j` for exact JSON serialization
of a decoded tree. Compact/pretty styles retain Unicode, original key order or
explicit sorted order, and exact separators/indentation. JSON is decomposed only
when reserialization reproduces the captured string exactly; otherwise it stays
literal. Exact captured Requirement strings and other captured string occurrences
can be shared inside bodies without merging their logical locations or roles.

The complete typed reconstruction is bound by SHA256. Duplicate object keys,
unsupported versions, malformed/dangling/cyclic/cross-envelope references,
conflicting definitions, unused definitions and content corruption fail before
returning any reconstructed authoritative input. Reference-like literal text is
ordinary data. There is no external lookup or partial-input fallback. Production
validates the full envelope before semantic transport and appends decoding
instructions plus every definition to that same prompt; output validation still
uses the complete original manifest and predecessor context.

This representation is transport, not review authority. The existing semantic
policy template is unchanged. The semantic policy identity also binds the global
header and the two receiving instruction templates, because their instructions can affect decisions;
this migration conservatively revalidates older-policy decisions using FULL input.
Changing serialization alone does not manufacture compatible predecessors or rerun
authority. Exact durable reuse under the same complete policy still makes zero
semantic calls. Existing selection uses the latest accepted compatible predecessor
or FULL review for valid current inputs when optional legacy/corrupt/incompatible
history cannot be used. Unsupported/corrupt required current evidence remains
ERROR. Currentness/late-result fences and complete coverage/dispositions remain
required. A decision-affecting semantic-policy change still invalidates reuse.
Retained input JSON must contain every captured field with its supported shape
and matching manifest, specification and relationship identities. A partial
JSON object is not complete evidence. An incomplete newest accepted record
selects FULL review rather than exact-current reuse or an older predecessor.
Complete retained finding payloads must cover the current finding and disposition
identities, and include the captured Objective anchor. Predecessor selection starts
from the newest accepted record before policy/rerun/role compatibility checks;
an intervening incompatible record cannot expose an older compatible predecessor.

Integrity and orchestration tests use independently authored expected logical
inputs and a hand-authored wire tree. They measure complete outgoing prompts,
including references/metadata/instructions, in characters and UTF-8 bytes
separately. Redundant large-family inputs shrink; unique content and metadata can
still exceed a provider limit. These measurements establish neither token counts
nor transport reliability or semantic discrimination.

`uv run python scripts/replay_individual_evidence_transport.py --repository OWNER/REPO
--output NEW_DIRECTORY` renders separate advisory missing-no-write-invariant and
adjacent-avatar-demand cases in FULL/INCREMENTAL form, with complete reconstructed
inputs and prompt hashes. The result records the case version and semantic policy
identity; executed cases also record the backend/model observed after the call,
including fallback selection. Add `--execute` to make one configured read-only semantic
call per case and retain actual responses/results. Rendering and preselected test
responses are not semantic proof. Observed model verdicts are advisory, may vary,
and are not a new deterministic CI or merge gate.

Retained finding validation uses the bound caller manifest, never a fresh body
parse to establish Requirement authority. Production Review-lane regressions
independently reconstruct standalone and reconciled-child FULL/INCREMENTAL
requests from real persisted predecessors, resolved findings and changed closed
siblings. Missing/corrupt optional evidence selects FULL without granting reuse.
