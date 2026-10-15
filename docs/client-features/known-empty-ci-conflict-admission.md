# Known-empty CI conflict admission

Candidate discovery distinguishes a complete, exact-head `KNOWN_EMPTY` CI
observation from pending CI. An affirmatively conflicting PR may proceed to the
existing mergeability-remediation policy when remediation is enabled; pending,
incomplete, unavailable, mismatched, or missing observations retain their
fail-closed behavior. This admission is not CI success and does not authorize a
merge or CI-failure repair.
