# Durable per-Issue Objective anchors

Every supported individual or complete direct-child-set specification review
captures one repository-and-Issue-number-scoped Objective anchor once the
reviewed member is structurally valid for its role (Issue #1953): a contract-free
tracking parent needs only its required Objective, not a valid implementation
Requirements manifest, while a direct child still needs its own valid explicit
Requirements contract; the set as a whole reaches Objective-evidence capture only
once every member, parent included, passes its own role-aware structural check.
The first valid individual baseline takes precedence for legacy migration;
otherwise the first admitted current snapshot wins atomically.
Anchored text and explicit legacy absence survive edits, relationship changes,
concurrent review work, and restart. Individual and decomposition prompts receive
the immutable original state separately from the current structural extraction,
and Objective evidence never enters the normative Requirement manifest. Invalid,
unreadable, or unpersistable required evidence fails closed before semantic review.
Before semantic execution or cached-result reuse, an anchored current Objective is
compared literally after CRLF normalization and outer-whitespace trimming. Missing,
empty, duplicate, or changed text produces a structured `objective_conflict` and
in-place restoration guidance; legacy `UNANCHORED` Issues continue ordinary review.
With an intact anchor, semantic review uses its purpose only to detect concrete
outcome conflicts and reject adjacent scope, never to promote Objective prose into
Requirements.
Complete-set review preserves a distinct fixed purpose for the parent and every
direct child. It checks both each child's scope and whether their explicitly
contracted outcomes compose to the parent's purpose; tracking parents coordinate
missing ownership rather than receiving a final implementation pass. Set findings
support `objective_conflict` in addition to the Objective-coverage categories that
replaced the former explicit parent-Requirement oracles, and unchanged-purpose
restoration is an in-place repair.
