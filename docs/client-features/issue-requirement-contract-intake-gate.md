# Issue requirement-contract intake gate

The shared requirement contract also exposes a provider-independent normative
Issue manifest. It reports explicit-contract presence and structural validity,
and exposes exact, ordered `REQ-NNN` text only for a valid contract. Intake and
adversarial validation consume this same line-oriented manifest boundary;
missing contracts remain explicit in the manifest and invalid contracts expose
no trusted partial entries.

Before any implementation slot, label, branch, local backend, or cloud task is
started for an Issue, Auto-Coder validates an explicit Markdown
`## Requirements` section with the same deterministic parser used by adversarial
PR validation. Explicit contracts must contain only unique `REQ-NNN:` entries;
empty, malformed, and duplicate-ID contracts are rejected with an actionable
Issue comment. The intake gate bypasses the candidate-list cache and reads the
current Issue type and body in one authoritative snapshot. A versioned body
fingerprint prevents repeat scans from posting the same diagnostic, while an
edited body is evaluated again and can proceed normally once valid. Issues
without an explicit Requirements section continue to use legacy requirement
extraction.
